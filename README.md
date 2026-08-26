# Guidance for Codex on AWS

Reference access patterns for connecting [OpenAI Codex](https://developers.openai.com/codex/overview) to models on [Amazon Bedrock](https://aws.amazon.com/bedrock/), with corporate SSO, optional quota enforcement, and observability.

---

## Choose a Rollout Path

```text
Need centralized routing, hard budgets, or gateway policy?
|
|-- NO  -> Native AWS Access with IAM Identity Center
|
`-- YES -> LLM Gateway
```

| Path | Operations | Identity evidence | Best for |
|---|---|---|---|
| **[IAM Identity Center](docs/QUICKSTART_NATIVE_AWS_ACCESS.md)** | Lowest | Native AWS session and CloudTrail identity | Existing AWS SSO and direct Bedrock access |
| **[LiteLLM on ECS](docs/QUICKSTART_LLM_GATEWAY_LITELLM.md)** | Customer-operated stack | Scoped gateway key; optional OIDC mapping | Inspectable AWS stack and hard controls |
| **[Portkey](docs/QUICKSTART_LLM_GATEWAY_PORTKEY.md)** | Customer-operated EKS data plane; Portkey-managed control plane | Workspace Service API key and gateway logs | Integrated Portkey policy and analytics on a private AWS data plane |

A specialized [AgentCore Gateway](docs/QUICKSTART_AGENTCORE_GATEWAY.md) pattern
is also available for customers who need AWS-managed routing, Bedrock
Guardrails, or AWS-private web search.

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

## Client tooling — Codex-native by design

Authentication and telemetry use features built into Codex across every
rollout path.

### Authentication

- **Native AWS Access.** Codex's `amazon-bedrock` provider signs requests with AWS
  SigV4 using the standard credential chain and IAM Identity Center.
- **LiteLLM Gateway.** Codex uses a scoped LiteLLM key, with optional OIDC
  self-service key mapping.
- **Portkey Gateway.** Codex uses a Workspace Service API key.
- **AgentCore Gateway.** Codex sends an OIDC bearer token to the `CUSTOM_JWT`
  authorizer.

Each path's quickstart contains its exact Codex configuration and daily login
flow.

### Telemetry

Codex emits OpenTelemetry natively. An
[ADOT collector](https://aws-otel.github.io/) can add organizational
attribution and sign exports to CloudWatch, while gateway paths also provide
server-side metrics and logs. See
[Monitoring and operations](docs/operate-monitoring.md) for configuration and
dashboard details.

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
