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
3. Refreshes Bedrock Mantle credentials server-side.
4. Authenticates each developer and records a stable user or team identity.
5. Enforces the customer's RPM, TPM, model, and spend policy.
6. Exports redacted audit events without storing prompts by default.

Run the repository contract probe against every candidate and every promoted
environment:

```bash
export GATEWAY_BASE_URL=https://gateway.example.com/v1
export GATEWAY_API_KEY=<test-identity-key>
export GATEWAY_MODEL=gpt-5.5

python3 deployment/scripts/validate-responses-contract.py
```

Passing this probe is necessary but not sufficient. A production evaluation
must also test streaming, tool calls, cancellation, long-running requests,
rate-limit responses, identity attribution, and the customer's retention
policy.

## Compatibility Matrix

| Capability | LiteLLM reference | Portkey evaluation |
|------------|-------------------|--------------------|
| Responses endpoint | Configured and tested by this repository | Documented by Portkey; run the repository probe |
| Bedrock Mantle GPT-5.x | Configured with `bedrock_mantle/` and server-side token refresh | Not verified by this repository; do not infer Mantle support from classic Bedrock support |
| Classic Bedrock assumed role | ECS task role | Documented Portkey integration pattern |
| OIDC/JWT | Included middleware or LiteLLM Enterprise | Use Portkey's workspace/service-account controls and verify the selected deployment tier |
| Per-user/team budgets | LiteLLM budgets and rate limits | Portkey budgets, rate limits, and virtual keys |
| Customer-operated data plane | ECS reference stack | Evaluate Portkey hybrid deployment and support terms |
| Promotion gate | CI plus Responses contract probe | Same probe, plus vendor-specific integration tests |

`Documented` means the vendor describes the feature. `Verified` means this
repository has an executable path for it. Keep that distinction in customer
architecture documents and in any derived blog post.

## LiteLLM Path

Use LiteLLM when the customer wants a customer-operated gateway, can own
ECS/RDS operations, and values an inspectable reference implementation.

The deployable path is:

```text
Codex -> ALB/WAF -> JWT middleware -> LiteLLM -> Bedrock Mantle
                            |              |
                         DynamoDB       PostgreSQL
```

Use immutable image digests for both containers. The ECS task role is scoped
to one region and Mantle project. RDS generates its password in Secrets
Manager; the password is injected into the container and never rendered in the
task definition. For native LiteLLM Enterprise JWT support, remove the custom
middleware only after reproducing the same issuer, audience, key-rotation,
identity, and readiness tests.

Developer configuration belongs in user-level `~/.codex/config.toml`:

```toml
model = "gpt-5.5"
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

Start with a non-production workspace and one test identity. Configure an AWS
role with an external ID and only the provider actions and resources required
by the selected route. Portkey's classic Bedrock assumed-role guidance is not
evidence that Bedrock Mantle's Responses endpoint is supported. Require Portkey
to identify the exact upstream API, IAM actions, region, model identifier,
credential refresh behavior, and data path in the customer design.

A header-based Portkey evaluation can use Codex custom-provider environment
headers:

```toml
model = "<portkey-model-alias>"
model_provider = "portkey"

[model_providers.portkey]
name = "Portkey"
base_url = "https://api.portkey.ai/v1"
wire_api = "responses"
env_http_headers = {
  "x-portkey-api-key" = "PORTKEY_API_KEY",
  "x-portkey-virtual-key" = "PORTKEY_VIRTUAL_KEY"
}
```

Confirm the endpoint and required headers against the customer's Portkey
workspace before distribution. Keep both values in the operating-system secret
store or an approved credential helper, not in `config.toml`.

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
