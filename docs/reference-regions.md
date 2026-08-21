# Reference — Region Availability

This repository does not maintain a hard-coded Bedrock region × model matrix.
Availability changes over time, can differ by account, and should be verified
against current AWS documentation and your own AWS account.

For GPT-5.6 applications, AWS recommends the `bedrock-runtime` endpoint whenever
possible. GPT-5.6 uses cross-Region inference profile IDs there. The LiteLLM
sample defaults to `global.openai.gpt-5.6-sol`,
`global.openai.gpt-5.6-terra`, and `global.openai.gpt-5.6-luna`. Use the
equivalent `us.` profiles when US data residency is required and they pass
validation from the chosen source region.

## Source of truth

- AWS Bedrock documentation for OpenAI model availability is the source of truth.
- Account-level verification is the final check: a model may exist in AWS docs
  but still require model access or account enablement in your region.

## Endpoints

- **Bedrock Runtime (preferred):** `bedrock-runtime.<region>.amazonaws.com/openai/v1`
  supports Responses, Chat Completions, and Converse for GPT-5.6. The LiteLLM
  gateway uses this endpoint with Global cross-Region profile IDs.
- **Mantle (compatibility):** `bedrock-mantle.<region>.api.aws/openai/v1`
  remains the endpoint used by Codex's built-in `amazon-bedrock` provider and
  AgentCore's built-in `bedrock-mantle` connector.

The Runtime Responses API requires an inference profile rather than the
foundation model ID. Creating a response also requires `bedrock:InvokeModel`
on the account's `project/default` resource.

The OpenAI-compatible Runtime endpoint authenticates with a Bearer token
(`Authorization: Bearer <key>`). The LiteLLM gateway sample in this repo now
refreshes `AWS_BEARER_TOKEN_BEDROCK` automatically from the gateway's AWS
credentials using the official `aws-bedrock-token-generator` package. For
direct manual API testing, you can still generate a short-term key (12h) from
your IAM credentials:
```bash
uv run --with aws-bedrock-token-generator \
  python -c "from aws_bedrock_token_generator import provide_token; print(provide_token())"
```

## How to verify availability

1. Check the current AWS Bedrock documentation for the model you want.
2. Verify the model appears in your account for the target region:

```bash
aws bedrock list-inference-profiles \
  --region <region> \
  --query "inferenceProfileSummaries[?contains(inferenceProfileId,'openai.gpt-5.6')].inferenceProfileId" \
  --output text
```

If a model ID you need is not in that list, model access is likely not enabled
for the account in that region. Request access in the **Amazon Bedrock** →
**Model access** console page.

## Quotas

Per-account Bedrock invoke quotas apply. Check the Service Quotas console under
**Amazon Bedrock** and filter by the specific inference profile.

For live dashboards of quota consumption, see `operate-monitoring.md` ("Quota
monitoring" section).
