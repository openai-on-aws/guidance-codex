# Decide

Three deployment patterns. Choose the first one that fits your organization's
priorities on the two axes that matter: *managed-vs-self-run ops* and
*soft-vs-hard governance*.

> **Decision rule:**
> - If you can run IAM Identity Center and want native per-user cost attribution
>   with no gateway, use **Native AWS Access**.
> - If you want a managed gateway with minimal ops — and **especially if you need
>   central content guardrails, multi-provider routing, or AWS-private web search**
>   (none of which Native offers) — use **AgentCore Gateway**.
> - If you need hard per-user/per-team budgets or rate limiting, use **LLM Gateway**.

## Pattern Comparison

| Capability | Native AWS Access | AgentCore Gateway | LLM Gateway |
|------------|-------------------|-------------------|-------------|
| **Authentication** | SAML → IdC | OIDC bearer → Gateway (authorizer: `CUSTOM_JWT`) | OIDC bearer → Gateway |
| **IAM Identity Center Required?** | ✅ Yes | ❌ No | ❌ No |
| **Path to Bedrock** | Codex → Bedrock (native AWS SDK) | Codex → managed gateway → Bedrock | Codex → self-run gateway → Bedrock |
| **Infra you operate** | None | None (managed/serverless) | ECS + RDS + ALB |
| **GPT-5.x via Bedrock Mantle** | ✅ Native | ✅ Built-in `bedrock-mantle` connector | ✅ Custom config |
| **Developer Command** | `aws sso login` | `export AGENTCORE_TOKEN=<oidc-jwt>` | `export OPENAI_API_KEY=...` |
| **Per-user Bedrock CloudTrail / CUR** | ✅ Native | ❌ Gateway role only | ❌ Gateway role only |
| **Soft Alerts (CloudWatch)** | Optional | ✅ `AWS/BedrockMantle` metrics | Optional |
| **Hard Budget Limits** | ❌ No | ❌ No (not built-in) | Optional |
| **Per-team Quotas** | ❌ No | ❌ No (not built-in) | Optional |
| **Rate Limiting (RPM/TPM)** | ❌ No | ⚠️ RPM throttle only | Optional |
| **Model Routing/Fallback** | ❌ No | ✅ Multi-provider (Bedrock/OpenAI/Anthropic) | ✅ Yes |
| **Content Guardrails** | ❌ No | ✅ Bedrock Guardrails + Policy | ❌ No |
| **AWS-managed web search (MCP tool)** | ❌ No | ✅ Verified ([guide](QUICKSTART_AGENTCORE_GATEWAY.md#optional-aws-managed-web-search-mcp-tool)) | ❌ No |
| **Setup Time** | 5-60 min | ~10 min (2 API calls + IAM role) | 15 min + build/harden |
| **Infra Cost** | Free (AWS control plane) | Pay-per-use (managed) | ~$100-150/mo |

> Model availability is region-bound — `gpt-5.5` (us-east-1 / us-east-2), `gpt-5.4`
> also in us-west-2; the AgentCore web search connector is us-east-1 only. See
> [reference-regions.md](reference-regions.md).

> **AgentCore Gateway auth (verified end-to-end on this repo's PoC):** create the
> gateway with a `CUSTOM_JWT` authorizer pointed at your OIDC discovery URL. Codex
> then authenticates with a plain OIDC bearer token — the **same** token the other
> patterns already issue — talking directly to the gateway with no signing proxy
> and no new credential mechanism. See
> [QUICKSTART_AGENTCORE_GATEWAY.md](QUICKSTART_AGENTCORE_GATEWAY.md).

---

## Prerequisite checklist — Native AWS Access (IAM Identity Center)

Run this path if **all** of the following are true:

- [ ] AWS Organizations is enabled (or you can enable it).
- [ ] Your IdP supports SAML 2.0 + SCIM 2.0 (EntraID, Okta, Ping, JumpCloud,
      Google Workspace, CyberArk, OneLogin).
- [ ] You can distribute AWS CLI v2 to developers (winget / MSI / Homebrew /
      MDM).
- [ ] Amazon Bedrock is activated in at least one region you plan to use
      (see [reference-regions.md](reference-regions.md) for how to verify model
      availability against AWS docs and your account).
- [ ] Per-user *attribution* in CloudTrail/CUR is sufficient — you do **not**
      require hard per-user token or cost cutoffs.

If all five apply, proceed to [Deploy — IAM Identity Center](deploy-identity-center.md) or [QUICKSTART_NATIVE_AWS_ACCESS.md](QUICKSTART_NATIVE_AWS_ACCESS.md).

## Prerequisite checklist — LLM Gateway

Run this path if IdC is not available **or** you need centralized
enforcement. All of the following must apply:

- [ ] IdC is not achievable, **or** you require one of the following: hard per-user token or cost
      budgets with automatic cutoff behind a single endpoint; reuse of an existing
      platform-team gateway.
- [ ] You have a container runtime you can operate (ECS Fargate, EKS, or
      equivalent) plus ALB and Postgres. Reference LiteLLM footprint is
      ~$90–150/mo + 0.1–0.25 FTE of ongoing ops.
- [ ] You have an OIDC IdP that can issue JWTs to developer machines (for
      client → gateway auth).
- [ ] You accept Codex running through a custom gateway provider (for example
      `model_provider = "litellm-gateway"` with a custom `base_url`),
      bypassing the native `amazon-bedrock` code path.
- [ ] Amazon Bedrock is activated in the region the gateway task role will
      call (see [reference-regions.md](reference-regions.md)).

**Reference implementation:** this repository ships LiteLLM under
`deployment/litellm/` as a working example. The pattern applies equally to
other OpenAI-compatible gateways — **Portkey**, **Bifrost**, **Kong AI Gateway**,
**Helicone**, or a custom FastAPI shim.
Choose whichever matches your organization's operational posture.

*(Canonical deploy doc: [QUICKSTART_LLM_GATEWAY.md](QUICKSTART_LLM_GATEWAY.md).)*

## Prerequisite checklist — AgentCore Gateway

Run this path if you want a **managed** gateway and can accept soft (not hard)
governance. All of the following must apply:

- [ ] You want a single managed endpoint with multi-provider routing and/or
      central Bedrock Guardrails, and you do **not** want to operate ECS/RDS/ALB.
- [ ] Soft controls are sufficient: per-user/per-team **hard budgets** are *not*
      a requirement (AgentCore inference targets do not provide them today).
- [ ] You have an OIDC IdP with a reachable discovery URL that can issue JWTs to
      developer machines (Cognito, Okta, Entra ID, Auth0). The gateway uses a
      `CUSTOM_JWT` authorizer; Codex sends the OIDC bearer directly — no proxy.
- [ ] Your AWS CLI / SDK is recent enough to expose AgentCore *inference* targets
      (botocore/boto3 ≥ 1.43.33; older SDKs only show mcp/http targets).
- [ ] Amazon Bedrock Mantle access for GPT-5.x in the target region
      (us-east-1 / us-east-2; `gpt-5.5` is not in us-west-2 — see
      [reference-regions.md](reference-regions.md)).

**Reference implementation:** CloudFormation templates in
`deployment/infrastructure/` — `agentcore-websearch.yaml` (fully CFN-native) and
`agentcore-inference.yaml` (gateway + role in CFN; the inference target is added by
one documented CLI call, as CloudFormation does not yet support inference targets).
Canonical deploy doc: [QUICKSTART_AGENTCORE_GATEWAY.md](QUICKSTART_AGENTCORE_GATEWAY.md).

---

## Why this order

**Native AWS Access (IdC) is recommended first because:**

1. **Enterprise audiences need centralized cost and usage attribution with
   scalable distribution.** That eliminates the static Bedrock API key as a
   ranked option — Bedrock's own documentation describes it as a pilot/POC
   mechanism, not an enterprise path.
2. **IdC delivers all three from a single identity plane.** SSO user name in
   CloudTrail → CUR attribution; the same identity stamped into OTel as
   `user.id` → CloudWatch dashboards; signed AWS CLI v2 distribution →
   no SmartScreen or Gatekeeper friction.
3. **Native Codex integration.** Codex natively speaks SigV4 to Bedrock via the AWS SDK credential chain.

**LLM Gateway provides additional value for:**

1. **Hard enforcement.** The gateway retains real value for *enforcement* (hard per-user budgets, rate limiting, central policy).
2. **Organizations without IdC.** Gateway with OIDC is faster to set up than IdC + SAML federation.

**Trade-offs:**

- Pointing Codex at a gateway requires a custom provider definition with a custom
  `base_url`, bypassing the native `amazon-bedrock` code path.
- Gateway adds operational overhead (~$100-150/mo + 0.1-0.25 FTE).
- Bedrock CloudTrail and CUR no longer identify end users on the gateway path;
  use gateway-native telemetry and spend logs for per-user reporting.

## Open questions that may shift the pick

- **Session duration vs. long Codex runs.** The 8-hour default IdC session can
  interrupt multi-hour agent runs. Raise the permission-set session duration or
  accept `aws sso login` re-authentication as expected UX.
- **GovCloud parity.** Whether IdC-in-GovCloud meets the FedRAMP alignment
  some customers require is not yet confirmed.
