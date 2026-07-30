# Quick Start: LiteLLM Gateway on AWS

> **Status:** Reference Implementation  
> **Audience:** Organizations evaluating LLM gateway patterns, learning CloudFormation deployment  
> **Production Readiness:** Requires security hardening before production use (see [Security Considerations](#security-considerations))

Deploy LiteLLM gateway on ECS Fargate for OpenAI Codex with Amazon Bedrock backend. This is the AWS-maintained reference implementation of the [LLM Gateway pattern](QUICKSTART_LLM_GATEWAY.md).

**Features:**
- Per-user and per-team budget limits (`max_budget`, `budget_duration`)
- Rate limiting (RPM and TPM controls)
- Model routing and fallback
- Admin API for key generation
- Optional OIDC self-service portal
- CloudWatch metrics via OpenTelemetry

---

## Prerequisites

- AWS account with admin permissions (ECS, VPC, ALB, RDS, CloudFormation, ECR, Secrets Manager)
- Amazon Bedrock activated in target region (this walkthrough uses `us-east-1`)
- AWS CLI v2 installed and authenticated
- Docker installed and running
- `jq` for parsing CloudFormation outputs (optional but recommended)
- [Codex CLI](https://developers.openai.com/codex/cli) installed
- ACM certificate in `us-east-1` for the HTTPS hostname you want the gateway to use
- For trusted Codex/browser HTTPS, a DNS name you control must resolve to the ALB and match that ACM certificate

---

## Deployment

### Step 1: Clone and Set Variables

```bash
git clone https://github.com/openai-on-aws/guidance-codex.git
cd guidance-codex

export AWS_REGION=us-east-1
export BEDROCK_REGION=us-east-1
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export ECR_REGISTRY="$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"
export ALB_CERTIFICATE_ARN=arn:aws:acm:us-east-1:123456789012:certificate/replace-me
export ALLOWED_CIDR="$(curl -Ls https://checkip.amazonaws.com)/32"

# Optional but recommended for trusted HTTPS.
# If you omit this, the stack returns the ALB DNS name and you'll need `curl -k`
# for low-level smoke tests because the ACM certificate will not match the ALB hostname.
# Codex itself should use a cert-matching hostname, not the raw ALB DNS name.
export GATEWAY_DOMAIN_NAME=gateway.example.com
```

### Step 2: Build and Push LiteLLM Image

```bash
# Create ECR repository
export LITELLM_REPO=codex-litellm
aws ecr create-repository \
  --repository-name "$LITELLM_REPO" \
  --region "$AWS_REGION" \
  --image-scanning-configuration scanOnPush=true \
  || echo "Repository already exists"

# Authenticate Docker to ECR
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$ECR_REGISTRY"

# Build and push. Resolve and review the upstream digest before this step.
export LITELLM_BASE_IMAGE=ghcr.io/berriai/litellm@sha256:<reviewed-digest>
export LITELLM_IMAGE_TAG=v1
export LITELLM_IMAGE_TAGGED="$ECR_REGISTRY/$LITELLM_REPO:$LITELLM_IMAGE_TAG"

docker buildx create --use --name codex-builder 2>/dev/null || true
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  --build-arg LITELLM_BASE_IMAGE="$LITELLM_BASE_IMAGE" \
  --tag "$LITELLM_IMAGE_TAGGED" \
  --file deployment/litellm/Dockerfile \
  --push \
  deployment/litellm
```

**For single-arch (faster, recommended on Apple Silicon):**
```bash
docker buildx build \
  --builder codex-builder \
  --platform linux/amd64 \
  --build-arg LITELLM_BASE_IMAGE="$LITELLM_BASE_IMAGE" \
  --tag "$LITELLM_IMAGE_TAGGED" \
  --file deployment/litellm/Dockerfile \
  --push \
  deployment/litellm
```

After either build, resolve the immutable ECR digest used by CloudFormation:

```bash
export LITELLM_IMAGE_DIGEST=$(aws ecr describe-images \
  --repository-name "$LITELLM_REPO" \
  --image-ids imageTag="$LITELLM_IMAGE_TAG" \
  --region "$AWS_REGION" \
  --query 'imageDetails[0].imageDigest' \
  --output text)
export LITELLM_IMAGE="$ECR_REGISTRY/$LITELLM_REPO@$LITELLM_IMAGE_DIGEST"
```

### Step 3: Deploy Networking

```bash
export NETWORKING_STACK=codex-networking

aws cloudformation deploy \
  --stack-name "$NETWORKING_STACK" \
  --template-file deployment/infrastructure/networking.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --region "$AWS_REGION" \
  --parameter-overrides VpcCidr=10.0.0.0/16
```

### Step 4 (Optional): Gateway telemetry

For CloudWatch metrics on the gateway path, the LiteLLM gateway emits its own
telemetry via the collector config at
`deployment/litellm/otel-collector-config.yaml`, visualized by
`deployment/infrastructure/litellm-dashboard.yaml`. Keep `EnableOtel="false"` in
Step 6 unless you have wired up a collector endpoint the gateway can export to.

### Step 5 (Optional): Deploy User-Key-Mapping for OIDC

Only if enabling OIDC self-service:

```bash
export USERKEY_STACK=codex-user-key-mapping

aws cloudformation deploy \
  --stack-name "$USERKEY_STACK" \
  --template-file deployment/litellm/ecs/user-key-mapping.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --region "$AWS_REGION" \
  --parameter-overrides TableName=codex-user-keys
```

### Step 6: Deploy LiteLLM Gateway

```bash
export GATEWAY_STACK=codex-litellm-gateway
export MASTER_KEY=$(openssl rand -hex 32)

# Deploy gateway (references networking stack via imports)
aws cloudformation deploy \
  --stack-name "$GATEWAY_STACK" \
  --template-file deployment/litellm/ecs/litellm-ecs.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --region "$AWS_REGION" \
  --parameter-overrides \
      NetworkingStackName="$NETWORKING_STACK" \
      EnableOtel="false" \
      LiteLLMMasterKey="$MASTER_KEY" \
      DBUsername=litellm \
      AwsRegion="$BEDROCK_REGION" \
      LiteLLMImage="$LITELLM_IMAGE" \
      AlbCertificateArn="$ALB_CERTIFICATE_ARN" \
      AllowedCidr="$ALLOWED_CIDR" \
      EnableJwtMiddleware="false"

# For trusted HTTPS, add:
#     AlbDomainName="$GATEWAY_DOMAIN_NAME"
#
# If you deployed Step 4, also add:
#     OtelStackName="$OTEL_STACK"
#     EnableOtel="true"

# Save credentials
echo "LITELLM_MASTER_KEY=$MASTER_KEY" > .env.gateway
chmod 600 .env.gateway

# Get gateway URL
export GATEWAY_URL=$(aws cloudformation describe-stacks \
  --stack-name "$GATEWAY_STACK" --region "$AWS_REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`GatewayEndpoint`].OutputValue' --output text)

echo "Gateway URL: $GATEWAY_URL"
```

The bundled LiteLLM image now uses LiteLLM's documented
`bedrock_mantle/openai.gpt-5.x` provider and refreshes
`AWS_BEARER_TOKEN_BEDROCK` in-process from the gateway task role using the
official `aws-bedrock-token-generator` package. That matches OpenAI's Bedrock
guidance for long-running applications: use a token provider rather than
manually injecting a static 12-hour bearer token.

If you omit `AlbDomainName`, CloudFormation returns `https://<alb-dns>/v1` as
`GatewayEndpoint`. That endpoint is useful for low-level smoke tests with
`curl -k`, but it is not a trusted Codex endpoint because the ACM certificate
does not match the raw ALB hostname.

---

## Developer Configuration

### Get API Key

#### Option A: Admin-Generated Keys

```bash
# Generate key for a user
curl -X POST "$GATEWAY_URL/key/generate" \
  -H "Authorization: Bearer $MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "key_alias": "alice@company.com",
    "user_id": "alice@company.com",
    "models": ["gpt-5.5", "gpt-5.4", "gpt-oss-120b"],
    "max_budget": 50.0,
    "budget_duration": "30d",
    "tpm_limit": 100000,
    "rpm_limit": 1000
  }'

# Returns: {"key": "sk-litellm-..."}
```

#### Option B: Self-Service OIDC

If you deployed with `EnableJwtMiddleware=true`, see [deployment/litellm/jwt-middleware/README.md](../deployment/litellm/jwt-middleware/README.md) for OIDC setup.

### Codex Configuration

Developers add this to the user-level `~/.codex/config.toml`:

```toml
model_provider = "litellm-gateway"
model = "gpt-5.5"         # Preferred latest GPT-5 model when available
web_search = "disabled"   # Bedrock Mantle does not accept the hosted web_search tool type

[model_providers.litellm-gateway]
name = "LiteLLM Gateway"
base_url = "<gateway-endpoint>"  # Paste the exact GatewayEndpoint value, including scheme and /v1
env_key = "OPENAI_API_KEY"
wire_api = "responses"    # Optional but explicit; custom providers default to Responses
```

> **Note:** Bedrock Mantle serves GPT-5.x through the Responses API, so `wire_api = "responses"` is the right setting here. Codex custom providers already default to Responses; this guide keeps the setting explicit because it makes the Mantle dependency obvious. `web_search = "disabled"` is a Bedrock Mantle compatibility choice, not a general OpenAI recommendation. The bundled LiteLLM image refreshes its upstream Mantle bearer token automatically from the gateway's AWS credential chain. For Codex itself, `base_url` should be a trusted HTTPS hostname whose certificate matches the URL. Keep this provider block in user-level `~/.codex/config.toml`; Codex ignores provider and auth settings in project-local `.codex/config.toml`.

Set API key:

```bash
# macOS / Linux
echo 'export OPENAI_API_KEY=sk-litellm-xxxxxxxxxxxxx' >> ~/.zshrc
source ~/.zshrc

# Windows PowerShell (replace sk-litellm-xxx with actual key from /key/generate)
[Environment]::SetEnvironmentVariable("OPENAI_API_KEY", "sk-litellm-xxxxxxxxxxxxx", "User")
```

Test:

```bash
codex exec "Create a hello world function in Python"

# Expected: Codex returns Python code, no auth/connection errors
```

---

## Quota Management

### Per-User Budgets

```bash
curl -X POST "$GATEWAY_URL/key/generate" \
  -H "Authorization: Bearer $MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "bob@company.com",
    "max_budget": 100.0,
    "budget_duration": "30d"
  }'
```

### Per-Team Budgets

```bash
curl -X POST "$GATEWAY_URL/key/generate" \
  -H "Authorization: Bearer $MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "team_id": "platform-team",
    "max_budget": 500.0,
    "budget_duration": "30d",
    "tpm_limit": 100000,
    "rpm_limit": 1000
  }'
```

### Check Usage

```bash
curl -X GET "$GATEWAY_URL/user/info" \
  -H "Authorization: Bearer $USER_API_KEY"
```

**Documentation:**
- [LiteLLM User Budgets](https://docs.litellm.ai/docs/proxy/users)
- [LiteLLM Team Budgets](https://docs.litellm.ai/docs/proxy/team_budgets)
- [LiteLLM Rate Limiting](https://docs.litellm.ai/docs/proxy/rate_limit_tiers)

---

## Monitoring

If you deployed the OTel collector (Step 4), metrics flow to CloudWatch
namespace `Codex` by default unless you override `MetricsNamespace` in
the collector stack:

```bash
aws cloudwatch list-metrics \
  --namespace Codex \
  --region "$AWS_REGION" \
  --query 'Metrics[0:5].[MetricName]' \
  --output table
```

**Metrics available:**
- `gen_ai.client.operation.duration` - Request latency
- `gen_ai.client.token.usage` - Token usage
- `litellm.request_total_cost_usd` - Request costs

**Dashboard:**
```bash
aws cloudformation deploy \
  --stack-name codex-litellm-dashboard \
  --template-file deployment/infrastructure/litellm-dashboard.yaml \
  --parameter-overrides MetricsNamespace=Codex \
  --region "$AWS_REGION"
```

---

## Troubleshooting

### Gateway returns 500 "Database connection failed"

**Cause:** RDS not accessible from ECS tasks

**Fix:**
```bash
aws logs tail "/ecs/$GATEWAY_STACK" --follow --region "$AWS_REGION"

# Check security groups
aws ec2 describe-security-groups \
  --filters "Name=tag:aws:cloudformation:stack-name,Values=$GATEWAY_STACK" \
  --query 'SecurityGroups[*].[GroupId,GroupName]'
```

### Gateway returns 403 "AccessDeniedException" calling Bedrock

**Cause:** ECS task role missing Bedrock permissions

**Fix:**
```bash
# Get task role name from stack resources
TASK_ROLE=$(aws cloudformation describe-stack-resource \
  --stack-name "$GATEWAY_STACK" --region "$AWS_REGION" \
  --logical-resource-id TaskRole \
  --query 'StackResourceDetail.PhysicalResourceId' --output text)

aws iam list-attached-role-policies --role-name "$TASK_ROLE"
```

### Codex returns 401 "Unauthorized"

**Cause:** API key wrong or expired

**Fix:**
```bash
# Verify API key is set
echo $OPENAI_API_KEY

# Test key directly
curl -X POST "$GATEWAY_URL/v1/responses" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-5.5","input":"test"}'
```

If `GATEWAY_URL` uses the raw ALB DNS name instead of a cert-matching domain,
add `-k` for this low-level smoke test.

### Request to a specific model hangs, then returns 504 Gateway Time-out

**Cause:** The requested model is not served by Bedrock Mantle in your region or
is not enabled for your account. The upstream call never returns and the ALB
closes the connection at its idle timeout (~60s). In testing, `gpt-5.5`
returned normally while `gpt-5.4` timed out in the same region/account.

**Fix:**
```bash
# Prefer gpt-5.5 (the recommended Codex model). Confirm the model works:
curl -X POST "$GATEWAY_URL/responses" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-5.5","input":"ping"}'
```

If a specific model times out, verify it is available for your account and
region (see [reference-regions.md](reference-regions.md)) and request model
access in the **Amazon Bedrock → Model access** console before relying on it.

### Codex fails TLS verification but `curl -k` works

**Cause:** `GatewayEndpoint` is using the raw ALB DNS name while the ACM
certificate is issued for a different hostname.

**Fix:**
```bash
# Deploy or update the stack with a DNS name that resolves to the ALB and
# matches the ACM certificate.
AlbDomainName="$GATEWAY_DOMAIN_NAME"
```

Use the raw ALB DNS name only for low-level `curl -k` smoke tests.

### Docker build fails

**Cause:** Docker not running

**Fix:**
```bash
docker ps
# If error, start Docker Desktop
```

---

## Security Considerations

This reference implementation includes secure defaults but still requires a
customer landing zone, threat model, load test, and operational review before
production use.

**Already implemented:**
- RDS-managed database password in Secrets Manager
- KMS encryption for RDS, logs, secrets, and user-key mappings
- ALB access logging and retained stateful resources
- ECS deployment rollback, target-tracking autoscaling, alarms, and optional WAF
- Region and Mantle-project scoping on the ECS task role
- Immutable ECS image references

**Hardening Checklist:**
- [ ] Use private ECS and database subnets with `AssignPublicIp=DISABLED`
- [ ] Enable deletion protection after the first successful deployment
- [ ] Configure master-key rotation and emergency revocation
- [ ] Enable and tune WAF for the customer's traffic profile
- [ ] Enable VPC Flow Logs and GuardDuty
- [ ] Configure Security Hub benchmarks (CIS AWS Foundations)
- [ ] Restore an RDS snapshot and exercise rollback in staging
- [ ] Run the Responses contract and customer load tests

For production settings and promotion gates, see
[Production Deployment](PRODUCTION_DEPLOYMENT.md).

---

## Cleanup

```bash
# Delete gateway stack
aws cloudformation delete-stack --stack-name "$GATEWAY_STACK" --region "$AWS_REGION"

# Delete optional stacks
aws cloudformation delete-stack --stack-name "$USERKEY_STACK" --region "$AWS_REGION"
aws cloudformation delete-stack --stack-name "$OTEL_STACK" --region "$AWS_REGION"

# Delete networking (wait for above to complete first)
aws cloudformation wait stack-delete-complete --stack-name "$GATEWAY_STACK" --region "$AWS_REGION"
aws cloudformation delete-stack --stack-name "$NETWORKING_STACK" --region "$AWS_REGION"

# Delete ECR images
aws ecr delete-repository --repository-name "$LITELLM_REPO" --region "$AWS_REGION" --force

# Developers remove config
# Delete litellm-gateway block from ~/.codex/config.toml
# unset OPENAI_API_KEY
```

---

## Advanced Configuration

### Model Routing

Edit `deployment/litellm/litellm_config.yaml`:

```yaml
model_list:
  - model_name: gpt-5.4
    litellm_params:
      model: bedrock_mantle/openai.gpt-5.4

  - model_name: gpt-5.5
    litellm_params:
      model: bedrock_mantle/openai.gpt-5.5
```

> **Note on GPT-5.4 / GPT-5.5:** These models are Responses-only on Bedrock Mantle. The `bedrock_mantle/` prefix keeps LiteLLM on its documented Mantle Responses provider, which preserves the OpenAI Responses payload shape Codex expects. Prefer `gpt-5.5` when you want the latest OpenAI-recommended Codex model; keep `gpt-5.4` when you need broader Bedrock regional coverage. The bundled LiteLLM image refreshes `AWS_BEARER_TOKEN_BEDROCK` automatically from the gateway's AWS credential chain, and LiteLLM derives the Mantle endpoint from the selected region. GPT-5.4 is also available in `us-west-2` — see `reference-regions.md` if you prefer a different region.

Rebuild and redeploy the image (Steps 2 & 6).

### Custom JWT Middleware

For OIDC self-service portal, see [deployment/litellm/jwt-middleware/README.md](../deployment/litellm/jwt-middleware/README.md).

---

## Support

- **LiteLLM Documentation:** [docs.litellm.ai](https://docs.litellm.ai)
- **Pattern Documentation:** [QUICKSTART_LLM_GATEWAY.md](QUICKSTART_LLM_GATEWAY.md)
- **Issues:** [GitHub Issues](https://github.com/openai-on-aws/guidance-codex/issues)
- **Codex Configuration:** [OpenAI Codex docs](https://developers.openai.com/codex/config-advanced)
