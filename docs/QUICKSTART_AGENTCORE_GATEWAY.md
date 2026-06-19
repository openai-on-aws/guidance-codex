# Quick Start: AgentCore Gateway (managed) for Codex on Bedrock

> **Status:** Reference Implementation
> **Audience:** Organizations that want a managed LLM gateway (no ECS/RDS/ALB to run)
> **Production Readiness:** Requires an OIDC IdP; see [Limitations](#limitations)

Amazon Bedrock **AgentCore Gateway** is a fully managed AI gateway. With an
*inference target* it behaves as an OpenAI-compatible LLM proxy: a single
endpoint that routes to model providers by the `model` field in the request.
This is the managed counterpart to the self-hosted
[LiteLLM gateway](QUICKSTART_LLM_GATEWAY_LITELLM.md) — same gateway *shape*, but
AWS runs the infrastructure and `bedrock-mantle` (GPT-5.x via the Responses API)
is a built-in connector.

**What you get:**
- A managed, serverless endpoint — no containers, database, or load balancer to operate
- Built-in `bedrock-mantle`, `openai`, and `anthropic` connectors with model-based routing
- Outbound SigV4 to Bedrock handled by a gateway IAM role
- Usage telemetry in CloudWatch (`AWS/BedrockMantle`)

**What you do *not* get** (vs. the LiteLLM pattern): hard per-user/per-team
budgets, per-user cost attribution, or per-tenant TPM enforcement. See
[Limitations](#limitations).

---

## Auth model: CUSTOM_JWT (no proxy)

The gateway is created with a **`CUSTOM_JWT`** authorizer pointed at your OIDC
IdP's discovery URL. Codex's custom providers authenticate with a plain bearer
token, and the gateway validates that token as an OIDC JWT — so **Codex talks to
the gateway directly, with no signing proxy and no new credential mechanism.**
The bearer is the same kind of OIDC token the repo's other patterns already
issue (e.g. via `aws-oidc-auth`).

This was verified end-to-end: a real `codex exec` turn reached the gateway with
only `Authorization: Bearer <jwt>`, streamed a response, and emitted telemetry.

> An `AWS_IAM` authorizer is also technically possible, but Codex cannot produce
> the SigV4 signature it requires for a custom provider — it would need a local
> signing shim. This guide does not use or ship that path.

---

## Prerequisites

- AWS account with permissions for `bedrock-agentcore`, IAM, and Bedrock, with AWS
  credentials available in your shell.
- Amazon Bedrock **Mantle** access for GPT-5.x in the target region
  (`us-east-1` / `us-east-2`; `gpt-5.5` is **not** in `us-west-2` — see
  [reference-regions.md](reference-regions.md))
- AWS CLI v2 authenticated, **and** botocore/boto3 ≥ 1.43.33 — the AgentCore
  *inference target* shape was added in that release; older SDKs only expose
  `mcp`/`http` targets. Check with:
  ```bash
  python3 -c "import boto3,botocore;print(botocore.__version__)"
  ```
- [Codex CLI](https://developers.openai.com/codex/cli) installed
- **An OIDC IdP you control** (Amazon Cognito, Okta, Entra ID, or Auth0). The
  gateway uses a `CUSTOM_JWT` authorizer, so Codex authenticates with a plain OIDC
  bearer token — see [Set up your OIDC IdP](#step-1-set-up-your-oidc-idp).

---

## Step 1: Set up your OIDC IdP

The gateway validates an inbound OIDC JWT, so you need an IdP that issues tokens to
your developers (or, for automation, a machine-to-machine client). **This repo does
not script IdP creation — follow your provider's own documentation:**

- **Amazon Cognito (recommended for a quick start):** create a user pool, a domain,
  a resource server with a custom scope, and an app client. For headless/automation
  use, enable the **client credentials** grant.
  - User pool + app client with client credentials:
    <https://docs.aws.amazon.com/cognito/latest/developerguide/user-pool-settings-client-credentials.html>
  - Token endpoint:
    <https://docs.aws.amazon.com/cognito/latest/developerguide/token-endpoint.html>
- **Okta / Entra ID / Auth0:** use that provider's OIDC app + client-credentials (or
  authorization-code) flow.

From your IdP you need two values for Step 2:

| Value | Cognito example |
|---|---|
| **Discovery URL** | `https://cognito-idp.us-east-1.amazonaws.com/<POOL_ID>/.well-known/openid-configuration` |
| **Allowed client id** | your app client id (Cognito M2M access tokens carry `client_id`, not `aud`) |

---

## Step 2: Deploy the gateway (CloudFormation)

```bash
git clone https://github.com/openai-on-aws/guidance-codex.git
cd guidance-codex

aws cloudformation deploy \
  --region us-east-1 \
  --stack-name codex-agentcore-inference \
  --template-file deployment/infrastructure/agentcore-inference.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    DiscoveryUrl="https://cognito-idp.us-east-1.amazonaws.com/<POOL_ID>/.well-known/openid-configuration" \
    AllowedClient="<OIDC_CLIENT_ID>"
```

The stack creates the IAM service role (scoped `bedrock-mantle:*` incl.
`ListModels`, plus `bedrock:*`) and the `CUSTOM_JWT` gateway.

> **One manual step for the inference target.** CloudFormation cannot yet express
> an *inference* target — the `AWS::BedrockAgentCore::GatewayTarget` schema today
> exposes only `Mcp`/`Http` target types, not `Inference`. The stack therefore
> outputs a single ready-to-run CLI command (`AddInferenceTargetCommand`) that adds
> the `bedrock-mantle` target. Run it once after deploy:
> ```bash
> aws cloudformation describe-stacks --region us-east-1 \
>   --stack-name codex-agentcore-inference \
>   --query "Stacks[0].Outputs[?OutputKey=='AddInferenceTargetCommand'].OutputValue" --output text | bash
> ```
> (The web-search target *is* fully CloudFormation-native — see
> [the web-search section](#optional-aws-managed-web-search-mcp-tool).)

Read the stack outputs for `InferenceBaseUrl`:
```bash
aws cloudformation describe-stacks --region us-east-1 \
  --stack-name codex-agentcore-inference \
  --query "Stacks[0].Outputs" --output table
```

---

## Step 3: Get a token and point Codex at it

The deploy script's output gives you both, filled in for your gateway. The shape:

```bash
# fetch a bearer token from your IdP (Cognito client-credentials shown)
export AGENTCORE_TOKEN=$(curl -s -X POST \
  "https://<your-cognito-domain>.auth.us-east-1.amazoncognito.com/oauth2/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=client_credentials&client_id=<CLIENT_ID>&client_secret=<CLIENT_SECRET>&scope=<RESOURCE_SERVER>/<SCOPE>" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
```

Then, in `~/.codex/config.toml`:

```toml
model = "gpt-5.5"
model_provider = "agentcore-gateway"

[model_providers.agentcore-gateway]
name     = "AgentCore Gateway (bedrock-mantle)"
base_url = "https://<gateway-id>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/inference/v1"
env_key  = "AGENTCORE_TOKEN"   # env var holding the OIDC bearer fetched above
wire_api = "responses"
```

```bash
codex exec "What is 17 multiplied by 23?"
```

---

## Verify telemetry

GPT-5.x calls routed through the gateway land in CloudWatch namespace
**`AWS/BedrockMantle`**, keyed by `Model=openai.gpt-5.5` and `Project=default`:

```bash
aws cloudwatch get-metric-statistics --region us-east-1 \
  --namespace AWS/BedrockMantle --metric-name Inferences \
  --dimensions Name=Model,Value=openai.gpt-5.5 Name=Project,Value=default \
  --start-time "$(date -u -v-1H +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 3600 --statistics Sum
```

Available metrics include `Inferences`, `InputTokens`, `OutputTokens`,
`TotalInputTokens`, `TotalOutputTokens`, and `InferenceClientErrors`.

---

## Optional: AWS-managed web search (MCP tool)

The same gateway model can also expose Amazon Bedrock AgentCore **Web Search** — an
Amazon-operated web index, **queries never leave AWS** — as an MCP tool that Codex
calls directly. This is a genuine differentiator: neither the Native nor LiteLLM
pattern offers it. It is a *separate capability* from the inference target above:
it lives on the gateway's `/mcp` endpoint (not `/inference`) and is consumed by
Codex's MCP client. The two can share one gateway or run on separate gateways.

**Verified end-to-end** (2026-06-19): a real `codex exec` turn called the tool and
returned a cited result — confirmed by `mcp_tool_call` events in `--json` output,
with no shell fallback:

```
mcp_tool_call | web-search-tool___WebSearch
agent_message | Amazon Bedrock AgentCore - AWS
                https://aws.amazon.com/bedrock/agentcore/
```

> Codex's hosted `web_search` tool type is **not** an alternative on this stack —
> Bedrock Mantle rejects it. A Gateway MCP target is the only path to AgentCore's
> web search.

### 1. Deploy a web-search gateway (CloudFormation)

Same [IdP setup (Step 1)](#step-1-set-up-your-oidc-idp). This path is **fully
CloudFormation-native** — role, gateway, and the web-search MCP target are all in
one stack:

```bash
aws cloudformation deploy \
  --region us-east-1 \
  --stack-name codex-agentcore-websearch \
  --template-file deployment/infrastructure/agentcore-websearch.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    DiscoveryUrl="https://cognito-idp.us-east-1.amazonaws.com/<POOL_ID>/.well-known/openid-configuration" \
    AllowedClient="<OIDC_CLIENT_ID>"

# read the MCP endpoint for your Codex config:
aws cloudformation describe-stacks --region us-east-1 \
  --stack-name codex-agentcore-websearch \
  --query "Stacks[0].Outputs[?OutputKey=='McpEndpoint'].OutputValue" --output text
```

The stack creates the role (with `bedrock-agentcore:InvokeGateway` +
`InvokeWebSearch`), the `CUSTOM_JWT` gateway, and the `web-search` MCP target.

> **Gotcha (handled by the template):** the role needs `InvokeWebSearch` on the
> service-owned ARN `arn:aws:bedrock-agentcore:<region>:aws:tool/web-search.v1`.
> Without it, `tools/list` succeeds but `tools/call` fails with *"Execution role is
> not authorized for connector web-search."*

To add a server-side domain denylist (hidden from the model), set the target's
`ParameterValues` in the template: `{ domainFilter: { exclude: ["blocked.com"] } }`.

### 2. Get a token and wire it into Codex

Fetch the bearer token exactly as in [Step 3](#step-3-get-a-token-and-point-codex-at-it)
(`export AGENTCORE_TOKEN=...`). Web search **augments whatever model Codex already
uses** — it does not require the inference gateway provider. The verified config
drives it with the native `amazon-bedrock` provider:

```toml
# --- model provider: needs AWS credentials in your env for SigV4 ---
model = "openai.gpt-5.5"
model_provider = "amazon-bedrock"

[model_providers.amazon-bedrock.aws]
region = "us-east-1"
wire_api = "responses"

# --- web search MCP tool ---
approval_policy = "never"
sandbox_mode = "workspace-write"

[sandbox_workspace_write]
network_access = true                      # REQUIRED: the MCP tool needs egress

[mcp_servers.agentcore_websearch]
url = "https://<gateway-id>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
bearer_token_env_var = "AGENTCORE_TOKEN"   # the OIDC bearer from Step 3
startup_timeout_sec = 60
default_tools_approval_mode = "approve"    # REQUIRED for non-interactive use
```

```bash
codex exec "Use the agentcore_websearch tool to find <something current>. Cite the URL."
```

**Two Codex client settings are mandatory** (verified against Codex source —
`codex-rs/core/src/mcp_tool_call.rs`, `codex-rs/codex-mcp/src/mcp/mod.rs`); both
apply to any remote MCP tool:

1. **`network_access = true`** — the default sandbox blocks the MCP tool's egress.
2. **`default_tools_approval_mode = "approve"`** — the WebSearch tool advertises no
   `read_only_hint`, so Codex defaults to "approval required"; in non-interactive
   `codex exec` an un-approvable call is auto-**cancelled** (`user cancelled MCP
   tool call`). This auto-approves the server's tools.

> The `amazon-bedrock` provider needs AWS credentials in your environment for SigV4
> — that's **separate** from the `AGENTCORE_TOKEN` bearer the gateway requires.

**Notes:** web search connector is `us-east-1`-only at time of writing; you must
retain/display the source citations returned with each result (per AWS terms).

---

## Limitations

- **No hard budgets / no per-user cost attribution.** All traffic shares the
  single gateway IAM role, so Bedrock CloudTrail/CUR see the gateway, not the
  end user. If you need hard per-user/per-team budgets or billing-grade per-user
  spend, use the [LLM Gateway pattern](QUICKSTART_LLM_GATEWAY.md) (hard
  enforcement; LiteLLM reference impl) or
  [Native AWS Access](QUICKSTART_NATIVE_AWS_ACCESS.md) (native per-user
  attribution).
- **RPM throttling only**, not per-request cost control; users on a target share
  provider credentials.
- **SDK floor:** inference targets require botocore/boto3 ≥ 1.43.33.

---

## Teardown

Delete the stack — CloudFormation removes the target, gateway, and role in the
right order:

```bash
aws cloudformation delete-stack --region us-east-1 --stack-name codex-agentcore-websearch
aws cloudformation wait stack-delete-complete --region us-east-1 --stack-name codex-agentcore-websearch
```

> **Inference path only:** because the `bedrock-mantle` target was added by the
> post-deploy CLI command (CFN can't yet manage inference targets), delete that
> target *before* deleting the inference stack, or the gateway delete will report
> "has targets associated":
> ```bash
> REGION=us-east-1; GW=$(aws cloudformation describe-stacks --region $REGION \
>   --stack-name codex-agentcore-inference \
>   --query "Stacks[0].Outputs[?OutputKey=='GatewayId'].OutputValue" --output text)
> for T in $(aws bedrock-agentcore-control list-gateway-targets --region $REGION \
>   --gateway-identifier $GW --query 'items[].targetId' --output text); do
>   aws bedrock-agentcore-control delete-gateway-target --region $REGION --gateway-identifier $GW --target-id $T
> done
> aws cloudformation delete-stack --region $REGION --stack-name codex-agentcore-inference
> ```

Also delete your throwaway Cognito pool/domain if you created one.

---

## Gotchas (from live E2E testing)

1. **`bedrock-mantle:ListModels` is required** on the service role — the connector
   discovers its model list at target creation. Without it the target goes
   `FAILED` with HTTP 401.
2. **No `aws:RequestedRegion` condition** on the Mantle statement — those calls
   don't populate that key, so the condition causes an implicit deny.
