# Guidance for Codex on AWS

Production-ready deployment patterns for running [OpenAI Codex](https://developers.openai.com/codex/overview) at enterprise scale on [Amazon Bedrock](https://aws.amazon.com/bedrock/) — with corporate SSO, optional quota enforcement, and observability built in.

---

## Three Deployment Patterns

```text
Need hard quota enforcement? (Block requests when limits hit)
│
├── YES → LLM Gateway
│
└── NO → Want a fully managed gateway (multi-provider routing, content
         guardrails, AWS-private web search) with no infra to run?
          │
          ├── YES → AgentCore Gateway
          │
          └── NO → Already use AWS IAM Identity Center?
                    │
                    ├── YES → Native AWS Access
                    │
                    └── NO → Native AWS Access (set up IdC) OR AgentCore Gateway
```

| Pattern | Setup Time | Telemetry | Best For |
|---------|------------|-----------|----------|
| **[Native AWS Access](docs/QUICKSTART_NATIVE_AWS_ACCESS.md)** | 5–60 min | Optional Codex-side OTel | Teams with IdC, native per-user attribution, soft monitoring OK |
| **[AgentCore Gateway](docs/QUICKSTART_AGENTCORE_GATEWAY.md)** | ~10 min | CloudWatch `AWS/BedrockMantle` | Managed gateway, guardrails, AWS-private web search, minimal ops |
| **[LLM Gateway](docs/QUICKSTART_LLM_GATEWAY.md)** | 15 min | Provided by the gateway | Hard budgets, rate limiting, per-user spend |

All patterns include:
- Corporate SSO (Okta, Azure AD, Auth0, AWS IAM Identity Center)
- Per-user CloudTrail audit trails (Native AWS Access; gateway patterns attribute via gateway telemetry)
- One-command authentication
- Cross-platform support (Windows, macOS, Linux)
- CloudFormation templates for one-command infrastructure deployment

## Quick Start

- **Overview & decision guide** → [QUICKSTART.md](QUICKSTART.md)
- **Native AWS Access** → [Quickstart](docs/QUICKSTART_NATIVE_AWS_ACCESS.md)
- **AgentCore Gateway** → [Quickstart](docs/QUICKSTART_AGENTCORE_GATEWAY.md)
- **LLM Gateway** → [Quickstart](docs/QUICKSTART_LLM_GATEWAY.md)

## Documentation

- [Architecture & pattern comparison](docs/01-decide.md)
- [Monitoring & operations](docs/operate-monitoring.md)
- [Troubleshooting](docs/operate-troubleshooting.md)
- [CHANGELOG](CHANGELOG.md)

## Client tooling — prefer Codex-native, no binaries

This guidance favors **Codex-native** authentication and telemetry over shipping
custom client binaries:

- **Authentication** — Codex's built-in `amazon-bedrock` provider signs AWS SigV4
  using the standard AWS credential chain; developers authenticate with
  `aws sso login` (IAM Identity Center). The gateway patterns use a `CUSTOM_JWT`
  authorizer, so Codex sends a plain OIDC bearer token issued by your IdP. Codex can
  refresh that token automatically — point the provider at a token-fetch `auth`
  command (model-provider path) or use `[mcp_servers.*.oauth]` (MCP path); a static
  `env_key` token is the manual alternative. See
  [daily use](docs/QUICKSTART_AGENTCORE_GATEWAY.md#daily-use).
  **No credential-helper binary is required for these default paths.**
- **Telemetry** — Codex emits OpenTelemetry natively via its `[otel]` config; you
  point it at a collector (see [operate-monitoring.md](docs/operate-monitoring.md)).
  Per-user identity is added by the **local collector** — baked into the sidecar
  config as `user.id` / `user.email` resource attributes — so **no
  header-enrichment binary is required.** (Codex can set `otel.span_attributes`,
  but those apply to traces, not metrics; for per-user metric attribution either
  bake it in the collector as above, or have Codex forward static `[otel.*].headers`
  and lift them via `from_context`.) Note: **metrics** export (not logs/traces) is
  gated behind `analytics.enabled`, which `codex exec` and the TUI default to
  `true` — so metrics flow by default; they are dropped only if a config sets
  `[analytics] enabled = false`.

> **SigV4 caveat:** Codex cannot sign requests to CloudWatch's native OTLP endpoint
> (which requires SigV4). Any path that ships Codex's own client OTEL to CloudWatch
> therefore runs a standard
> [AWS Distro for OpenTelemetry (ADOT) Collector](https://aws-otel.github.io/) that
> signs and forwards. That is upstream AWS software you run, not a binary shipped by
> this repo.
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

### Optional helper (escape hatch)

| Package | When you need it |
|---------|------------------|
| [aws-oidc-auth/](https://github.com/aws-samples/sample-openai-on-aws/tree/main/aws-oidc-auth) | **Optional.** A `credential_process` helper for organizations that federate a raw OIDC IdP (Okta / Entra ID / Auth0 / Cognito) to AWS **without** IAM Identity Center. If you use IdC (`aws sso login`) or a gateway with OIDC bearer auth, you do **not** need this. See [AUTH_HELPER.md](https://github.com/aws-samples/sample-openai-on-aws/blob/main/AUTH_HELPER.md). |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## License

This repository is dual-licensed:

- **Code** (`.py`, `.js`, `.ts`, `.go`, configuration files, and other source) is licensed under the [MIT No Attribution (MIT-0)](LICENSE) license.
- **Documentation, media, and text content** (`.md` documentation, images, and diagrams) is licensed under the [Creative Commons Attribution-ShareAlike 4.0 International (CC-BY-SA 4.0)](LICENSE-DOCS.md) license.
