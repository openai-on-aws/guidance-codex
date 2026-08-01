# Quick Start: Portkey with Bedrock Mantle

Evaluate Codex through Portkey's hosted gateway and Amazon Bedrock Mantle in
`us-east-1`. This walkthrough fixes the upstream model at `openai.gpt-5.5`,
uses Portkey assumed-role authentication, and exercises the Responses protocol
that Codex uses for reasoning and tools.

Hybrid or self-hosted Portkey is a separate vendor-assisted deployment and is
not part of this walkthrough.

## Evidence Status

| Layer | Status | Evidence |
|---|---|---|
| AWS role deployment | Executable | CloudFormation and Make targets provision a region- and model-scoped role with an external ID |
| Codex custom provider | Verified offline | Generated config uses environment-backed bearer auth and `wire_api = "responses"` |
| Portkey Responses endpoint | Vendor documented | Hosted `/v1/responses` supports SSE, reasoning, and function tools |
| Portkey `bedrock-mantle` provider | Vendor documented | Routes to `bedrock-mantle.<region>.api.aws` and supports assumed-role authentication |
| Stateful continuation | AWS documented | Mantle supports stored Responses and `previous_response_id`; the live probe verifies semantics |
| Live Portkey workspace | Requires credentials | Run the live targets below with a non-production workspace and AWS sandbox |

Do not call this route production-ready until the strict probe, a real
`codex exec`, identity attribution, policy enforcement, and AWS audit evidence
all pass. A failure is evidence to preserve, not a reason to weaken the probe.

## Classic Bedrock Versus Bedrock Mantle

Portkey exposes two distinct provider types:

- **Bedrock** uses the classic Bedrock runtime and Converse/Invoke APIs. In
  Portkey's Responses adapter, `previous_response_id` is not stateful.
- **Bedrock Mantle** uses the OpenAI-compatible Mantle endpoint, including
  native `/v1/responses`. This is the provider required by this walkthrough.

Do not select classic **Bedrock** in Model Catalog for a GPT-5.x Codex test.
The role in this repository grants only `bedrock-mantle:*` operations and does
not grant `bedrock:InvokeModel*`.

## Request Flow

```text
Codex
  -> Portkey hosted /v1/responses
  -> workspace API-key authentication and policy
  -> Bedrock Mantle provider assumes the scoped AWS role
  -> bedrock-mantle.us-east-1.api.aws/v1/responses
  -> openai.gpt-5.5 returns reasoning, text, or a function call
  -> Codex runs local tools in its sandbox and continues the Responses loop
```

Portkey governs model traffic. It does not run the Codex process, shell, or
local tools.

## Prerequisites

- a non-production Portkey organization and workspace;
- the hosted Portkey AWS principal ARN shown for assumed-role integrations;
- a unique external ID of at least 16 characters;
- AWS credentials authorized to deploy an IAM role in `us-east-1`;
- access to `openai.gpt-5.5` through Bedrock Mantle;
- AWS CLI v2, Python 3, GNU Make, and Codex for the full test.

Use an isolated workspace and synthetic prompts. The strict continuation test
sets `store=true` on its initial request. AWS documents that stored Mantle
Responses are retained in the source region for 30 days and scoped to the
calling Bedrock Project. The follow-up and all other probe requests set
`store=false`.

## 1. Configure the Local Deployment File

```bash
cp deployment/portkey/.env.deploy.example \
  deployment/portkey/.env.deploy
```

Initially set:

```dotenv
AWS_REGION=us-east-1
PORTKEY_AWS_PRINCIPAL_ARN=<principal supplied by Portkey>
PORTKEY_EXTERNAL_ID=<unique external ID>
BEDROCK_MANTLE_PROJECT_ID=*
```

The real file is ignored by Git. The helper never prints the external ID or
Portkey API key, and passes CloudFormation parameters through a mode-`0600`
temporary file rather than command-line values.

## 2. Provision the AWS Role

```bash
make portkey-aws-check
make portkey-aws-plan
make portkey-aws-deploy
make portkey-aws-status
```

The stack creates one IAM role with:

- a trust policy for the supplied Portkey principal;
- mandatory `sts:ExternalId` equality;
- `bedrock-mantle:CreateInference` restricted to
  `openai.gpt-5.5` through the `bedrock-mantle:Model` condition;
- response/project read operations on Mantle projects in `us-east-1` only.

The initial project scope is `project/*` because the hosted provider setup does
not expose which Project Portkey will resolve before the first request. After
the live run, record the `proj_...` identifier from CloudTrail, set
`BEDROCK_MANTLE_PROJECT_ID`, and run `make portkey-aws-deploy` again.

## 3. Create the Portkey Provider

In the evaluation workspace:

1. Open **Model Catalog** and add a provider.
2. Select **Bedrock Mantle**, not **Bedrock**.
3. Select **Assumed Role** authentication.
4. Enter the stack's role ARN, the same external ID, and `us-east-1`.
5. Limit provisioning to the evaluation workspace and record the provider slug.
6. Create a workspace API key scoped to inference for that workspace.

Finish the ignored environment file:

```dotenv
PORTKEY_PROVIDER_SLUG=bedrock-mantle-validation
PORTKEY_MODEL=@bedrock-mantle-validation/openai.gpt-5.5
PORTKEY_API_KEY=<workspace key>
```

The helper rejects any region, hosted URL, or upstream model other than this
walkthrough's fixed target. It does not silently fall back to another model.

## 4. Validate the Contract and Codex

```bash
make portkey-check
make portkey-codex-config
make portkey-auth-negative
make portkey-validate
make portkey-codex-validate
```

`portkey-validate` requires:

- `GET /v1/models` to expose the exact configured provider/model entry;
- the exact `openai.gpt-5.5` upstream model;
- Responses object shape and a reasoning output item;
- semantic `previous_response_id` continuation;
- multiple SSE events ending in `response.completed`;
- a forced function-tool call with a call ID.

`portkey-codex-validate` uses `--ignore-user-config`, `--ephemeral`, and a
disposable directory. Codex must read a fixture through a local tool, create a
sentinel file, and return the expected final message. It does not modify the
developer's normal Codex configuration or repository.

## 5. Capture Promotion Evidence

Keep redacted evidence outside Git under `deployment/portkey/.evidence/` until
it is reviewed. Capture:

- output from the strict probe and isolated Codex run;
- Portkey request IDs, workspace identity, provider slug, model, and policy;
- a deliberately exceeded budget or rate policy returning a block;
- API-key revocation time and a rejected request after revocation;
- the assumed role and `bedrock-mantle.amazonaws.com` `CreateInference` event;
- the resolved Mantle Project ARN and the tightened IAM stack status;
- prompt, response, trace, and Portkey log-retention settings.

Mantle inference calls are CloudTrail **data events**, which are not enabled by
default and may incur charges. Enable them explicitly on the evaluation trail
or event data store, then filter for:

```text
eventSource = bedrock-mantle.amazonaws.com
eventName   = CreateInference
awsRegion   = us-east-1
requestParameters.model = openai.gpt-5.5
```

Do not place API keys, external IDs, prompts, response bodies, or unredacted
account identifiers in committed evidence.

## Troubleshooting

- **`previous_response_id` is ignored:** confirm the Model Catalog provider is
  **Bedrock Mantle**. Classic Bedrock is an adapter and is the wrong route.
- **Model validation fails:** confirm the exact Portkey model is
  `@<provider-slug>/openai.gpt-5.5` and that the provider is in `us-east-1`.
- **Access denied from AWS:** verify the Portkey principal, external ID, project
  scope, and `bedrock-mantle:Model` condition. Do not add classic Bedrock
  permissions as a workaround.
- **No CloudTrail inference event:** Mantle inference is a data event; verify
  the trail's advanced event selectors include `AWS::BedrockMantle::Project`.
- **Codex works but identity is missing:** stop promotion and correct the
  workspace key or JWT attribution before distributing configuration.

## Cleanup

First revoke the evaluation API key and remove the Portkey provider. Then:

```bash
make portkey-aws-cleanup-plan
make portkey-aws-cleanup
```

Confirm the IAM role is gone and retain only approved, redacted evidence.

## References

- [Portkey Codex integration](https://docs.portkey.ai/docs/integrations/libraries/codex)
- [Portkey Open Responses](https://docs.portkey.ai/docs/product/ai-gateway/responses-api)
- [Portkey Bedrock Mantle](https://portkey.ai/docs/integrations/llms/bedrock-mantle)
- [AWS Responses API on Bedrock Mantle](https://docs.aws.amazon.com/bedrock/latest/userguide/bedrock-mantle.html)
- [AWS Mantle IAM permissions](https://docs.aws.amazon.com/bedrock/latest/userguide/inference.html)
- [AWS Mantle CloudTrail events](https://docs.aws.amazon.com/bedrock/latest/userguide/logging-cloudtrail-mantle.html)
- [Enterprise Gateway Guidance](ENTERPRISE_GATEWAY_GUIDANCE.md)
