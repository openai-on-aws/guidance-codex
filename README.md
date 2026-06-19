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

## Source Packages

| Package | Description |
|---------|-------------|
| [aws-oidc-auth/](https://github.com/aws-samples/sample-openai-on-aws/tree/main/aws-oidc-auth) | Go credential helper — exchanges OIDC tokens or AWS IdC sessions for temporary AWS credentials. See [AUTH_HELPER.md](https://github.com/aws-samples/sample-openai-on-aws/blob/main/AUTH_HELPER.md) for full docs. |
| [otel-helper/](https://github.com/aws-samples/sample-openai-on-aws/tree/main/otel-helper) | Go binary that enriches OTel headers with AWS credentials for the Native AWS Access OTel pipeline. |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## License

This repository is dual-licensed:

- **Code** (`.py`, `.js`, `.ts`, `.go`, configuration files, and other source) is licensed under the [MIT No Attribution (MIT-0)](LICENSE) license.
- **Documentation, media, and text content** (`.md` documentation, images, and diagrams) is licensed under the [Creative Commons Attribution-ShareAlike 4.0 International (CC-BY-SA 4.0)](LICENSE-DOCS.md) license.
