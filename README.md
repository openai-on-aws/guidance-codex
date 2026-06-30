# Guidance for Codex on AWS

Deployment patterns for running [OpenAI Codex](https://developers.openai.com/codex/overview) on [Amazon Bedrock](https://aws.amazon.com/bedrock/) with corporate SSO, optional quota enforcement, and observability.

## Three Deployment Patterns

| Pattern | Setup Time | Best For |
|---------|------------|----------|
| **[Native AWS Access](docs/QUICKSTART_NATIVE_AWS_ACCESS.md)** | 5–60 min | Teams with IAM Identity Center; native per-user CloudTrail attribution |
| **[AgentCore Gateway](docs/QUICKSTART_AGENTCORE_GATEWAY.md)** | ~10 min | Managed gateway; guardrails, multi-provider routing, AWS-private web search |
| **[LLM Gateway](docs/QUICKSTART_LLM_GATEWAY.md)** | 15 min | Hard per-user/per-team budgets and rate limiting |

Not sure which to pick? See the [decision guide](QUICKSTART.md).

## Documentation

- [Architecture & pattern comparison](docs/01-decide.md)
- [Monitoring & operations](docs/operate-monitoring.md)
- [Troubleshooting](docs/operate-troubleshooting.md)
- [CHANGELOG](CHANGELOG.md)

## Related

- [aws-oidc-auth](https://github.com/aws-samples/sample-openai-on-aws/tree/main/aws-oidc-auth) — optional credential helper for federating OIDC IdPs without IAM Identity Center

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## License

- **Code** is licensed under [MIT No Attribution (MIT-0)](LICENSE).
- **Documentation and text content** is licensed under [CC-BY-SA 4.0](LICENSE-DOCS.md).
