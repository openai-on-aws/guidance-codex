# Guidance for Codex on AWS

Reference access patterns for connecting [OpenAI Codex](https://developers.openai.com/codex/overview) to models on [Amazon Bedrock](https://aws.amazon.com/bedrock/), with corporate SSO, optional quota enforcement, and observability.

---

## Choose a Rollout Path

```text
Need centralized routing, hard budgets, or gateway policy?
|
|-- NO  -> Native AWS Access with IAM Identity Center
|
`-- YES -> Which gateway operating model fits?
           |-- Operate it in your AWS account     -> LiteLLM on ECS
           `-- Portkey-managed control plane,
               your AWS data plane                -> Portkey Hybrid on EKS
```

| Path | Operations | Identity evidence | Best for |
|---|---|---|---|
| **[IAM Identity Center](docs/QUICKSTART_NATIVE_AWS_ACCESS.md)** | Lowest | Native AWS session and CloudTrail identity | Existing AWS SSO and direct Bedrock access |
| **[LiteLLM on ECS](docs/QUICKSTART_LLM_GATEWAY_LITELLM.md)** | Customer-operated stack | Scoped gateway key; optional OIDC mapping | Inspectable AWS stack and hard controls |
| **[Portkey](docs/QUICKSTART_LLM_GATEWAY_PORTKEY.md)** | Customer-operated EKS data plane; Portkey-managed control plane | Workspace Service API key and gateway logs | Integrated Portkey policy and analytics on a private AWS data plane |

The repository also retains an
[AgentCore Gateway](docs/QUICKSTART_AGENTCORE_GATEWAY.md) pattern for customers
who specifically need AWS-managed routing, Bedrock Guardrails, or AWS-private
web search.

## Start Here

- **Overview and decision guide** → [QUICKSTART.md](QUICKSTART.md)
- **Native AWS Access** → [Quickstart](docs/QUICKSTART_NATIVE_AWS_ACCESS.md)
- **LiteLLM on ECS** → [Deployable reference](docs/QUICKSTART_LLM_GATEWAY_LITELLM.md)
- **Portkey Hybrid** → [Hybrid AWS deployment](docs/QUICKSTART_LLM_GATEWAY_PORTKEY.md)
- **AgentCore Gateway** → [Specialized AWS-managed pattern](docs/QUICKSTART_AGENTCORE_GATEWAY.md)
- **Gateway requirements** → [Pattern requirements](docs/QUICKSTART_LLM_GATEWAY.md)

## Featured Reference: LiteLLM on ECS

This repository documents multiple rollout paths. The remainder of this section
highlights LiteLLM on ECS because it is the most complete deployable reference
implementation. Use the links above for the Native AWS, Portkey, and AgentCore
patterns.

### How Requests Flow

LiteLLM does not deploy or run Codex. Codex stays on the developer machine.
For each turn, Codex sends a Responses request containing task context and tool
definitions. LiteLLM authenticates the developer, applies model and budget
policy, obtains upstream AWS credentials, and forwards the request to Bedrock
Runtime. If the model requests a tool, Codex runs it locally and sends the
result through LiteLLM in the next turn. That loop continues until the model
returns a final response.

```text
Codex -> /v1/responses -> LiteLLM policy -> Bedrock Runtime
  ^                                               |
  `------------ local tool result <---------------'
```

The value is the control point: developers retain the normal Codex experience,
while the platform team gets centralized identity, model access, rate limits,
budgets, and gateway telemetry.

![Codex request flow through LiteLLM on AWS](docs/assets/litellm-architecture.png)

## Also Included: Portkey Hybrid

Portkey Hybrid keeps the gateway data plane in your AWS account while Portkey
manages the control plane. Codex reaches the EKS gateway through private
DNS/VPN and an internal NLB; prompt and response inference traffic stays on
that private path. See the [Portkey quickstart](docs/QUICKSTART_LLM_GATEWAY_PORTKEY.md)
for the full deployment.

![Codex request flow through Portkey Hybrid on AWS](docs/assets/portkey-architecture.png)

## Client tooling — Codex-native by design

Authentication and telemetry both use features built into Codex.

### Authentication

- **Native AWS Access.** Codex's `amazon-bedrock` provider signs requests with AWS
  SigV4 from the standard credential chain. Developers sign in with `aws sso login`
  (IAM Identity Center).
- **AgentCore Gateway.** Its `CUSTOM_JWT` authorizer validates an OIDC bearer
  token from your IdP. Codex can refresh the token through a provider `auth`
  command. See [daily use](docs/QUICKSTART_AGENTCORE_GATEWAY.md#daily-use).
- **LiteLLM Gateway.** Codex retrieves a scoped LiteLLM key from Secrets Manager;
  the optional middleware adds OIDC self-service key mapping. See
  [Codex configuration](docs/QUICKSTART_LLM_GATEWAY_LITELLM.md#codex-configuration).
- **Portkey Gateway.** Codex reads a Workspace Service API key from
  `PORTKEY_API_KEY` and sends it as bearer authorization and
  `x-portkey-api-key`. See [Configure Codex](docs/QUICKSTART_LLM_GATEWAY_PORTKEY.md#7-configure-codex).

### Telemetry

Codex emits OpenTelemetry natively through its `[otel]` config; you point it at a
local collector — the sidecar — that adds the attribution the dashboards run on
(see [operate-monitoring.md](docs/operate-monitoring.md)):

- **Per-user identity** — the sidecar adds `user.id` and `user.email`.
- **Organizational grouping** — it can also add `user.name`, `department`,
  `team.id`, `cost_center`, `organization`, `location`, `role`, and `manager`, so you
  can slice spend and usage by team, department, or cost center.

These are **resource** attributes — the shape the dashboards expect
(`@resource.team.id`, …) and the same keys the collector-free
[bearer-token path](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/coding-agents-codex-bearer-token.html)
sets via `OTEL_RESOURCE_ATTRIBUTES`. One dashboard works for both the sidecar and the
bearer-token path.

Metric attribution comes from the sidecar, because Codex's `otel.span_attributes`
apply only to traces, not metrics. (You can instead have Codex forward static headers
via `[otel.*].headers` and have the collector lift them into attributes with
`from_context`, but the sidecar is simpler.)

Metric export is gated behind `analytics.enabled`, which `codex exec` and the TUI
default to `true`, so metrics flow out of the box.

**Gateway path caveat:** On the LiteLLM gateway, metric attribution covers the
identity fields LiteLLM puts on metric datapoints: `user.email`, `user.id`,
`team.id`, `organization`, and `model`. The other org fields travel on spans and
logs, so attribute gateway metrics by team or org and join the rest downstream
(CUR / Athena). Full per-attribute metric parity is a local-sidecar capability.

> **SigV4 caveat:** CloudWatch's native OTLP endpoint requires SigV4-signed requests,
> which Codex does not sign directly. Any path that ships Codex's client OTEL to
> CloudWatch runs a standard
> [AWS Distro for OpenTelemetry (ADOT) Collector](https://aws-otel.github.io/) — the
> upstream AWS collector — to sign and forward.
>
> **Two telemetry sources:**
> - **Server-side metrics.** With AgentCore Gateway, AWS records usage telemetry
>   without a collector: GPT-5.x token usage in `AWS/BedrockMantle` (emitted by
>   Bedrock Mantle, the inference layer) and gateway invocation / latency / error
>   metrics in `AWS/Bedrock-AgentCore` (emitted by AgentCore Gateway observability).
> - **Client OTEL (Codex's `[otel]`).** The per-turn / per-tool / per-user signals
>   come from Codex itself and require the ADOT collector above on every pattern,
>   AgentCore included. See [operate-monitoring.md](docs/operate-monitoring.md) for
>   how to wire client OTEL.

### Optional helpers

| Helper / guidance | When it helps |
|-------------------|---------------|
| [aws-oidc-auth/](https://github.com/aws-samples/sample-openai-on-aws/tree/main/aws-oidc-auth) | A `credential_process` helper for organizations that federate a raw OIDC IdP (Okta / Entra ID / Auth0 / Cognito) to AWS without IAM Identity Center. If you use IdC (`aws sso login`) or a gateway with OIDC bearer auth, the default paths already cover you. See [AUTH_HELPER.md](https://github.com/aws-samples/sample-openai-on-aws/blob/main/AUTH_HELPER.md). |
| [deployment/scripts/codex-sso-creds*](deployment/scripts/) | For the Native AWS Access path: a `credential_process` helper script (bash + PowerShell) that makes IAM Identity Center login seamless — it auto-triggers `aws sso login` when the token expires, so the daily loop is just `codex`. Supports macOS, Linux, and Windows, including headless device-code hosts. See [credential-helper-auto-login.md](docs/credential-helper-auto-login.md). |

## Documentation

- [Architecture and pattern comparison](docs/01-decide.md)
- [Enterprise gateway evaluation](docs/ENTERPRISE_GATEWAY_GUIDANCE.md)
- [Production deployment gates](docs/PRODUCTION_DEPLOYMENT.md)
- [Monitoring and operations](docs/operate-monitoring.md)
- [Troubleshooting](docs/operate-troubleshooting.md)
- [CHANGELOG](CHANGELOG.md)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## License

This repository is dual-licensed:

- **Code** (`.py`, `.js`, `.ts`, `.go`, configuration files, and other source) is licensed under the [MIT No Attribution (MIT-0)](LICENSE) license.
- **Documentation, media, and text content** (`.md` documentation, images, and diagrams) is licensed under the [Creative Commons Attribution-ShareAlike 4.0 International (CC-BY-SA 4.0)](LICENSE-DOCS.md) license.
