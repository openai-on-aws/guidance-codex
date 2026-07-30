# Quick Start: LiteLLM Gateway on AWS

> **Status:** Hardened reference baseline
> **Audience:** Organizations evaluating LLM gateway patterns, learning CloudFormation deployment  
> **Production Readiness:** Requires customer landing-zone validation and recorded production evidence (see [Security Considerations](#security-considerations))

Deploy a LiteLLM gateway on ECS Fargate and connect Codex to an Amazon Bedrock
backend. This is the repository's primary enterprise implementation of the
[LLM Gateway pattern](QUICKSTART_LLM_GATEWAY.md).

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
- `asm-exec` available for runtime Secrets Manager references
- [Codex CLI](https://developers.openai.com/codex/cli) installed
- A public Route 53 hosted zone, or an existing ACM certificate in the deployment Region
- A DNS name in that zone for the trusted Codex HTTPS endpoint

---

## Deployment

### Automated Path

The Make targets wrap the detailed commands below and never push git changes:

```bash
cp deployment/litellm/.env.deploy.example deployment/litellm/.env.deploy
# Edit the gitignored file with your profile, DNS, CIDR, and asm-exec path.

make litellm-check
CONFIRM_AWS_WRITE=1 make litellm-build
make litellm-plan
CONFIRM_AWS_WRITE=1 make litellm-deploy
```

After the stack is healthy:

```bash
CONFIRM_AWS_WRITE=1 make litellm-provision-key
make litellm-codex-config
make litellm-validate
```

`litellm-plan` creates a non-executed change set. On the first deployment it
plans networking first; apply networking before planning the dependent gateway
stack. The one-command `litellm-deploy` deploys both stacks sequentially and
requires the explicit write confirmation shown above. `litellm-provision-key`
creates a model-scoped LiteLLM key, writes it to Secrets Manager without
printing it, and records only the secret ID in the gitignored deployment state.

### Step 1: Clone and Set Variables

```bash
git clone https://github.com/openai-on-aws/guidance-codex.git
cd guidance-codex

export AWS_REGION=us-east-1
export BEDROCK_REGION=us-east-1
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export ECR_REGISTRY="$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"
export ALLOWED_CIDR="$(curl -Ls https://checkip.amazonaws.com)/32"
export LITELLM_BASE_IMAGE=ghcr.io/berriai/litellm@sha256:65d84a2282137b4dc73bbe184650a7c807177c533e4223b3bfbc87963fe3fabe
export GATEWAY_DOMAIN_NAME=gateway.example.com
export ROUTE53_HOSTED_ZONE_ID=Z0123456789EXAMPLE

# Alternatively, use an existing certificate and omit ROUTE53_HOSTED_ZONE_ID:
# export ALB_CERTIFICATE_ARN=arn:aws:acm:us-east-1:123456789012:certificate/replace-me

# Read-only checks before building.
python3 deployment/scripts/preflight-litellm.py --stage build
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

# Build and push. The upstream digest was resolved and reviewed in Step 1.
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

# Read-only checks before creating or updating stacks.
python3 deployment/scripts/preflight-litellm.py \
  --stage deploy \
  --check-ecr-image
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

# Deploy gateway (references networking stack via imports)
aws cloudformation deploy \
  --stack-name "$GATEWAY_STACK" \
  --template-file deployment/litellm/ecs/litellm-ecs.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --region "$AWS_REGION" \
  --parameter-overrides \
      NetworkingStackName="$NETWORKING_STACK" \
      EnableOtel="false" \
      DBUsername=litellm \
      AwsRegion="$BEDROCK_REGION" \
      LiteLLMImage="$LITELLM_IMAGE" \
      AlbDomainName="$GATEWAY_DOMAIN_NAME" \
      Route53HostedZoneId="$ROUTE53_HOSTED_ZONE_ID" \
      AllowedCidr="$ALLOWED_CIDR" \
      EnableJwtMiddleware="false"

# To reuse an existing certificate, replace Route53HostedZoneId with:
#     AlbCertificateArn="$ALB_CERTIFICATE_ARN"
#
# If you deployed Step 4, also add:
#     OtelStackName="$OTEL_STACK"
#     EnableOtel="true"

# Get gateway URL
export GATEWAY_URL=$(aws cloudformation describe-stacks \
  --stack-name "$GATEWAY_STACK" --region "$AWS_REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`GatewayEndpoint`].OutputValue' --output text)
export GATEWAY_ADMIN_URL=$(aws cloudformation describe-stacks \
  --stack-name "$GATEWAY_STACK" --region "$AWS_REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`GatewayAdminEndpoint`].OutputValue' --output text)

echo "Gateway URL: $GATEWAY_URL"

# Create a dynamic reference; the secret value is resolved only inside asm-exec.
export LITELLM_SECRET_ARN=$(aws cloudformation describe-stacks \
  --stack-name "$GATEWAY_STACK" --region "$AWS_REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`LiteLLMSecretArn`].OutputValue' --output text)
export MASTER_KEY_REF="{{resolve:secretsmanager:${LITELLM_SECRET_ARN}:SecretString:LITELLM_MASTER_KEY}}"
```

The bundled LiteLLM image now uses LiteLLM's documented
`bedrock_mantle/openai.gpt-5.x` provider and refreshes
`AWS_BEARER_TOKEN_BEDROCK` in-process from the gateway task role using the
official `aws-bedrock-token-generator` package. That matches OpenAI's Bedrock
guidance for long-running applications: use a token provider rather than
manually injecting a static 12-hour bearer token.

With `Route53HostedZoneId`, CloudFormation creates and DNS-validates the ACM
certificate and creates the ALB alias record. With `AlbCertificateArn`, the
certificate is reused; create the matching DNS alias in your existing DNS
workflow.

---

## Developer Configuration

### Get API Key

#### Option A: Admin-Generated Keys

```bash
# Generate key for a user without exposing the admin key to the shell.
asm-exec curl -X POST "$GATEWAY_ADMIN_URL/key/generate" \
  -H "Authorization: Bearer $MASTER_KEY_REF" \
  -H "Content-Type: application/json" \
  -d '{
    "key_alias": "alice@company.com",
    "user_id": "alice@company.com",
    "models": ["gpt-5.5", "gpt-5.4"],
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
model = "gpt-5.5"         # Walkthrough model; verify account and Region availability
web_search = "disabled"   # Bedrock Mantle does not accept the hosted web_search tool type

[model_providers.litellm-gateway]
name = "LiteLLM Gateway"
base_url = "<gateway-endpoint>"  # Paste the exact GatewayEndpoint value, including scheme and /v1
wire_api = "responses"    # Optional but explicit; custom providers default to Responses

[model_providers.litellm-gateway.auth]
command = "/usr/bin/env"
args = ["AWS_PROFILE=<developer-profile>", "AWS_REGION=<region>", "PATH=/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin", "<absolute-path-to-asm-exec>", "/usr/bin/printf", "%s\n", "<scoped-key-secrets-manager-reference>"]
refresh_interval_ms = 300000
```

`make litellm-codex-config` prints this block with the deployed endpoint,
resolver path, and scoped-key reference filled in. Bedrock Mantle serves GPT-5.x
through the Responses API, so `wire_api = "responses"` is the right setting
here. Keep this provider block in user-level `~/.codex/config.toml`; Codex
ignores provider and auth settings in project-local `.codex/config.toml`.
For customer rollout, replace the deployment profile with a developer profile
that can read only this scoped-key secret and decrypt it with the stack KMS key.

Test:

```bash
codex exec "Create a hello world function in Python"

# Expected: Codex returns Python code, no auth/connection errors
```

Before promotion, run the gateway contract probe with a synthetic identity:

```bash
make litellm-validate
```

This validates Responses fields, continuation, SSE streaming, and a function
tool call. It is a contract check, not a substitute for load, policy, rollback,
and restore tests.

---

## Quota Management

### Per-User Budgets

```bash
asm-exec curl -X POST "$GATEWAY_ADMIN_URL/key/generate" \
  -H "Authorization: Bearer $MASTER_KEY_REF" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "bob@company.com",
    "max_budget": 100.0,
    "budget_duration": "30d"
  }'
```

### Per-Team Budgets

```bash
asm-exec curl -X POST "$GATEWAY_ADMIN_URL/key/generate" \
  -H "Authorization: Bearer $MASTER_KEY_REF" \
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
# The helper resolves the scoped key at runtime and tests the full contract.
make litellm-validate
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
# Confirm the configured walkthrough model works:
GATEWAY_MODEL=gpt-5.5 make litellm-validate
```

If a specific model times out, verify it is available for your account and
region (see [reference-regions.md](reference-regions.md)) and request model
access in the **Amazon Bedrock → Model access** console before relying on it.

### Codex fails TLS verification but `curl -k` works

**Cause:** `GatewayEndpoint` is using the raw ALB DNS name while the ACM
certificate is issued for a different hostname.

**Fix:**
```bash
# Deploy or update with managed DNS and certificate validation.
AlbDomainName="$GATEWAY_DOMAIN_NAME"
Route53HostedZoneId="$ROUTE53_HOSTED_ZONE_ID"
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
# Delete the scoped key secret after revoking the matching LiteLLM virtual key
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

> **Note on GPT-5.4 / GPT-5.5:** These models are Responses-only on Bedrock Mantle. The `bedrock_mantle/` prefix keeps LiteLLM on its documented Mantle Responses provider, which preserves the Responses payload shape Codex expects. Use the newest model that is approved and available in the selected customer account and Region. The bundled LiteLLM image refreshes `AWS_BEARER_TOKEN_BEDROCK` automatically from the gateway's AWS credential chain, and LiteLLM derives the Mantle endpoint from the selected Region. See `reference-regions.md` before choosing a different Region.

Rebuild and redeploy the image (Steps 2 & 6).

### Custom JWT Middleware

For OIDC self-service portal, see [deployment/litellm/jwt-middleware/README.md](../deployment/litellm/jwt-middleware/README.md).

---

## Support

- **LiteLLM Documentation:** [docs.litellm.ai](https://docs.litellm.ai)
- **Pattern Documentation:** [QUICKSTART_LLM_GATEWAY.md](QUICKSTART_LLM_GATEWAY.md)
- **Issues:** [GitHub Issues](https://github.com/openai-on-aws/guidance-codex/issues)
- **Codex Configuration:** [OpenAI Codex docs](https://developers.openai.com/codex/config-advanced)
