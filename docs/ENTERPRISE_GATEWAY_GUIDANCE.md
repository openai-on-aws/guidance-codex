# Enterprise Gateway Guidance

This guide evaluates LiteLLM and Portkey as enterprise gateways between Codex
clients and Amazon Bedrock. It separates documented capability from what this
repository has contract-tested so architecture reviews can make an explicit
decision.

## Required Contract

A gateway is ready for a Codex rollout only when it:

1. Implements `POST /v1/responses` and streaming Responses events.
2. Preserves `reasoning`, `text`, `prompt_cache_key`, and
   `previous_response_id` instead of translating them to a reduced chat schema.
3. Refreshes Bedrock credentials server-side.
4. Authenticates each developer and records a stable user or team identity.
5. Enforces the customer's RPM, TPM, model, and spend policy.
6. Exports redacted audit events without storing prompts by default.

Run the repository contract probe against every candidate and every promoted
environment:

```bash
export GATEWAY_BASE_URL=https://gateway.example.com/v1
export GATEWAY_API_KEY=<test-identity-key>
export GATEWAY_MODEL=gpt-5.6-sol

python3 deployment/scripts/validate-responses-contract.py \
  --include-tool-call
```

The default probe tests SSE streaming; `--include-tool-call` adds a function
tool-call requirement. Passing it is necessary but not sufficient. A
production evaluation must also test cancellation, long-running requests,
rate-limit responses, identity attribution, load, rollback, restore, and the
customer's retention policy.

## Compatibility Matrix

| Capability | LiteLLM reference | Portkey evaluation |
|------------|-------------------|--------------------|
| Responses endpoint | Live contract probe passed in `us-east-1` | Documented by Portkey; strict live probe requires a workspace key |
| Bedrock Runtime GPT-5.6 | Configured with Global cross-Region profiles and server-side token refresh | Require the vendor to identify the exact Runtime API and inference profile |
| Classic Bedrock assumed role | ECS task role | Documented Portkey integration pattern |
| OIDC/JWT | Included middleware; compare licensed LiteLLM features against current vendor terms | Verify Portkey workspace/service-account controls for the selected tier |
| Per-user/team budgets | Vendor documented; prove blocking with customer policy | Vendor documented; prove blocking with customer policy |
| Customer-operated data plane | ECS reference stack | Hybrid ECS requires vendor-issued image credentials, client auth, and organization ID |
| Promotion gate | CI plus Responses contract probe | Same probe, plus vendor-specific integration tests |

`Documented` means the vendor describes the feature. `Verified` means this
repository has an executable path for it. Keep that distinction in customer
architecture documents and in any derived guidance.

## LiteLLM Path

Use LiteLLM when the customer wants a customer-operated gateway, can own
ECS/RDS operations, and values an inspectable reference implementation.

The deployable path is:

```text
Codex -> ALB/WAF -> JWT middleware -> LiteLLM -> Bedrock Runtime
                            |              |
                         DynamoDB       PostgreSQL
```

Use immutable image digests for both containers. The ECS task role is scoped
to the Bedrock Runtime inference and default-project resources needed by the
gateway. RDS generates its password in Secrets Manager; the password is
injected into the container and never rendered in the task definition. For
native LiteLLM Enterprise JWT support, remove the custom middleware only after
reproducing the same issuer, audience, key-rotation, identity, and readiness
tests.

Developer configuration belongs in user-level `~/.codex/config.toml`:

```toml
model = "gpt-5.6-sol"
model_provider = "enterprise-gateway"

[model_providers.enterprise-gateway]
name = "Enterprise Gateway"
base_url = "https://gateway.example.com/v1"
env_key = "ENTERPRISE_GATEWAY_TOKEN"
wire_api = "responses"
```

For short-lived OIDC tokens, use command-backed provider authentication rather
than asking developers to refresh an environment variable manually. The
official Codex custom-provider configuration supports an `auth` command and a
proactive `refresh_interval_ms`.

## Portkey Path

Use Portkey when the customer prefers a managed control plane or an evaluated
hybrid deployment and wants vendor-provided policy, analytics, and virtual-key
workflows.

Start with a non-production workspace and one test identity. Portkey's current
Model Catalog replaces the older virtual-key-first flow. Configure an AWS
role with an external ID and only the provider actions and resources required
by the selected route. Portkey's classic Bedrock assumed-role guidance is not
evidence that the GPT-5.6 Bedrock Runtime Responses path is supported. Require
Portkey to identify the exact upstream API, IAM actions, region, inference
profile, credential refresh behavior, and data path in the customer design.

A Portkey evaluation can use Codex custom-provider bearer authentication:

```toml
model = "<portkey-model-alias>"
model_provider = "portkey"

[model_providers.portkey]
name = "Portkey"
base_url = "https://api.portkey.ai/v1"
wire_api = "responses"
env_key = "PORTKEY_API_KEY"
```

Confirm the endpoint and key scope against the customer's Portkey workspace
before distribution. Keep the key in the operating-system secret store or an
approved credential helper, not in `config.toml`.

The repository probe accepts the same secret headers without putting their
values in command-line arguments:

```bash
export GATEWAY_BASE_URL=https://api.portkey.ai/v1
export PORTKEY_API_KEY=<secret>
export GATEWAY_MODEL=@<provider-slug>/<model-id>

python3 deployment/scripts/validate-responses-contract.py \
  --api-key-env PORTKEY_API_KEY \
  --include-tool-call
```

Portkey documents `previous_response_id` as unavailable for adapter providers,
including classic Bedrock. The repository probe checks that continuation is
semantic, so an ignored continuation ID cannot produce a false pass. See the
[Portkey Quick Start](QUICKSTART_LLM_GATEWAY_PORTKEY.md).

Do not mark the Portkey route production-ready until the contract probe passes
with the intended Bedrock GPT-5.x model and the following evidence is captured:

- AWS CloudTrail or vendor logs identify the assumed role and target service.
- A deliberately exceeded budget returns the expected blocking response.
- Revoking a user or virtual key takes effect within the agreed SLA.
- Prompt, response, and trace retention match the customer's data policy.
- Regional routing and disaster-recovery behavior are documented.

## Enterprise Control Plane

Apply the same controls regardless of gateway:

| Control | Minimum decision |
|---------|------------------|
| Identity | OIDC issuer and audience, group mapping, deprovisioning SLA |
| Authorization | Allowed models, regions, tools, and per-team policy |
| Secrets | Rotation owner, storage system, emergency revocation procedure |
| Data | Prompt/response retention, redaction, residency, support access |
| Reliability | SLO, timeout budget, retries, capacity, failover behavior |
| Observability | Request ID, user/team, model, tokens, latency, status, cost |
| Change management | Immutable artifact, staged promotion, rollback owner |
| Evidence | Contract results, load test, threat model, runbook exercise |

## Promotion Decision

Use three environments with separate credentials and policy objects:

1. **Development:** synthetic data, narrow IAM, contract tests on every change.
2. **Staging:** production topology, load and failure tests, no customer data.
3. **Production:** approved image digest, deletion protection, WAF, alarms,
   backup restore evidence, and a recorded rollback decision.

See [Production Deployment](PRODUCTION_DEPLOYMENT.md) for the reference stack
settings and promotion checklist.

## Primary References

- [Codex custom model providers](https://learn.chatgpt.com/docs/config-file/config-advanced#custom-model-providers)
- [Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference#configtoml)
- [LiteLLM proxy documentation](https://docs.litellm.ai/docs/simple_proxy)
- [LiteLLM budgets](https://docs.litellm.ai/docs/proxy/users)
- [Portkey documentation](https://portkey.ai/docs)
