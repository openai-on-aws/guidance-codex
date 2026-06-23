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
the gateway directly, with no signing proxy and no client binary.** The bearer is
a standard OIDC token from your IdP (Cognito / Okta / Entra ID / Auth0).

> **Who authenticates whom.** On this path **your IdP issues the token; Codex
> presents it.** You choose how Codex obtains that token, and the choice decides
> whether refresh is automatic (verified against the Codex source and live):
>
> - **`env_key` (manual).** Codex reads a static token from an env var and forwards
>   it. It does **not** refresh — on expiry the gateway returns 401 and you re-mint
>   and re-run. Simplest to start with.
> - **`auth` command (auto-refresh, recommended).** Point the provider at a small
>   token-fetch command; Codex runs it itself, caches the token for
>   `refresh_interval_ms`, and **re-runs it to refresh** — no manual step, no 401
>   loop. Verified live: with no token in the environment, Codex invoked the command
>   and authenticated. See [Daily use](#daily-use).
>
> (The native `amazon-bedrock` provider is different again — Codex authenticates via
> the AWS credential chain / SigV4.)

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

Codex authenticates to the gateway with an **OIDC bearer token from your IdP** —
the developer needs **no AWS account, no IAM user, and no AWS credentials.** Their
corporate identity (EntraID / Okta / Auth0 / Cognito) is the only thing AWS trusts,
via the gateway's `CUSTOM_JWT` authorizer. See
[Obtaining & refreshing your token](#obtaining--refreshing-your-token) below for the
full per-IdP flow and how refresh works.

Quick version — fetch a token into `AGENTCORE_TOKEN` (Cognito M2M shown):

```bash
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

## Daily use

Pick one of two token strategies. **Both are verified end-to-end against a live
gateway.**

### Option A — `auth` command (recommended: Codex auto-refreshes)

Point the provider at a small command that prints a fresh OIDC token to stdout.
Codex runs it itself, caches the token for `refresh_interval_ms`, and re-runs it to
refresh — so the everyday loop is just `codex`, with **no token export and no 401
loop.**

```toml
[model_providers.agentcore-gateway]
name     = "AgentCore Gateway (bedrock-mantle)"
base_url = "https://<gateway-id>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/inference/v1"
wire_api = "responses"

[model_providers.agentcore-gateway.auth]
command = "/path/to/fetch-token.sh"   # prints the access_token to stdout
refresh_interval_ms = 300000          # Codex re-runs the command when this elapses
```

`fetch-token.sh` is any script that emits the raw `access_token` (the per-IdP
`curl` from [Obtaining & refreshing your token](#obtaining--refreshing-your-token),
piped to extract `access_token`). Then daily use is simply:

```bash
codex exec "..."   # Codex invokes fetch-token.sh on its own and refreshes on interval
```

> Verified live: with **no** token in the environment, Codex invoked the command and
> authenticated to the gateway. `auth` and `env_key` are mutually exclusive — use one.
> (Note: this `auth`-command auto-refresh is a **model-provider** feature, i.e. the
> inference path. For the web-search **MCP** path, use `[mcp_servers.<name>.oauth]` +
> `codex mcp login`, which performs OAuth with automatic refresh-token renewal.)

### Option B — `env_key` (manual)

Simplest to start with; **Codex does not refresh** a static env token.

```bash
export AGENTCORE_TOKEN=$(...)   # mint/refresh from your IdP; re-mint when it expires
codex exec "..."
```

- On expiry the gateway returns **401**; re-mint into `AGENTCORE_TOKEN` and re-run.
- For human developers the refresh-token grant renews silently (no browser re-login
  until the refresh token lapses) — see
  [Obtaining & refreshing your token](#obtaining--refreshing-your-token).

### Both options

- **No AWS credentials involved** — unlike the Native path there is no `aws sso login`
  and no AWS profile; the IdP token is the only credential and never touches AWS IAM.

---

## Obtaining & refreshing your token

The gateway accepts a standard **OIDC bearer token (JWT)** issued by your IdP.
Developers never hold AWS credentials — the token *is* their credential, and it is
short-lived, so the practical question is **how to obtain it and refresh it when it
expires.** Pick the flow that matches who is calling.

### Which flow?

| Caller | OAuth grant | Identity in the token |
|---|---|---|
| **A human developer** (interactive) | Authorization Code + PKCE | the user (`sub`/`email`) — enables per-user attribution |
| **Automation / CI / a shared service** | Client Credentials (M2M) | the client app (`client_id`) — no per-user identity |

Set the gateway's `allowedClients` (or `allowedAudience`) to match the client/app
you use here. Cognito **machine-to-machine** tokens carry `client_id` and no `aud`,
so match on `allowedClients`.

### Per-IdP token endpoints

All four issue OIDC JWTs from a standard token endpoint. Examples (client-credentials
shown; swap `grant_type` for the interactive flow — see below):

```bash
# Amazon Cognito
curl -s -X POST "https://<domain>.auth.<region>.amazoncognito.com/oauth2/token" \
  -d "grant_type=client_credentials&client_id=<ID>&client_secret=<SECRET>&scope=<resource-server>/<scope>"

# Okta
curl -s -X POST "https://<tenant>.okta.com/oauth2/v1/token" \
  -d "grant_type=client_credentials&client_id=<ID>&client_secret=<SECRET>&scope=<custom-scope>"

# Microsoft Entra ID
curl -s -X POST "https://login.microsoftonline.com/<tenant-id>/oauth2/v2.0/token" \
  -d "grant_type=client_credentials&client_id=<ID>&client_secret=<SECRET>&scope=<app-id-uri>/.default"

# Auth0
curl -s -X POST "https://<tenant>.auth0.com/oauth/token" \
  -H "Content-Type: application/json" \
  -d '{"grant_type":"client_credentials","client_id":"<ID>","client_secret":"<SECRET>","audience":"<api-identifier>"}'
```

Each returns JSON with `access_token` (and, for interactive flows, a `refresh_token`).
Extract **`access_token`** into `AGENTCORE_TOKEN` as shown in Step 3.

> **Use `access_token`, not `id_token`.** Verified against a live gateway: a Cognito
> `id_token` is rejected with **HTTP 403 `insufficient_scope`** — the gateway's
> `CUSTOM_JWT` authorizer validates the OAuth **access token** (and its scope), not
> the OIDC identity token. If you see 403 with a valid-looking login, you almost
> certainly sent the `id_token`.

### Refreshing — there is no AWS credential to manage

The token has an IdP-controlled TTL (commonly 1 hour). When it expires the gateway
returns **401**; the fix is to mint a new token — there is nothing in AWS to refresh
or rotate, because the developer has no AWS identity. Two patterns:

- **Interactive (human) developers** — use the Authorization Code + PKCE flow once
  (browser sign-in against EntraID/Okta, MFA included), then use the returned
  **`refresh_token`** to obtain new access tokens silently until the refresh token
  itself expires:
  ```bash
  export AGENTCORE_TOKEN=$(curl -s -X POST "<idp-token-endpoint>" \
    -d "grant_type=refresh_token&client_id=<ID>&refresh_token=<REFRESH_TOKEN>" \
    | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
  ```
  Wrap this in a shell function / login script so a new token is fetched at the
  start of a session (or on a 401). No browser re-auth until the refresh token
  lapses.

- **Automation / M2M** — re-run the client-credentials `curl` (no refresh token
  exists for that grant; you simply request a fresh one). Cheap and stateless — do
  it on a timer or before each batch.

> **Relationship to the Native AWS Access path:** there, `aws sso login` plays the
> same role — it re-authenticates against the same corporate IdP (EntraID/Okta via
> IAM Identity Center) and caches fresh **temporary AWS credentials**. The gateway
> path skips AWS entirely: the IdP token goes straight to the gateway. Either way,
> **refresh = re-authenticate against your corporate IdP**, never an AWS IAM user.

> **Optional credential helper:** the repo's optional
> [`aws-oidc-auth`](../README.md#optional-helper-escape-hatch) implements the
> browser OIDC (PKCE) → token flow as a reusable helper for orgs that want a
> packaged refresh loop instead of scripting the `curl` above. It is **not
> required** for this pattern.

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

Gateway invocation/latency/error metrics (separate from the Mantle usage metrics
above) land in `AWS/Bedrock-AgentCore` after a one-time **CloudWatch Transaction
Search** enablement. Both namespaces are recorded server-side, with no collector
to run. They do not include Codex's own per-turn / per-user client OTEL; for that
(and how to wire it on this path when developers hold no AWS credentials), see
[operate-monitoring.md — AgentCore Gateway path](operate-monitoring.md#agentcore-gateway-path).

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

> Codex consumes AgentCore Web Search as a **Gateway MCP target**: it discovers the
> `web-search-tool___WebSearch` tool through a standard `tools/list` call and invokes
> it like any MCP tool, grounding answers in Amazon's web index with cited sources.

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
and refresh it the same way (see
[Obtaining & refreshing your token](#obtaining--refreshing-your-token)). Web search
**augments whatever model Codex already uses** — it does not require the inference
gateway provider. The verified config drives it with the native `amazon-bedrock`
provider:

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
