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
| Responses endpoint | Live contract probe passed in `us-east-1` | Portkey documents `/v1/responses`; strict live probe still requires a workspace key |
| Bedrock Mantle GPT-5.x | Configured with `bedrock_mantle/` and server-side token refresh | Dedicated `bedrock-mantle` provider uses the EKS service role and an explicit model allowlist; live workspace evidence is pending |
| Classic Bedrock assumed role | ECS task role | Documented Portkey integration pattern |
| OIDC/JWT | Included middleware; compare licensed LiteLLM features against current vendor terms | Verify Portkey workspace/service-account controls for the selected tier |
| Per-user/team budgets | Vendor documented; prove blocking with customer policy | Vendor documented; prove blocking with customer policy |
| Customer-operated data plane | ECS reference stack | EKS/Helm data plane with an internal ACM-backed NLB TLS listener, S3 logs, and IRSA; vendor-issued image credentials, client auth, and organization ID are required |
| Promotion gate | CI plus Responses contract probe | Same probe, plus vendor-specific integration tests |

`Documented` means the vendor describes the feature. `Verified` means this
repository has an executable path for it. Keep that distinction in customer
architecture documents and in any derived guidance.

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

Start with a non-production workspace and one test identity. Deploy the
Enterprise gateway to EKS using the repository Helm workflow. Portkey's current
Model Catalog replaces the older virtual-key-first flow. Select the dedicated
**Bedrock Mantle** provider with **Service Role (EKS / IRSA)** authentication; classic
**Bedrock** uses Converse/Invoke and its Responses adapter does not provide
stateful `previous_response_id`. The repository CloudFormation path trusts only
the configured Kubernetes service account. `AWS_REGION` selects EKS,
CloudFormation, S3 logging, and AWS Load Balancer Controller resources;
`BEDROCK_MANTLE_REGION` independently selects the Mantle endpoint and IAM region
condition. Both default to the offline-test reference region, `us-east-1`, but
may differ when they remain in the same AWS partition.

Restricted-egress designs must separate node image pulls from pod traffic.
Worker nodes/container runtimes need Docker Hub registry, authentication, and
content access for the values template's hard-coded
`docker.io/portkeyai/gateway_enterprise` and `docker.io/redis` repositories. An
approved mirror requires a reviewed values-template customization because no
repository override is exposed. Gateway pods separately need HTTPS access to
Portkey `api.portkey.ai`/`albus.portkey.ai`, AWS STS for IRSA, regional S3 in
`AWS_REGION`, and `bedrock-mantle.<BEDROCK_MANTLE_REGION>.api.aws`. Provide NAT
or service-specific VPC endpoints where the service and region support them;
Portkey outbound PrivateLink remains the separate vendor-assisted configuration
described in the quick start.

This path requires Helm CLI 3; CI and the reference render use 3.21.4. Helm 4
changes `--post-renderer` to plugin semantics and is unsupported by the
checked-in executable-post-renderer workflow.

Require approved tag/digest pairs for both the Portkey gateway and Redis.
Moving gateway aliases such as `latest`, `edge`, and `main-latest` are rejected;
the Redis tag remains patch-pinned. The final references use
`repository:<tag>@<configured-digest>`, so the digest—not the tag—selects
content.
Digest pinning establishes artifact identity, not a publisher signature or
provenance. The workflow validates syntax but does not resolve private registry
tags; approve each pair through Portkey or the organization's registry-promotion
process. The included `t4g.medium` nodes require a multi-architecture index
containing `linux/arm64` or a compatible `linux/arm64` manifest. Existing
environment files without both digest values fail before a Helm write. Roll
back by redeploying a prior approved tag/digest pair, not by restoring a
pre-digest Helm revision.

Before deployment, require exclusive or explicitly dedicated stack,
namespace/gateway-service-account, and Helm-release names. The workflow creates
a gateway service account only when it and its deterministic `eksctl` IAM
stack are both absent. It allows an idempotent rerun only after verifying the
existing pair's stack, role, trust, attached policy, and Kubernetes annotation;
it never adopts an unmanaged same-named account. Partial, drifted, or unreadable
state fails before writes. A same-named CloudFormation stack or Helm release can
still be updated, so resolve those collisions with the existing owner.

The included durable client path is deliberately private:

```text
Codex over corporate/VPN routing
  -> customer-owned private DNS hostname
  -> internal IPv4 NLB TLS :443 (ACM; approved prefix list only)
  -> Portkey gateway TCP :8787
```

The gateway is the sole `LoadBalancer` Service and sets
`allocateLoadBalancerNodePorts: false`; Redis uses `ClusterIP`, and neither
Service may retain a `nodePort`.

Before deployment, the customer must provide private routing and resolver
access to the EKS VPC, a controlled hostname, an issued certificate in the
authenticated AWS account and `AWS_REGION` whose SAN covers that hostname,
and an active customer-managed IPv4 prefix list in that account and region
containing only approved corporate/VPN source networks. Configure them with
`PORTKEY_GATEWAY_HOSTNAME`,
`PORTKEY_NLB_TLS_CERTIFICATE_ARN`, and
`PORTKEY_NLB_ALLOWED_PREFIX_LIST_IDS`; set
`PORTKEY_BASE_URL=https://<PORTKEY_GATEWAY_HOSTNAME>/v1` for durable Codex use.
The workflow rejects entries that individually or together cover all IPv4
addresses and caps the lists' aggregate `MaxEntries` at 60. This is a
conservative frontend-security-group quota guard because AWS charges
`MaxEntries`, not the current entry count; a lower account quota or other rules
can still exhaust capacity. It does not provision or delete the certificate,
DNS record, prefix list, VPN, routes, or resolver rules. Create the private DNS
record after the NLB hostname exists and verify it from an approved routed
client. If the certificate uses a private CA, every Codex client must trust
that CA.

The deployment principal needs fail-closed read access as well as mutation
permissions: `sts:GetCallerIdentity`, `eks:DescribeCluster`,
`acm:DescribeCertificate`, `ec2:DescribeManagedPrefixLists`,
`ec2:GetManagedPrefixListEntries`, `cloudformation:ValidateTemplate`,
`cloudformation:DescribeStacks`, `cloudformation:GetTemplate`,
`cloudformation:DescribeStackResource`, `iam:GetRole`, `iam:GetRolePolicy`,
`iam:GetPolicy`, `iam:GetPolicyVersion`, `iam:ListRolePolicies`, and
`iam:ListAttachedRolePolicies`. Denying one of these reads stops the check; it
does not downgrade to an unverified deployment.

Before changing a walkthrough-managed controller policy, `portkey-lbc-deploy`
also validates the configured hostname, same-account/region issued ACM
certificate and SAN, and active customer-managed prefix lists and their
entries. Reusing an externally managed controller instead requires a fresh
cluster-owner attestation on each applicable command:

```bash
PORTKEY_EXTERNAL_LBC_POLICY_CONFIRMED=true \
  CONFIRM_AWS_WRITE=1 make portkey-lbc-deploy
PORTKEY_EXTERNAL_LBC_POLICY_CONFIRMED=true make portkey-lbc-status
PORTKEY_EXTERNAL_LBC_POLICY_CONFIRMED=true \
  CONFIRM_AWS_WRITE=1 make portkey-helm-deploy
```

The owner must verify the full version-matched base NLB/security-group policy.
For an external role, static inspection proves exactly one trust statement
matches the expected EKS OIDC issuer, audience, and service account, but it
permits additional trust statements/principals and a permissions boundary. The
owner must review and accept each addition and the effective restrictions from
SCPs and other organization policies. The helper checks the TLS-listener
permission subset, not sole trust, complete base permissions, or effective
organization-level authorization. Keep the attestation command-scoped; never
persist it in deployment configuration, shell profiles, or CI.

The general `eksctl` requirement is 0.229.0 or newer. The workflow enforces
exactly 0.229.0 at `cluster-deploy`'s pre-write gate and when `lbc-deploy` will
create or update its walkthrough-managed controller IAM stack, whose
CloudFormation shape was validated against that release. External-controller
reuse and status or cleanup paths require only the general minimum.

Set `PORTKEY_ALLOWED_MODELS` to an explicit comma-separated list of bare Mantle
model IDs. `openai.gpt-5.5` is the default reference, not a hard-coded limit.
Set `PORTKEY_MODEL=@<provider-slug>/<model-id>` to exactly one member of that
allowlist for Codex. The helper rejects unlisted selections and never chooses a
fallback model.

Create one Portkey provider per `BEDROCK_MANTLE_REGION`. Concurrent regional
deployments use separate clusters by default. A shared existing cluster needs a
unique stack, namespace, service account, and Helm release per region plus a
pre-existing compatible load-balancer controller that watches all namespaces;
the included namespace-scoped controller cannot serve both. Redeploying one
stack with a different region replaces that stack's IAM scope; it does not add
a second region. Preflight checks provider-slug syntax, but cannot prove the
region configured behind that slug. Confirm it in Portkey Model Catalog and
with live CloudTrail `CreateInference` evidence.

A Portkey evaluation can use Codex custom-provider bearer authentication:

```toml
# PORTKEY_MODEL from the deployment environment; this example uses the default.
model = "@bedrock-mantle-validation/openai.gpt-5.5"
model_provider = "portkey"

[model_providers.portkey]
name = "Portkey Hybrid on AWS"
base_url = "https://portkey-gateway.example.com/v1"
wire_api = "responses"
env_key = "PORTKEY_API_KEY"
env_http_headers = { "x-portkey-api-key" = "PORTKEY_API_KEY" }
```

Confirm the endpoint and key scope against the customer's Portkey workspace
before distribution. Keep the key in the operating-system secret store or an
approved credential helper, not in `config.toml`.

The deployment helper accepts the same secret headers without putting their
values in command-line arguments. After configuring the regional provider and
allowlist in the ignored deployment environment, run:

```bash
make portkey-check
make portkey-validate
make portkey-codex-validate
```

When `PORTKEY_BASE_URL` is empty, these checks use a local port-forward to the
stable Service port named `gateway`. That name maps both the legacy Service
port 80 and the new Service port 443 to plaintext gateway port 8787. This is
useful for isolating gateway, provider, and model failures, but it does not
validate NLB TLS, DNS, routing, or the prefix-list boundary. Repeat the checks
through the private HTTPS base URL from an approved client before promotion.

The strict validation target probes every entry in
`PORTKEY_ALLOWED_MODELS`, requiring the exact listed model, Responses shape,
reasoning, stored-state continuation, completed SSE events, and a forced
function call. The isolated `codex exec` test then uses exactly
`PORTKEY_MODEL`, reads a fixture file through a local tool, and writes a
sentinel. An unavailable or mismatched model fails validation; neither path
silently switches models.

Portkey documents `previous_response_id` as unavailable for adapter providers,
including classic Bedrock. Bedrock Mantle instead exposes the native Responses
API and AWS documents stored continuation. The repository probe checks that
continuation is semantic, so a misconfigured classic adapter cannot produce a
false pass. See the [Portkey Quick Start](QUICKSTART_LLM_GATEWAY_PORTKEY.md).

Do not mark the Portkey route production-ready until every allowlisted model
passes the contract probe, the selected model passes the real Codex workflow,
and the following evidence is captured:

- Realized AWS and Kubernetes state shows an internal IPv4 NLB, only a TLS
  listener on port 443, the expected ACM certificate and TLS policy, no
  plaintext port-80 listener, healthy port-8787 targets, and frontend ingress
  limited to the configured customer-managed prefix list.
- The gateway is the only `LoadBalancer` Service, its
  `allocateLoadBalancerNodePorts` field is `false`, Redis is `ClusterIP`, and
  neither Service has a `nodePort`.
- The private hostname resolves through the intended resolver path, an
  approved corporate/VPN client succeeds, and an unapproved routed client is
  rejected.
- A representative long-running SSE request succeeds. NLB TLS listeners have
  a [fixed 350-second idle timeout](https://docs.aws.amazon.com/elasticloadbalancing/latest/network/update-idle-timeout.html),
  so the gateway and upstream path must emit data often enough that no idle
  interval reaches that limit; total stream duration may be longer.
- AWS CloudTrail identifies the EKS IRSA role, Mantle region, and each target
  model.
- A deliberately exceeded budget returns the expected blocking response.
- Revoking the evaluation Workspace Service API key blocks inference within
  the agreed SLA.
- Prompt, response, and trace retention match the customer's data policy.
- Regional routing and disaster-recovery behavior are documented.

For an existing TLS release with chart-default NodePorts, the helper first
proves the expected single-port, IP-target gateway Service. With explicit write
confirmation, it disables allocation and removes the existing gateway
`nodePort` in one patch while Helm changes Redis to `ClusterIP`; IP targets do
not require NLB replacement. Unexpected Service shape, target mode, lookup
failure, or a remaining NodePort fails closed.

Treat a deployment created with the earlier plaintext port-80 NLB as a
controlled migration, not an in-place listener or security-group edit. First
inventory the old NLB ARN and every PrivateLink, DNS, ALB/proxy, client, and
shared-controller dependency. Stop before replacement while any dependency is
unresolved. Coordinate PrivateLink association and approval changes with
Portkey; migrate and validate DNS before removing a customer-owned ALB or
proxy. Opening ALB or NLB ingress to `0.0.0.0/0` or `::/0` is not an acceptable
migration fix.

Approve a maintenance window and notify durable-endpoint users. The private
HTTPS endpoint is unavailable from deletion of the legacy Service until the
old NLB is removed, the controller policy is changed, the new NLB is healthy,
and DNS is repointed. The helper's Service-based port-forward is also
unavailable in that interval; direct pod forwarding would be a separate manual
diagnostic, not a provided Make target.

After resolving those stops, populate the TLS inputs and run the read-only
`make portkey-helm-plan`. If it passes, delete only the legacy gateway Service
and wait for the old NLB to be fully removed. Only then run
`CONFIRM_AWS_WRITE=1 make portkey-lbc-deploy` to switch the
walkthrough-managed controller from the legacy TCP policy to the reviewed TLS
policy, followed by `CONFIRM_AWS_WRITE=1 make portkey-helm-deploy`. Verify the
new NLB before changing private DNS. Switching the controller policy before
the old NLB is gone creates a recovery gap if recovery must recreate or modify
the legacy TCP listener.

Prefix-list contents remain mutable, so apply AWS network change control and
monitoring and rerun `make portkey-helm-plan` after edits. Restrict Kubernetes
RBAC for mutations to the gateway Service and enforce the reviewed internal
scheme, TLS, prefix-list, health-check, and backend-security-group annotations
with admission policy. For a reviewed certificate-ARN or prefix-list-ID
rotation, pass the exact Service identity only to the individual command:

```bash
CONFIRM_PORTKEY_NLB_TLS_UPDATE=portkeyai/portkey-ai-gateway \
  CONFIRM_AWS_WRITE=1 make portkey-helm-deploy
```

Adjust the identity for custom names; never persist this confirmation in the
deployment environment, a shell profile, or CI.

Before cleanup, revoke the evaluation Workspace Service API key and remove the
evaluation Model Catalog provider. Gateway cleanup targets the configured
stack, release, namespace, and service-account names. It refuses to remove an
unreadable, partial, unmanaged, or drifted gateway service-account/`eksctl`
stack pair. Resolve that state with its owner, and separately verify the
CloudFormation stack and Helm release because those names can still identify
pre-existing resources. The load-balancer-controller cleanup performs its own
ownership checks.
Even when the controller Deployment is missing, it scans load-balancer,
Ingress, and Gateway dependencies across all namespaces and probes the
deterministic `eksctl` IAM stack. An orphaned stack without the expected
managed service account requires ownership restoration or owner-assisted
manual cleanup; the command will not delete it automatically. A successful
`eksctl` deletion is followed by verification that the stack disappeared.

The retained S3 evidence bucket and customer- or vendor-owned ACM certificate,
prefix list, DNS, private connectivity, manually created proxy, and PrivateLink
resources require separate ownership decisions. Their availability is not
required to run cleanup for the resources targeted by the walkthrough.

The repository's coverage remains offline; none of these live Portkey or AWS
acceptance checks is claimed as completed by this guidance.

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
