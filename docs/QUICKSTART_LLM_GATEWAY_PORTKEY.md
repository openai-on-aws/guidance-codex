# Quick Start: Portkey Gateway Evaluation

Evaluate Codex through Portkey's managed gateway or customer-hosted hybrid data
plane. This path suits teams that want a vendor-managed control plane,
workspace policy, budgets, routing, and analytics.

## Evidence Status

| Layer | Status | Evidence |
|---|---|---|
| Codex custom provider | Verified | Current Codex supports the Responses wire protocol and environment-backed bearer auth |
| Managed endpoint authentication | Verified | `/v1/responses` is reachable and rejects a missing workspace key with HTTP 401 |
| Portkey Responses endpoint | Vendor documented | `/v1/responses`, SSE, reasoning, and function tools |
| Portkey with classic Bedrock | Vendor documented | Assumed-role integration uses `bedrock:InvokeModel*` |
| Continuation on a Bedrock adapter | Known gap | Portkey documents `previous_response_id` as native-provider-only |
| Live customer workspace | Requires credentials | Run `make portkey-validate` with a workspace key and Bedrock provider |
| Hybrid deployment | Requires vendor onboarding | Portkey supplies image credentials, client auth, and organization ID |

Do not call Portkey on Bedrock production-ready until the strict contract probe
passes. The probe verifies semantic continuation, not merely a second
successful HTTP response.

## How a Codex Task Flows

```text
Codex
  -> POST /v1/responses with tools and task context
  -> Portkey authenticates the developer key or JWT
  -> workspace policy selects an allowed Model Catalog provider
  -> Portkey adapts the request to the upstream provider
  -> model returns text or a function call
  -> Codex runs the tool locally in its sandbox
  -> Codex sends the tool result in the next Responses request
  -> loop continues until the model returns a final answer
```

Portkey governs model traffic. It does not run the Codex process, shell, or
local tools.

## Choose a Deployment

### Managed

Use `https://api.portkey.ai/v1`. Portkey operates the data and control planes.
This is the fastest evaluation and requires:

- a Portkey organization and workspace;
- a workspace API key scoped for inference;
- a Model Catalog provider;
- for Bedrock, an AWS role with an external ID and least-privilege model access.

### Hybrid on AWS

The Portkey data plane runs in the customer VPC while Portkey operates the
control plane. The current ECS path requires vendor-issued container
credentials, `PORTKEY_CLIENT_AUTH`, and an organization ID. It also deploys
Redis or ElastiCache and optional S3 log storage.

Treat hybrid as an enterprise procurement and architecture exercise, not an
anonymous public-image deployment. Pin vendor module and image versions, use
private subnets, TLS, a multi-AZ cache, and customer-controlled log retention.

### Public Open-Source Gateway

The public gateway is useful for protocol exploration, but it is not evidence
for managed workspace controls. During this repository's July 2026 audit, the
public `2.0.0` branch identified itself as version `2.2.3` and failed a clean
Linux container build because case-sensitive imports did not resolve. Validate
a vendor-supported release before adopting this route.

## Configure Bedrock in Portkey

1. In **Model Catalog**, create a Bedrock integration.
2. Prefer **AWS Assumed Role** over long-lived access keys.
3. Generate a unique external ID.
4. Scope the role to approved model or inference-profile ARNs and regions.
5. Provision the integration only to the evaluation workspace.
6. Enable only the model used by the walkthrough.

Classic Bedrock access needs:

```json
{
  "Effect": "Allow",
  "Action": [
    "bedrock:InvokeModel",
    "bedrock:InvokeModelWithResponseStream"
  ],
  "Resource": [
    "arn:aws:bedrock:us-east-1::foundation-model/<approved-model>",
    "arn:aws:bedrock:us-east-1:<account-id>:inference-profile/<approved-profile>"
  ]
}
```

Portkey's classic Bedrock assumed-role guide does not establish support for
Bedrock Mantle. Confirm the exact upstream API and IAM actions with Portkey if
the target is a Mantle-served GPT-5 model.

## Run the Evaluation

```bash
cp deployment/portkey/.env.deploy.example \
  deployment/portkey/.env.deploy

# Fill in PORTKEY_API_KEY and PORTKEY_MODEL without committing the file.
make portkey-check
make portkey-codex-config
make portkey-validate
```

Use the Model Catalog form `@<provider-slug>/<model-id>`.

The strict probe tests request fields used by Codex, Responses object shape,
semantic `previous_response_id` continuation, SSE streaming, and forced
function tool calls.

## Codex Configuration

Put this in user-level `~/.codex/config.toml`:

```toml
model_provider = "portkey"
model = "@bedrock-validation/<approved-model-id>"

[model_providers.portkey]
name = "Portkey"
base_url = "https://api.portkey.ai/v1"
env_key = "PORTKEY_API_KEY"
wire_api = "responses"
```

Codex currently supports `responses` as the custom-provider wire protocol. Do
not copy older examples that set `wire_api = "chat"`.

For a short-lived enterprise JWT, replace `env_key` with command-backed auth:

```toml
[model_providers.portkey.auth]
command = "/absolute/path/to/fetch-portkey-token"
refresh_interval_ms = 300000
```

The command must print only the bearer token. Keep static keys in an approved
secret store and rotate them after the evaluation.

## Production Gate

Capture all of the following before promotion:

- strict contract probe output;
- a real Codex task that reads files, invokes a tool, and writes a harmless change;
- user/team identity in Portkey logs;
- a deliberately exceeded rate or usage policy returning a blocking response;
- key revocation timing;
- Bedrock CloudTrail evidence for the assumed role;
- prompt, response, and trace retention settings;
- regional failover and rollback behavior.

## References

- [Portkey Codex integration](https://docs.portkey.ai/docs/integrations/libraries/codex)
- [Portkey Open Responses](https://docs.portkey.ai/docs/product/ai-gateway/responses-api)
- [Portkey Bedrock assumed role](https://docs.portkey.ai/docs/product/model-catalog/connect-bedrock-with-amazon-assumed-role)
- [Portkey hybrid architecture](https://docs.portkey.ai/docs/self-hosting/hybrid-deployments/architecture)
- [Enterprise Gateway Guidance](ENTERPRISE_GATEWAY_GUIDANCE.md)
