# Quick Start: Portkey Hybrid on AWS with Bedrock Mantle

Deploy Portkey's Enterprise data plane to Amazon EKS, configure its dedicated
`bedrock-mantle` provider for `openai.gpt-5.5` in `us-east-1`, and validate a
real Codex workflow through the AWS-hosted gateway.

This path keeps model traffic, the gateway cache, and request/response logs in
the customer AWS account. Portkey's managed control plane still distributes
configuration and provides administration. A fully air-gapped Portkey control
plane is a separate vendor-assisted Enterprise deployment.

Portkey's supported [EKS deployment](https://portkey.ai/docs/self-hosting/hybrid-deployments/aws/eks)
requires licensed images and a client-auth license supplied by Portkey.

## Architecture

```text
Codex
  -> approved HTTPS endpoint or local kubectl port-forward /v1/responses
  -> Portkey Enterprise gateway on EKS
     -> local Redis cache
     -> S3 request/response log store
     -> EKS IRSA service role
  -> bedrock-mantle.us-east-1.api.aws
  -> openai.gpt-5.5

Portkey control plane
  -> configuration sync to the AWS data plane
```

Portkey governs inference traffic only. Codex still runs locally and owns its
shell, filesystem, approvals, and sandbox.

## 1. Prerequisites

- AWS CLI v1 or v2 credentials for a non-production `us-east-1` account;
- `eksctl` 0.229.0 or newer, `kubectl`, Helm 3, Python 3, and Codex CLI;
- an existing EKS cluster with OIDC enabled, or permission to create the
  optional two-node sandbox cluster;
- permission to install the AWS Load Balancer Controller, or an existing ready
  controller for NLB IP targets;
- Portkey Enterprise Docker username/password, immutable gateway image tag,
  patch-pinned Redis image tag, pinned Helm chart version, client-auth license, organization ID,
  and workspace API key.

Copy the ignored environment file:

```bash
install -m 600 deployment/portkey/.env.deploy.example deployment/portkey/.env.deploy
```

Never commit that file. The helper validates credential presence without
printing values, refuses a group/world-readable environment file, does not
blanket-export deployment secrets, and renders Helm secrets into a temporary
mode-`0600` file.

## 2. Create or select EKS

For an existing cluster, set `PORTKEY_CLUSTER_NAME` and skip creation. For the
included sandbox cluster:

```bash
make portkey-cluster-plan
CONFIRM_AWS_WRITE=1 make portkey-cluster-deploy
```

The template creates two `t4g.medium` managed nodes across the EKS-managed VPC.
It also creates a controller-specific IRSA service account and installs the
pinned AWS Load Balancer Controller Helm chart. The command does not proceed to
Portkey until the controller deployment and `TargetGroupBinding` CRD are ready.
The controller watches only the configured Portkey namespace, and its Service
mutator webhook is disabled so it does not claim unrelated LoadBalancer
Services. Listener tagging is explicitly enabled because the checked-in IAM
policy uses the controller's exact cluster tag; ALB-only Shield and WAF
integrations are disabled.
Production users should apply their existing private-cluster, egress,
autoscaling, backup, and admission-control standards instead.

For an existing cluster, install or verify the controller explicitly:

```bash
make portkey-lbc-plan
CONFIRM_AWS_WRITE=1 make portkey-lbc-deploy
make portkey-lbc-status
```

If a ready standard controller deployment already exists, `lbc-deploy` leaves
it in place only after verifying the pinned controller version, target cluster,
and that it watches either all namespaces or the configured Portkey namespace.
Otherwise it creates an IRSA role trusted only by the controller service account
and installs the pinned chart. The reviewed policy in
`deployment/portkey/lbc-iam-policy.json.tmpl` is NLB-only: regional API calls
are fixed to `us-east-1`, mutable resources are account-scoped, security-group
operations are limited to the EKS VPC, new load balancers must be internal,
and controller resources require the exact cluster tag. Read-only EC2/ELB
discovery and service-linked-role creation retain the minimum wildcard resource
scope required by AWS. This controller policy is still broader than the
Portkey gateway's model-scoped permissions. Its chart version is intentionally
fixed because the IAM actions and listener-tag conditions are version-matched.

## 3. Deploy AWS resources

```bash
make portkey-aws-check
make portkey-aws-plan
CONFIRM_AWS_WRITE=1 make portkey-aws-deploy
```

CloudFormation creates:

- an encrypted, versioned S3 log bucket with a 30-day lifecycle;
- a scoped IAM managed policy; `eksctl` creates an IRSA role trusted only by
  `portkeyai/gateway-sa` (or the configured names) and attaches that policy;
- S3 permissions limited to that bucket;
- Bedrock Mantle permissions limited to `us-east-1`, Mantle projects in this
  account, and `openai.gpt-5.5`.

Start with `BEDROCK_MANTLE_PROJECT_ID=*`. After CloudTrail identifies the
project, set its `proj_...` ID—or `default` when Mantle used the account default
project—and deploy again to tighten the role.

The S3 bucket has `DeletionPolicy: Retain`; cleanup reports it rather than
silently deleting validation evidence.

## 4. Install the gateway

The supplied values explicitly assign the Service to the AWS Load Balancer
Controller and request an internal IP-target NLB. The helper requires
`PORTKEY_INTERNAL_NLB=true`; it will not place Portkey API keys or prompts on a
public plaintext listener. Local validation uses `kubectl port-forward` when a
durable private TLS endpoint is not available.

```bash
make portkey-helm-plan
CONFIRM_AWS_WRITE=1 make portkey-helm-deploy
make portkey-status
```

`helm-plan` rejects mutable `latest` image tags. Use versions supplied and
supported by Portkey. The chart deploys the Enterprise gateway, built-in Redis,
Kubernetes secrets, service account, and NLB service.

If upgrading a release created before this controller fix, do not patch the
Service's load-balancer type or scheme in place. The helper stops when either
annotation differs from `external` and `internal`. Delete only the Portkey
gateway Service, wait for its old load balancer to be removed, and rerun
`portkey-helm-deploy`; Helm recreates the Service with the supported
annotations. AWS warns that changing these annotations in place can
misconfigure or leak load balancers.

## 5. Configure Bedrock Mantle

In Portkey Model Catalog:

1. Select **Bedrock Mantle**, not classic **Bedrock**.
2. Select **Service Role (EKS / IRSA)** authentication.
3. Set the AWS region to `us-east-1`; no AWS access key or assumed-role secret
   is required because the pod receives the IRSA role.
4. Save the provider and copy its slug without `@` into
   `PORTKEY_PROVIDER_SLUG`.
5. Set `PORTKEY_MODEL=@<slug>/openai.gpt-5.5`.

Classic `bedrock` uses Converse/InvokeModel and does not provide the same
stateful Responses continuation. The dedicated `bedrock-mantle` provider
forwards Responses requests to the regional Mantle endpoint and supports EKS
service-role authentication. See [Portkey's Mantle integration](https://portkey.ai/docs/integrations/llms/bedrock-mantle).

## 6. Configure Codex

For durable use, place approved TLS termination in front of the internal NLB,
then set `PORTKEY_BASE_URL` to that HTTPS endpoint, including `/v1`, and run:

```bash
make portkey-check
make portkey-codex-config
```

The generated configuration is equivalent to:

```toml
model_provider = "portkey"
model = "@mantle-provider/openai.gpt-5.5"

[model_providers.portkey]
name = "Portkey Hybrid on AWS"
base_url = "https://your-approved-gateway.example/v1"
env_key = "PORTKEY_API_KEY"
wire_api = "responses"
env_http_headers = { "x-portkey-api-key" = "PORTKEY_API_KEY" }
```

Codex sends the key as both bearer authorization and `x-portkey-api-key`. The
strict and negative-auth probes use the identical header contract, preventing a
probe from passing with a request shape that Codex does not use. The isolated
Codex validation also uses `shell_environment_policy.inherit="core"`, so local
tool subprocesses do not inherit the Portkey key or deployment credentials.

Terminate TLS using an approved ALB/NLB listener or internal reverse proxy
before distributing a durable URL. Do not expose the example plaintext NLB to
the public internet.

## 7. End-to-end validation

When `PORTKEY_BASE_URL` is blank, the validation targets automatically open a
temporary local port-forward to the EKS service:

```bash
make portkey-auth-negative
make portkey-validate
make portkey-codex-validate
```

`portkey-validate` allows up to one minute for Model Catalog synchronization.
The strict contract then requires the exact configured
`@<provider-slug>/openai.gpt-5.5` model with no fallback,
Responses shape, reasoning output, stored `previous_response_id`
continuation, completed SSE events, and a forced function call. The Codex test
uses an isolated fixture repository and requires a file read, local tool use,
sentinel-file write, and exact final answer.

AWS documents stored Mantle Responses as retained for 30 days when
`store=true`. Treat continuation as an explicit data-retention choice; do not
send data that policy forbids AWS or the configured S3/Portkey log stores from
retaining. See [AWS Bedrock Mantle Responses](https://docs.aws.amazon.com/bedrock/latest/userguide/bedrock-mantle.html).

Also verify manually:

- the request is attributed to the evaluation key/workspace in Portkey logs;
- CloudTrail Mantle data events record `CreateInference` for the IRSA role and
  `openai.gpt-5.5`;
- a revoked key and an exceeded Portkey budget/rate limit reject inference;
- the IAM role rejects another model;
- captured evidence contains no licenses, Docker credentials, API keys, tokens,
  Kubernetes secrets, or generated Helm values.

The gateway pods require outbound HTTPS access to `api.portkey.ai`,
`albus.portkey.ai`, and `bedrock-mantle.us-east-1.api.aws`. Restricted clusters
must allow those destinations before validation. Full S3-backed log detail in
Portkey's managed control plane also requires the documented control-plane to
data-plane integration. With the default internal NLB, complete Portkey's
vendor-assisted AWS PrivateLink onboarding and endpoint-service approval before
claiming dashboard log evidence; basic inference and port-forward validation do
not require that inbound connection.

If a check fails, keep redacted failure evidence and mark it unverified. Do not
weaken the probe or silently change models.

## 8. Cleanup

Revoke the evaluation key and remove the Model Catalog provider first. Then:

```bash
make portkey-aws-cleanup-plan
CONFIRM_STACK_DELETE=codex-portkey-hybrid make portkey-aws-cleanup
```

If the included sandbox cluster was created solely for this evaluation:

```bash
CONFIRM_CLUSTER_DELETE=codex-portkey make portkey-cluster-cleanup
```

Cluster deletion removes the controller installed with the sandbox. On a
shared existing cluster, leave the AWS Load Balancer Controller in place unless
the cluster owner confirms no other Service or Ingress depends on it.

If this walkthrough installed the controller on an existing cluster and it is
not shared, remove it only after the Portkey cleanup above has deleted its
Service and load balancer:

```bash
make portkey-lbc-cleanup-plan
CONFIRM_LBC_DELETE=codex-portkey make portkey-lbc-cleanup
```

The cleanup verifies Helm and service-account ownership, requires the controller
to be scoped exactly to the Portkey namespace, and stops while any
`TargetGroupBinding`, LoadBalancer Service, Ingress, or Gateway remains. A
reused controller is never removed by this workflow.

The S3 log bucket is retained. Empty and delete it only after confirming the
evidence and retention requirements.
