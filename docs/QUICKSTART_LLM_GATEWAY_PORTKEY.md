# Quick Start: Portkey Hybrid on AWS with Bedrock Mantle

> **Status:** Reference implementation
>
> **Audience:** Teams evaluating Portkey Hybrid on Amazon EKS
>
> **Production use:** Requires customer networking, Portkey Enterprise
> artifacts, and operator-run acceptance tests

This guide deploys the Portkey Enterprise data plane to Amazon EKS and connects
Codex to an explicitly allowlisted Bedrock Mantle model. The durable client
endpoint is a private hostname backed by an internal Network Load Balancer
(NLB) with TLS on port 443.

Repository tests validate the templates and automation offline. Treat the
deployment as unverified until the checks in [Validate the deployment](#8-validate-the-deployment)
pass in your AWS account and Portkey workspace.

Portkey's supported [EKS deployment](https://portkey.ai/docs/self-hosting/hybrid-deployments/aws/eks)
requires licensed images and a client-auth license supplied by Portkey.
The Portkey organization and workspace must be enabled for Hybrid deployment.
This guide does not cover a fully air-gapped Portkey control plane, which
requires separate Enterprise artifacts and design work.

## What the deployment looks like

![Codex request flow through Portkey Hybrid on AWS](assets/portkey-architecture.png)

The request path is:

```text
Codex -> private DNS and routing -> internal NLB TLS :443
      -> Portkey gateway pod TCP :8787 -> Bedrock Mantle
```

Codex, local files, tools, sandboxing, and approvals stay on the developer
workstation. The NLB terminates TLS and sends plaintext TCP inside the VPC to
the gateway pod. Redis uses a `ClusterIP` Service. The gateway uses IRSA to
call Bedrock Mantle and write logs to S3.

Portkey's managed control plane is separate from the inference path. The
gateway initiates outbound HTTPS connections to it for configuration and
control synchronization.

| This repository deploys | You provide |
| --- | --- |
| Optional two-node sandbox EKS cluster | Portkey Enterprise entitlement and licensed artifacts |
| AWS Load Balancer Controller when the workflow owns it | Approved gateway and Redis image tag/digest pairs |
| Retained S3 log bucket and scoped IAM policy | Private routing, resolver path, hostname, ACM certificate, and prefix list |
| Gateway IRSA service account and role | Private DNS record that points the hostname to the NLB |
| Portkey Helm release, Redis, and internal NLB Service | Portkey Model Catalog provider, Workspace Service API key, and required egress |

## Prerequisites

You need:

- AWS CLI v1 or v2 credentials for a non-production AWS account;
- `eksctl`, `kubectl`, Helm 3, Python 3, and the Codex CLI;
- either an existing EKS cluster with OIDC enabled or permission to create the
  included sandbox cluster;
- permission to install or reuse a compatible AWS Load Balancer Controller;
- a Hybrid-enabled Portkey organization and workspace, plus Enterprise Docker
  credentials, client-auth license, organization ID, supported chart version,
  and approved image tag/digest pairs;
- a customer-controlled hostname, an issued ACM certificate in `AWS_REGION`
  that covers it, and an active customer-managed IPv4 prefix list in the same
  account and Region;
- private client routing and DNS resolution to the EKS VPC; and
- unused or explicitly dedicated stack, namespace/service-account, and Helm
  release names.

Use `eksctl` 0.229.0 or newer generally. The workflow requires exact version
0.229.0 when it creates the sandbox cluster or creates or updates its managed
load-balancer-controller IAM stack. Helm 4 is not supported because the
workflow uses an executable post-renderer; CI uses Helm 3.21.4.

The included sandbox uses `t4g.medium` nodes. Both image digests must resolve
to a multi-architecture index containing `linux/arm64` or to an ARM64 image.
Each digest must be `sha256:` plus exactly 64 lowercase hexadecimal characters,
and Redis must use a patch tag such as `7.2.10-alpine`. The helper validates
syntax, but it does not query the private registry or verify signatures. The
workflow rejects floating gateway tags such as `latest`, `edge`, and
`main-latest`.

This walkthrough creates billable resources, including EKS control-plane and
worker-node capacity, an NLB, S3 storage, and model inference. Review prices in
the selected Regions and clean up the evaluation when it is finished.

For production, apply your normal private-cluster, egress, autoscaling, backup,
and admission-control standards; the included cluster is an evaluation
sandbox.

The baseline also needs these network paths:

| Source | Destination | Purpose |
| --- | --- | --- |
| Codex client | Customer private DNS and routing to the internal NLB | Inference traffic |
| Worker nodes/container runtime | Docker Hub registry, auth, and content endpoints | Pull the gateway and Redis images |
| Gateway pod | `api.portkey.ai` and `albus.portkey.ai` | Configuration and control synchronization |
| Gateway pod | AWS STS and regional S3 in `AWS_REGION` | IRSA and request/response logs |
| Gateway pod | `bedrock-mantle.<BEDROCK_MANTLE_REGION>.api.aws` | Model inference |

Restricted clusters need NAT or supported service-specific VPC endpoints.
PrivateLink and registry-mirror customizations are described in the
[implementation reference](../deployment/portkey/README.md#network-requirements).

<details>
<summary>AWS read permissions used by preflight checks</summary>

In addition to permissions for the requested deployment changes, the caller
needs:

```text
sts:GetCallerIdentity
eks:DescribeCluster
acm:DescribeCertificate
ec2:DescribeManagedPrefixLists
ec2:GetManagedPrefixListEntries
cloudformation:ValidateTemplate
cloudformation:DescribeStacks
cloudformation:GetTemplate
cloudformation:DescribeStackResource
iam:GetRole
iam:GetRolePolicy
iam:GetPolicy
iam:GetPolicyVersion
iam:ListRolePolicies
iam:ListAttachedRolePolicies
```

A denied read stops the workflow. The helper does not skip ownership or
exposure checks when it cannot inspect a resource.

</details>

## Automated path

Create the ignored environment file first:

```bash
install -m 600 deployment/portkey/.env.deploy.example \
  deployment/portkey/.env.deploy
```

Fill the deployment and image settings described in step 1. Leave the provider
slug, selected model, and API key empty until the gateway IRSA role exists.
Then, for the included sandbox, run:

```bash
make portkey-cluster-plan
CONFIRM_AWS_WRITE=1 make portkey-cluster-deploy

make portkey-aws-check
make portkey-aws-plan
CONFIRM_AWS_WRITE=1 make portkey-aws-deploy

make portkey-helm-plan
CONFIRM_AWS_WRITE=1 make portkey-helm-deploy
make portkey-status
```

Pause here. Create private DNS, create the Bedrock Mantle provider and Workspace
Service key, and add `PORTKEY_PROVIDER_SLUG`, `PORTKEY_MODEL`, and
`PORTKEY_API_KEY` to `.env.deploy`. Print the Codex configuration:

```bash
make portkey-check
make portkey-codex-config
```

Paste that output into user-level `~/.codex/config.toml`, then provide only
`PORTKEY_API_KEY` to the Codex process through your approved secret manager or
credential helper. Do not source `.env.deploy`; it also contains registry
credentials and the gateway license. Verify the installed user configuration,
then run the deployment probes:

```bash
codex exec 'Reply with exactly PORTKEY_CODEX_READY'
make portkey-auth-negative
make portkey-validate
make portkey-codex-validate
```

For an existing EKS cluster, skip the cluster targets and run the
`portkey-lbc-*` sequence in step 2 before Helm. The sections below explain each
phase and its expected result.

## Deployment

Run the following steps in order. Commands that change AWS or Kubernetes
state require an explicit confirmation variable.

### 1. Create the environment file

Copy the example with restrictive permissions:

```bash
install -m 600 deployment/portkey/.env.deploy.example \
  deployment/portkey/.env.deploy
```

Edit the ignored file. The following block shows the settings to review before
creating the cluster or gateway resources:

```dotenv
# Regions and deployment names
AWS_REGION=us-east-1
BEDROCK_MANTLE_REGION=us-east-1
PORTKEY_CLUSTER_NAME=codex-portkey
PORTKEY_NAMESPACE=portkeyai
PORTKEY_SERVICE_ACCOUNT=gateway-sa
PORTKEY_STACK_NAME=codex-portkey-hybrid
PORTKEY_HELM_RELEASE=portkey-ai

# Private client endpoint
PORTKEY_INTERNAL_NLB=true
PORTKEY_GATEWAY_HOSTNAME=portkey.corp.example.com
PORTKEY_NLB_TLS_CERTIFICATE_ARN=arn:aws:acm:us-east-1:123456789012:certificate/replace-me
PORTKEY_NLB_ALLOWED_PREFIX_LIST_IDS=pl-0123456789abcdef0
PORTKEY_BASE_URL=https://portkey.corp.example.com/v1

# Portkey-supplied deployment artifacts
PORTKEY_DOCKER_USERNAME=replace-with-registry-user
PORTKEY_DOCKER_PASSWORD=replace-with-registry-password
PORTKEY_CLIENT_AUTH=replace-with-client-auth-license
PORTKEY_ORGANIZATION_ID=replace-with-organization-id
PORTKEY_HELM_CHART_VERSION=1.7.7
PORTKEY_GATEWAY_IMAGE_TAG=replace-with-approved-version
PORTKEY_GATEWAY_IMAGE_DIGEST=sha256:0000000000000000000000000000000000000000000000000000000000000000
PORTKEY_REDIS_IMAGE_TAG=replace-with-approved-patch-version
PORTKEY_REDIS_IMAGE_DIGEST=sha256:0000000000000000000000000000000000000000000000000000000000000000

# Start broad only long enough to discover the Mantle project in CloudTrail.
BEDROCK_MANTLE_PROJECT_ID=*
PORTKEY_ALLOWED_MODELS=openai.gpt-5.5

# Fill these after the gateway IRSA role exists and the provider is created.
PORTKEY_PROVIDER_SLUG=
PORTKEY_MODEL=
PORTKEY_API_KEY=
```

`AWS_REGION` controls EKS, CloudFormation, S3, and load-balancer resources.
`BEDROCK_MANTLE_REGION` controls the Mantle provider endpoint and the IAM Region
condition. They may differ, but the helper accepts only documented Mantle
Regions in the same AWS partition.

The prefix lists must be active, customer-managed, IPv4 lists in the same
account and `AWS_REGION`. Their entries may not individually or together cover
all IPv4 addresses, and the sum of their `MaxEntries` values must be at most
60. Protect later prefix-list edits with normal network change control.

Keep `.env.deploy` at mode `0600` or `0400` and never commit it. The helper
does not adopt a same-named gateway service account. Same-named CloudFormation
stacks and Helm releases can still be updated, so confirm their ownership
before continuing.

Leave `PORTKEY_BASE_URL` blank only when you intend to validate through the
temporary local port-forward. Helm still requires the hostname, certificate,
and prefix-list settings.

### 2. Create or select EKS

Choose one path.

#### Option A: Create the sandbox cluster

```bash
make portkey-cluster-plan
CONFIRM_AWS_WRITE=1 make portkey-cluster-deploy
```

The command creates two `t4g.medium` managed nodes, associates the cluster OIDC
provider, creates the controller IRSA role, and installs the pinned AWS Load
Balancer Controller. It waits for the controller Deployment and
`TargetGroupBinding` CRD before returning.

The controller watches the Portkey namespace. Its Service mutator webhook and
ALB-only Shield/WAF integrations are disabled. Do not run the separate
`portkey-lbc-*` deployment commands for this path.

Expected result: the EKS cluster is ready, its OIDC provider is associated,
and the controller Deployment and `TargetGroupBinding` CRD are available.

#### Option B: Use an existing cluster

Set `PORTKEY_CLUSTER_NAME` and confirm OIDC is enabled. If there is no
controller, or the existing controller is owned by this workflow, use:

```bash
make portkey-lbc-plan
CONFIRM_AWS_WRITE=1 make portkey-lbc-deploy
make portkey-lbc-status
```

The workflow reuses a ready controller only when its version, cluster, service
account, feature gates, and namespace scope are compatible. A controller
created by this repository uses the checked-in NLB-only policy.

If the controller is managed by the cluster owner, use the following block
instead. Review its complete NLB and security-group permissions and effective
IAM restrictions, then provide the attestation to each command that uses it:

```bash
make portkey-lbc-plan

PORTKEY_EXTERNAL_LBC_POLICY_CONFIRMED=true \
  CONFIRM_AWS_WRITE=1 make portkey-lbc-deploy

PORTKEY_EXTERNAL_LBC_POLICY_CONFIRMED=true \
  make portkey-lbc-status
```

Keep `PORTKEY_EXTERNAL_LBC_POLICY_CONFIRMED=true` command-scoped. Do not add it
to `.env.deploy`, a shell profile, or CI. The implementation reference lists
the trust, boundary, SCP, and policy checks the cluster owner must review.

Expected result: the selected cluster has one compatible, ready controller
that can create the reviewed NLB and frontend security-group resources.

### 3. Deploy AWS resources and gateway IRSA

```bash
make portkey-aws-check
make portkey-aws-plan
CONFIRM_AWS_WRITE=1 make portkey-aws-deploy
```

CloudFormation creates an encrypted, versioned S3 log bucket and a managed IAM
policy scoped to that bucket, `BEDROCK_MANTLE_REGION`, the configured project
scope, and every model in `PORTKEY_ALLOWED_MODELS`. The bucket is retained when
the stack is deleted.

`eksctl` creates the gateway service account and IRSA role; Helm reuses it.
Fresh creation requires both the service account and deterministic IAM stack
to be absent. A rerun is allowed only when the existing pair passes the
ownership check. See [Gateway service-account ownership](../deployment/portkey/README.md#gateway-service-account-ownership).

Expected result: the CloudFormation stack is deployed, and the gateway service
account is annotated with a verified IRSA role that uses the stack's managed
policy.

Start with `BEDROCK_MANTLE_PROJECT_ID=*`. After a live request identifies the
project, replace it with the observed `proj_...` ID, or `default` when no
OpenAI project header was supplied, and run the plan and deployment again.

### 4. Install the gateway

```bash
make portkey-helm-plan
CONFIRM_AWS_WRITE=1 make portkey-helm-deploy
make portkey-status
```

For an externally managed controller, use the attested deployment form:

```bash
PORTKEY_EXTERNAL_LBC_POLICY_CONFIRMED=true \
  CONFIRM_AWS_WRITE=1 make portkey-helm-deploy
```

The Helm plan and deployment use the same checked-in post-renderer. The
resulting Kubernetes services are:

- one internal, IPv4, IP-target NLB Service for the gateway;
- TLS only on port 443, using the configured ACM certificate;
- TCP from the NLB to gateway port 8787, with HTTP health checks on
  `/v1/health` at port 8787;
- frontend security-group ingress only from the configured prefix lists;
- `allocateLoadBalancerNodePorts: false` on the gateway Service; and
- a `ClusterIP` Service for Redis, with no NodePort.

The gateway and Redis images are rendered as `repository:<tag>@<digest>`.
Kubernetes pulls by digest, so a later tag move does not change the deployed
content. Digest pinning proves content identity, not publisher authenticity.

To roll back, restore a previously approved gateway and Redis tag/digest pair
in `.env.deploy`, review `make portkey-helm-plan`, and redeploy. Do not select a
Helm revision that predates digest enforcement.

Expected result: the release is ready, the gateway is the only
`LoadBalancer`, Redis is `ClusterIP`, and `portkey-status` prints the NLB target
after AWS provisions it.

### 5. Create private DNS and verify resolution

`make portkey-status` prints the NLB DNS name. Create a customer-owned private
DNS record for `PORTKEY_GATEWAY_HOSTNAME` that points to that NLB.

Check the path from an approved corporate or VPN client:

```bash
nslookup portkey.corp.example.com
```

The hostname must match both the ACM certificate and `PORTKEY_BASE_URL`. If
the certificate uses a private CA, each client must trust that CA. The live
request checks run after the API key is configured.

This workflow does not create or delete Route 53 records, enterprise DNS,
VPNs, routes, prefix lists, or resolver rules.

Expected result: an approved client resolves the private hostname to the NLB.

### 6. Configure Bedrock Mantle in Portkey

After the gateway IRSA role exists:

1. Open Portkey Model Catalog and select **Bedrock Mantle**, not classic
   **Bedrock**.
2. Choose **Service Role (EKS / IRSA)** authentication.
3. Set the provider Region to `BEDROCK_MANTLE_REGION`.
4. Save the provider and copy its slug without the leading `@`.
5. Create a **Workspace Service** API key with `completions.write`.
6. Add the provider slug, selected model, and key to `.env.deploy`:

```dotenv
PORTKEY_PROVIDER_SLUG=mantle-provider
PORTKEY_ALLOWED_MODELS=openai.gpt-5.5
PORTKEY_MODEL=@mantle-provider/openai.gpt-5.5
PORTKEY_API_KEY=replace-with-workspace-service-api-key
```

`PORTKEY_MODEL` must select one entry from `PORTKEY_ALLOWED_MODELS`. The
workflow does not fall back to another provider or model.

Admin API keys cannot call inference endpoints. Portkey Playground, Prompt
Studio, and Model Catalog test requests require a Workspace User API key;
Codex validation in this guide uses the Workspace Service key.

Classic `bedrock` uses Converse/InvokeModel and does not provide the same
stateful Responses continuation. This guide uses Portkey's dedicated
[`bedrock-mantle` integration](https://portkey.ai/docs/integrations/llms/bedrock-mantle).

For multi-Region deployments, use a separate provider and isolated gateway
scope per Region. See the [configuration contract](../deployment/portkey/README.md#configuration-contract).

Static checks cannot read the Region configured for a provider slug in
Portkey. Confirm it in Model Catalog, then use CloudTrail `CreateInference`
events in `BEDROCK_MANTLE_REGION` as live evidence.

Passing the supported-Region preflight does not prove that a particular model
is available there. Every allowlisted model must pass the live strict probe.

### 7. Configure Codex

Validate the configuration and print the Codex provider block:

```bash
make portkey-check
make portkey-codex-config
```

`portkey-codex-config` requires the durable HTTPS `PORTKEY_BASE_URL`. If you are
using only the temporary local diagnostic, skip that command and continue with
the validation commands in the next section.

The generated configuration is equivalent to:

```toml
model_provider = "portkey"
model = "@mantle-provider/openai.gpt-5.5"

[model_providers.portkey]
name = "Portkey Hybrid on AWS"
base_url = "https://portkey.corp.example.com/v1"
env_key = "PORTKEY_API_KEY"
wire_api = "responses"
env_http_headers = { "x-portkey-api-key" = "PORTKEY_API_KEY" }
```

Add the generated block to the user-level `~/.codex/config.toml`. Codex sends
the Workspace Service key as bearer authorization and as
`x-portkey-api-key`. The managed prefix list controls network reachability; it
does not replace application authentication.

For normal Codex use, inject only `PORTKEY_API_KEY` into the Codex process with
an approved secret manager or credential helper. Do not source
`.env.deploy`—that file also contains registry credentials and the gateway
license. With the key available, test the user-level configuration:

```bash
codex exec 'Reply with exactly PORTKEY_CODEX_READY'
```

`make portkey-codex-validate` uses its own isolated configuration, so it does
not replace this user-level smoke test.

The isolated Codex validation uses
`shell_environment_policy.inherit="core"`, so tool subprocesses do not inherit
the Portkey key or deployment credentials.

### 8. Validate the deployment

Run the checks in this order:

```bash
make portkey-auth-negative
make portkey-validate
make portkey-codex-validate
```

`portkey-auth-negative` verifies that deliberately invalid bearer and
`x-portkey-api-key` credentials are rejected. `portkey-validate` probes every
model in `PORTKEY_ALLOWED_MODELS`
with no fallback and checks the Responses shape, reasoning output, stored
`previous_response_id` continuation, completed SSE events, and a forced
function call. `portkey-codex-validate` uses exactly `PORTKEY_MODEL` in an
isolated repository and requires a file read, local tool use, sentinel-file
write, and exact final answer.

Allow roughly one minute per model for Model Catalog synchronization and the
contract checks. Each model probe uses `store=true`; AWS documents a 30-day
retention period for stored Mantle Responses. See [AWS Bedrock Mantle Responses](https://docs.aws.amazon.com/bedrock/latest/userguide/bedrock-mantle.html).

Stored continuation is an explicit retention choice. Do not send data that
your policy forbids AWS, the retained S3 bucket, or Portkey logs from retaining.

#### Durable endpoint versus local diagnostic

With `PORTKEY_BASE_URL=https://<hostname>/v1`, the checks use the private NLB
endpoint. When `PORTKEY_BASE_URL` is blank, they open a temporary tunnel:

```text
http://127.0.0.1:18787/v1 -> Service port named gateway -> pod port 8787
```

The local tunnel bypasses the NLB certificate, TLS policy, private DNS,
frontend security group, prefix-list restriction, and private routing. A
successful tunnel test does not validate those controls.

If you first ran the checks through this tunnel, set `PORTKEY_BASE_URL` to the
private HTTPS endpoint and rerun all three commands from an approved routed
client before accepting the durable path.

#### Manual acceptance checks

Before sharing the durable endpoint:

| Layer | Verify |
| --- | --- |
| NLB and TLS | Internal IPv4 NLB; TLS only on 443; expected certificate and policy; no port 80; healthy port-8787 targets |
| Network access | Frontend security group uses only the approved prefix lists; approved client succeeds; unapproved routed client fails |
| Kubernetes | Gateway is the only `LoadBalancer`; NodePort allocation is disabled; Redis is `ClusterIP`; neither Service has a `nodePort` |
| DNS | Private hostname resolves through the intended resolver and matches the certificate |
| Streaming | A long SSE response succeeds without a 350-second idle interval; NLB TLS listeners have a [fixed 350-second idle timeout](https://docs.aws.amazon.com/elasticloadbalancing/latest/network/update-idle-timeout.html) |
| Portkey | Provider uses `BEDROCK_MANTLE_REGION`; logs attribute the request to the evaluation key and workspace; key revocation and configured budget/rate limits reject inference |
| AWS | CloudTrail Mantle data events show `CreateInference` for the gateway IRSA role and each allowlisted model; the role rejects a model outside the allowlist |
| Evidence | Captures contain no licenses, credentials, API keys, tokens, Kubernetes secrets, or rendered Helm values |

CloudTrail data-event logging is not enabled by this workflow and may incur
charges. Enable it before collecting the `CreateInference` evidence, then
restore the customer's normal trail configuration.

Complete S3-backed log evidence in Portkey's managed dashboard requires the
separate vendor-assisted inbound PrivateLink endpoint-service and approval
flow. Do not claim that evidence before the path exists. Basic inference and
local validation do not require it.

If a check fails, keep redacted evidence and mark that check unverified. Do not
weaken the probe or silently switch models.

Expected result: all three commands succeed for the configured endpoint, and
the manual checks establish the NLB, DNS, TLS, network, and AWS controls that
the API probes cannot inspect.

## Existing endpoints and configuration changes

Fresh deployments can skip this section.

### Remove NodePorts from an existing TLS release

Earlier chart defaults may have allocated NodePorts. During a confirmed Helm
deployment, the helper first verifies the existing gateway is the expected
single-port, IP-target `LoadBalancer` Service. It then sets
`allocateLoadBalancerNodePorts: false` and removes the gateway `nodePort` in one
patch. Helm changes Redis to `ClusterIP`.

An unexpected Service shape, target type, lookup failure, or remaining
NodePort stops the deployment. This in-place cleanup does not replace the NLB.

### Replace a legacy plaintext port-80 NLB

A plaintext endpoint requires NLB replacement and planned downtime. Do not
patch it in place or update the controller policy before the old NLB is gone.
Follow the complete [legacy plaintext NLB procedure](../deployment/portkey/README.md#legacy-plaintext-nlb),
including the DNS, ALB/proxy, PrivateLink, and shared-controller stop
conditions.

### Rotate the certificate or prefix lists

Review `make portkey-helm-plan`, then scope the confirmation to the single
deployment command:

```bash
CONFIRM_PORTKEY_NLB_TLS_UPDATE=portkeyai/portkey-ai-gateway \
  CONFIRM_AWS_WRITE=1 make portkey-helm-deploy
```

For custom names, use `<namespace>/<helm-release>-gateway`. Never persist
`CONFIRM_PORTKEY_NLB_TLS_UPDATE`.

Prefix-list entries remain mutable after deployment. Put edits under network
change control, monitor them, and rerun `make portkey-helm-plan` after each
approved change. The implementation reference covers RBAC, admission policy,
and PrivateLink controls.

## Troubleshooting

### Helm plan rejects the certificate or prefix list

Confirm the certificate is `ISSUED`, belongs to the authenticated account and
`AWS_REGION`, and covers `PORTKEY_GATEWAY_HOSTNAME`. Confirm every prefix list
is active, customer-managed, IPv4, and in the same account and Region.

### Port-forward works but private HTTPS fails

The pod and Portkey configuration are reachable, but the durable path is not
yet proven. Check private DNS, resolver forwarding, routes, the NLB TLS
listener and certificate, target health, and frontend security-group rules.

### The provider or model returns an authorization error

Confirm the Model Catalog provider uses **Bedrock Mantle**, Service Role
authentication, and `BEDROCK_MANTLE_REGION`. Confirm the selected model is in
`PORTKEY_ALLOWED_MODELS`, then inspect the gateway IRSA policy and CloudTrail.

### An existing controller is rejected

Do not weaken the compatibility check. Ask the cluster owner to verify the
controller's version, feature gates, namespace scope, IRSA trust, base NLB
permissions, permissions boundary, and SCPs. Use the command-scoped external
controller attestation only after that review.

For implementation details and ownership checks, see
[deployment/portkey/README.md](../deployment/portkey/README.md).

## Cleanup

Revoke the evaluation Workspace Service key and remove the evaluation Model
Catalog provider first.

### Remove the gateway and AWS stack

The command below uses the default stack name. If you changed it, replace
`codex-portkey-hybrid` with your exact `PORTKEY_STACK_NAME`.

```bash
make portkey-aws-cleanup-plan
CONFIRM_STACK_DELETE=codex-portkey-hybrid make portkey-aws-cleanup
```

The cleanup removes the Helm release, verifies and removes the gateway IRSA
service-account stack, and deletes the CloudFormation stack. It stops when the
gateway ownership check fails.

The helper can prove ownership of the gateway service-account/`eksctl` stack
pair, but not of a same-named Helm release or main CloudFormation stack. Verify
both before confirming deletion. If cleanup stops after deleting an earlier
resource, inspect and recover the remaining state with its owner before
retrying.

The S3 log bucket is retained. Review its evidence and retention requirements
before emptying and deleting it separately.

### Remove the dedicated sandbox cluster

Run this only when the included cluster is dedicated to the walkthrough. The
command uses the default cluster name; if you changed it, replace
`codex-portkey` with your exact `PORTKEY_CLUSTER_NAME`.

```bash
CONFIRM_CLUSTER_DELETE=codex-portkey make portkey-cluster-cleanup
```

This deletes the configured cluster. It does not prove walkthrough ownership,
so never use it against a shared cluster.

### Remove a walkthrough-installed controller from an existing cluster

Remove the gateway first, and continue only if the controller is not shared.
The command uses the default cluster name; if you changed it, replace
`codex-portkey` with your exact `PORTKEY_CLUSTER_NAME`.

```bash
make portkey-lbc-cleanup-plan
CONFIRM_LBC_DELETE=codex-portkey make portkey-lbc-cleanup
```

The cleanup scans for `TargetGroupBinding`, LoadBalancer Service, Ingress, and
Gateway dependencies and refuses controllers it does not own. A reused
controller is never removed.

If the deterministic controller IAM stack remains without its matching
`eksctl`-managed service account, the workflow treats it as orphaned and does
not delete it automatically. Restore verifiable service-account/stack
ownership with the cluster owner or use an approved manual cleanup process.

The workflow does not delete the ACM certificate, prefix lists, private DNS,
VPN, routes, resolver rules, customer-created ALBs or proxies, or
Portkey-assisted PrivateLink resources. Their availability is not required for
walkthrough cleanup. Remove or repoint them only after checking for other
consumers.

## References

- [Portkey Hybrid on EKS](https://portkey.ai/docs/self-hosting/hybrid-deployments/aws/eks)
- [Portkey Bedrock Mantle integration](https://portkey.ai/docs/integrations/llms/bedrock-mantle)
- [AWS Bedrock Mantle documentation](https://docs.aws.amazon.com/bedrock/latest/userguide/bedrock-mantle.html)
- [Gateway pattern requirements](QUICKSTART_LLM_GATEWAY.md)
- [Portkey implementation reference](../deployment/portkey/README.md)
