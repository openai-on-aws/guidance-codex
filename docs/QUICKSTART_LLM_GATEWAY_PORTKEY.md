# Quick Start: Portkey Hybrid on AWS with Bedrock Mantle

Deploy Portkey's Enterprise data plane to Amazon EKS, configure its dedicated
`bedrock-mantle` provider with an explicit model allowlist, and validate a real
Codex workflow through the AWS-hosted gateway. The examples and checked-in
offline tests use `openai.gpt-5.5` in `us-east-1` as the tested default, not as
the only supported configuration. No successful live deployment is claimed by
this guide.

This path keeps model traffic, the gateway cache, and request/response logs in
the customer AWS account. Portkey's managed control plane still distributes
configuration and provides administration. A fully air-gapped Portkey control
plane is a separate vendor-assisted Enterprise deployment.

Portkey's supported [EKS deployment](https://portkey.ai/docs/self-hosting/hybrid-deployments/aws/eks)
requires licensed images and a client-auth license supplied by Portkey.

The durable Codex endpoint in this guide is an internal IPv4 NLB that
terminates TLS on port 443 with a pre-existing ACM certificate and forwards to
the Portkey gateway on port 8787. It is not an internet-facing endpoint:
clients need customer-provided private routing and DNS, and frontend access is
limited to corporate/VPN networks in a customer-managed prefix list.

## Portkey dependencies you must arrange manually

This guide automates the AWS data plane, not the Portkey Enterprise entitlement
or managed control plane. Obtain or configure these before the corresponding
deployment step:

- Portkey client-auth license, organization ID, Docker registry credentials, a
  supported Helm chart version, and approved gateway and Redis tag/digest
  pairs. Reject moving gateway aliases such as `latest`, `edge`, and
  `main-latest`; keep Redis patch-tagged. Each matching digest must use
  `sha256:<64-lowercase-hex>`. The included `t4g.medium` nodes require a
  multi-architecture index containing `linux/arm64` or a compatible
  `linux/arm64` manifest;
- worker-node/container-runtime access to the Docker Hub registry,
  authentication, and content endpoints used by the hard-coded
  `docker.io/portkeyai/gateway_enterprise` and `docker.io/redis` repositories,
  or an organization-approved mirror. The current values template does not
  expose repository overrides, so a mirror requires a reviewed customization;
- a Portkey organization/workspace enabled for Hybrid deployment and pod HTTPS
  access to `api.portkey.ai` and `albus.portkey.ai` for configuration sync, AWS
  STS for IRSA, regional S3 in `AWS_REGION` for logs, and
  `bedrock-mantle.<BEDROCK_MANTLE_REGION>.api.aws` for inference. Restricted
  clusters need NAT or service-specific VPC endpoints where the service and
  region support them. Portkey outbound PrivateLink requires vendor-assisted
  onboarding and additional `ALBUS_BASEPATH`, `CONTROL_PLANE_BASEPATH`,
  `SOURCE_SYNC_API_BASEPATH`, and `CONFIG_READER_PATH` settings that this
  repository does not expose; treat it as a separate customization;
- private connectivity from Codex clients to the EKS VPC, a working private
  DNS resolver path, a customer-controlled hostname, an issued ACM certificate
  in the authenticated AWS account and `AWS_REGION` whose SAN covers that
  hostname, and an active customer-managed IPv4 prefix list in that account
  and region containing only the approved corporate/VPN source networks.
  Entries that individually or together cover all IPv4 addresses are rejected,
  and the sum of the lists' `MaxEntries` values must be at most 60. This is a
  conservative security-group quota guard because AWS charges `MaxEntries`,
  not the current entry count. The workflow does not create the certificate,
  DNS, prefix list, VPN, routes, or resolver rules. If the certificate uses a
  private CA, every Codex client must trust that CA;
- a manually configured **Bedrock Mantle** Model Catalog provider using the EKS
  service role and selected Mantle region; the repository's
  `PORTKEY_ALLOWED_MODELS` setting, IAM policy, and probes enforce the model
  allowlist;
- a Portkey **Workspace Service** API key with `completions.write` for the Codex
  checks and evaluation. Admin API keys cannot call inference endpoints.
  Playground, Prompt Studio, and Model Catalog test requests instead require a
  **Workspace User** API key with `completions.write`;
- deployment identifiers that are unused or explicitly dedicated: the stack
  name, namespace/gateway-service-account pair, and Helm release. The helper
  never adopts a same-named gateway service account: it creates one only when
  both it and its deterministic `eksctl` IAM stack are absent, and permits a
  rerun only after verifying the existing pair. Partial, unmanaged, drifted,
  or unreadable state fails before writes. A same-named stack or Helm release
  can still be updated, so verify those owners separately; and
- a distinct Portkey-assisted inbound PrivateLink endpoint-service flow when
  the managed control plane must reach the internal NLB for complete dashboard
  log visibility. Basic inference and local port-forward checks do not require
  this inbound connection. The NLB continues to enforce its frontend security
  group for PrivateLink traffic, so the vendor-assisted design must account for
  that source path rather than widening ingress or disabling enforcement ad
  hoc.

The CloudFormation, `eksctl`, Helm, and Make workflows do not create or issue
these Portkey-side dependencies.

## Happy path

This is the recommended staged sequence for the included non-production
sandbox. The numbered sections below explain each command, security boundary,
and existing cluster variation.

1. Arrange the Enterprise entitlement, deployment artifacts, internet egress,
   private client route and resolver path, controlled hostname, issued
   same-region ACM certificate, corporate/VPN prefix list, and approved image
   tag/digest pairs listed above.
   Defer the Model Catalog provider, provider slug, selected model, and
   inference API key until step 4, after the gateway IRSA role exists. Create
   the ignored environment file and populate the remaining settings, including
   `PORTKEY_GATEWAY_HOSTNAME`, `PORTKEY_NLB_TLS_CERTIFICATE_ARN`, and
   `PORTKEY_NLB_ALLOWED_PREFIX_LIST_IDS`:

   ```bash
   install -m 600 deployment/portkey/.env.deploy.example deployment/portkey/.env.deploy
   ```

   For a durable endpoint, set
   `PORTKEY_BASE_URL=https://<PORTKEY_GATEWAY_HOSTNAME>/v1`. Leave it empty only
   when validation will use a temporary local port-forward.

2. Review each plan, then deploy the sandbox cluster, AWS resources, and
   licensed gateway:

   ```bash
   # Skip the cluster commands when using an existing compatible EKS cluster.
   make portkey-cluster-plan
   CONFIRM_AWS_WRITE=1 make portkey-cluster-deploy

   make portkey-aws-check
   make portkey-aws-plan
   CONFIRM_AWS_WRITE=1 make portkey-aws-deploy

   # Existing clusters only: run these before the Helm deployment.
   make portkey-lbc-plan
   CONFIRM_AWS_WRITE=1 make portkey-lbc-deploy
   make portkey-lbc-status

   make portkey-helm-plan
   CONFIRM_AWS_WRITE=1 make portkey-helm-deploy
   make portkey-status
   ```

3. After `make portkey-status` reports the NLB hostname, create the
   customer-owned private DNS record for `PORTKEY_GATEWAY_HOSTNAME` and point
   it at the NLB. Confirm resolution and HTTPS access from an approved
   corporate/VPN client. The repository does not automate Route 53 or
   enterprise DNS. Also confirm outbound data-plane-to-control-plane
   configuration sync over the internet-egress path implemented by the
   included Helm values. If outbound PrivateLink is required, stop and
   complete the separate Portkey-assisted customization before continuing.
   When full managed dashboard/log evidence is required, complete the distinct
   inbound control-plane-to-data-plane PrivateLink onboarding for the internal
   NLB.
4. In Portkey Model Catalog, create the **Bedrock Mantle** provider with
   **Service Role (EKS / IRSA)**. Then record `PORTKEY_PROVIDER_SLUG`, select an
   allowlisted `PORTKEY_MODEL`, and add a Workspace Service `PORTKEY_API_KEY`
   with `completions.write` to `.env.deploy`.
5. Run the configuration, strict contract, and real Codex checks:

   ```bash
   make portkey-check
   make portkey-validate
   make portkey-codex-validate
   ```

## Architecture

```text
Codex
  -> durable: private DNS over corporate/VPN routing
       -> internal NLB TLS :443 (ACM; approved prefix list only)
       -> gateway TCP :8787
     or validation: local kubectl port-forward /v1/responses
  -> Portkey Enterprise gateway on EKS
     -> local Redis cache (ClusterIP only)
     -> S3 request/response log store
     -> EKS IRSA service role
  -> bedrock-mantle.<BEDROCK_MANTLE_REGION>.api.aws
  -> each model explicitly listed in PORTKEY_ALLOWED_MODELS

Portkey control plane
  -> configuration sync to the AWS data plane
```

Portkey governs inference traffic only. Codex still runs locally and owns its
shell, filesystem, approvals, and sandbox.

## 1. Prerequisites

- AWS CLI v1 or v2 credentials for a non-production AWS account;
- `eksctl` 0.229.0 or newer, `kubectl`, Helm 3, Python 3, and Codex CLI. CI and
  the reference render test use Helm 3.21.4; Helm 4 changes `--post-renderer`
  to plugin semantics and is unsupported by this executable-post-renderer
  workflow. Exact `eksctl` 0.229.0 is enforced at `cluster-deploy`'s pre-write
  gate and when
  `lbc-deploy` will create or update this workflow's walkthrough-managed
  controller IAM stack, whose CloudFormation shape is verified against that
  release. External-controller reuse and status/cleanup flows retain the
  general minimum and do not require the exact pin;
- an existing EKS cluster with OIDC enabled, or permission to create the
  optional two-node sandbox cluster;
- permission to install the AWS Load Balancer Controller, or an existing ready
  controller for NLB IP targets;
- private routing and DNS resolution from approved Codex clients to the EKS
  VPC;
- a controlled gateway hostname, an issued ACM certificate in the
  authenticated AWS account and `AWS_REGION` covering that hostname, and an
  active customer-managed IPv4 prefix list in that account and region
  containing the approved corporate/VPN networks; and
- the Portkey-side artifacts, control-plane connectivity, Model Catalog access,
  and workspace authentication listed in the checklist above.

The deployer also needs these read APIs for fail-closed ownership and exposure
checks, in addition to permissions for the requested mutations:
`sts:GetCallerIdentity`, `eks:DescribeCluster`, `acm:DescribeCertificate`,
`ec2:DescribeManagedPrefixLists`, `ec2:GetManagedPrefixListEntries`,
`cloudformation:ValidateTemplate`, `cloudformation:DescribeStacks`,
`cloudformation:GetTemplate`, `cloudformation:DescribeStackResource`,
`iam:GetRole`, `iam:GetRolePolicy`, `iam:GetPolicy`,
`iam:GetPolicyVersion`, `iam:ListRolePolicies`, and
`iam:ListAttachedRolePolicies`. The helper stops when any required read is
denied instead of skipping the check.

Copy the ignored environment file:

```bash
install -m 600 deployment/portkey/.env.deploy.example deployment/portkey/.env.deploy
```

Never commit that file. The helper validates credential presence without
printing values, refuses a group/world-readable environment file, does not
blanket-export deployment secrets, and renders Helm secrets into a temporary
mode-`0600` file. Set `PORTKEY_GATEWAY_IMAGE_TAG` and
`PORTKEY_GATEWAY_IMAGE_DIGEST` to the approved Portkey release pair, and set
`PORTKEY_REDIS_IMAGE_TAG` and `PORTKEY_REDIS_IMAGE_DIGEST` to the approved
Redis pair. Existing environment files without both digests fail before a Helm
write.

Set the two regions deliberately:

- `AWS_REGION` controls EKS, CloudFormation, the S3 log bucket, and the AWS
  Load Balancer Controller.
- `BEDROCK_MANTLE_REGION` controls the Portkey provider's Mantle endpoint and
  the region condition on Mantle IAM access.

They may differ. `PORTKEY_ALLOWED_MODELS` is a comma-separated list of bare
Mantle IDs, such as `openai.gpt-5.5`. `PORTKEY_MODEL` is the one selected for
Codex and must be formatted as `@<provider-slug>/<allowed-model-id>`.
The helper and CloudFormation template accept only regions in AWS's current
[Bedrock Mantle region list](https://docs.aws.amazon.com/bedrock/latest/userguide/bedrock-mantle.html).
That region check does not prove a particular model is available there; the
live strict probe must still pass every allowlisted model.

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
The externally managed path also requires a fresh, command-scoped cluster-owner
attestation for every command that relies on the controller:

```bash
PORTKEY_EXTERNAL_LBC_POLICY_CONFIRMED=true \
  CONFIRM_AWS_WRITE=1 make portkey-lbc-deploy
PORTKEY_EXTERNAL_LBC_POLICY_CONFIRMED=true make portkey-lbc-status
PORTKEY_EXTERNAL_LBC_POLICY_CONFIRMED=true \
  CONFIRM_AWS_WRITE=1 make portkey-helm-deploy
```

The owner must first verify the version-matched controller's complete base NLB
and security-group actions. For an external role, the helper proves exactly one
trust statement matches the expected EKS OIDC issuer, audience, and service
account, but it permits additional trust statements/principals and a
permissions boundary. The owner must review and accept each addition plus the
effective restrictions imposed by SCPs and other organization policies. The
helper checks the TLS-listener permission subset, not sole trust, completeness
of the external base policy, or effective organization-level authorization.
Never persist
`PORTKEY_EXTERNAL_LBC_POLICY_CONFIRMED=true` in `.env.deploy`, a shell profile,
or CI.

Otherwise `lbc-deploy` creates an IRSA role trusted only by the controller
service account and installs the pinned chart. Before any update to a
walkthrough-managed IAM policy, it validates `PORTKEY_GATEWAY_HOSTNAME`, the
issued same-account/region ACM certificate and SAN, and the active
customer-managed prefix lists and their entries. The reviewed policy in
`deployment/portkey/lbc-iam-policy.json.tmpl` is NLB-only: regional API calls
are fixed to `AWS_REGION`, mutable resources are account-scoped, security-group
operations are limited to the EKS VPC, new load balancers must be internal,
and controller resources require the exact cluster tag. Read-only EC2/ELB
discovery and service-linked-role creation retain the minimum wildcard resource
scope required by AWS. This controller policy is still broader than the
Portkey gateway's model-scoped permissions. Its chart version is intentionally
fixed because the IAM actions, TLS listener-certificate operations, and
listener-tag conditions are version-matched. A walkthrough-managed controller
must reconcile this current policy before the TLS Service is deployed; do not
reuse an older TCP-only policy merely because its controller pods are ready.

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
- Bedrock Mantle permissions limited to `BEDROCK_MANTLE_REGION`, Mantle
  projects in this account, and every model explicitly named in
  `PORTKEY_ALLOWED_MODELS`.

Before OIDC association or CloudFormation mutation, the deployment checks the
gateway service account and its deterministic `eksctl` IAM stack together. If
both are absent, it creates them without an override/adoption flag and verifies
the result. If both already exist, an idempotent project/model-policy rerun is
allowed only after their stack metadata, IAM role and trust, attached managed
policy, and Kubernetes annotation match this deployment. Any partial,
unmanaged, drifted, or unreadable state stops before writes and names the
collision to resolve.

Start with `BEDROCK_MANTLE_PROJECT_ID=*`. After CloudTrail identifies the
project, set its `proj_...` ID—or `default` when Mantle used the account default
project—and deploy again to tighten the role.

The S3 bucket has `DeletionPolicy: Retain`; cleanup reports it rather than
silently deleting validation evidence.

## 4. Install the gateway

The supplied values explicitly keep Redis behind a `ClusterIP` Service and
assign only the gateway Service to the AWS Load Balancer Controller. The
gateway requests an internal IPv4, IP-target NLB with
`allocateLoadBalancerNodePorts: false`, so neither Service exposes a NodePort.
Its only client listener is TLS on port 443 with
`PORTKEY_NLB_TLS_CERTIFICATE_ARN`; it forwards TCP to gateway port 8787 and
uses the gateway's HTTP `/v1/health` check on that target port. Frontend
security-group access is limited to the customer-managed prefix lists in
`PORTKEY_NLB_ALLOWED_PREFIX_LIST_IDS`. The helper requires
`PORTKEY_INTERNAL_NLB=true` and rejects prefix lists whose entries individually
or together cover the full IPv4 address space. It also caps the aggregate
`MaxEntries` at 60; that conservative check does not guarantee capacity when
the account uses a lower quota or the frontend security group has other rules.
Local validation can still use `kubectl port-forward` when the private route or
DNS record is not ready.

```bash
make portkey-helm-plan
CONFIRM_AWS_WRITE=1 make portkey-helm-deploy
make portkey-status
```

`helm-plan` requires both approved tag/digest pairs, rejects moving gateway
aliases, and renders the images as `repository:<tag>@<configured-digest>`.
Kubernetes uses the digest as the content identity even if the registry later
moves the tag. This pins content but does not verify a publisher signature or
provenance. The workflow validates reference syntax but does not resolve
private registry tags; obtain and approve each matching pair through Portkey or
the organization's registry-promotion process. The chart deploys the
Enterprise gateway, built-in Redis, Kubernetes secrets, service account, and
NLB Service. The same checked-in post-renderer is used during plan and
deployment to add the gateway allocation field that chart 1.7.7 does not
expose.

Roll back by restoring a previously approved gateway and Redis tag/digest pair
and rerunning `make portkey-helm-deploy`. Do not select a Helm revision that
predates digest enforcement.

After deployment, obtain the NLB hostname from `make portkey-status`. Create a
customer-owned private DNS record for `PORTKEY_GATEWAY_HOSTNAME` that targets
that NLB, then verify that approved clients resolve it through the intended
resolver path. The hostname must match the ACM certificate and the hostname in
`PORTKEY_BASE_URL`. This workflow deliberately does not create or delete a
Route 53 record.

### Existing endpoint migration

An existing TLS release may already have NodePorts allocated by chart defaults.
The deployment first proves that the gateway is the expected single-port,
IP-target `LoadBalancer` Service. With the normal write confirmation, it then
sets `allocateLoadBalancerNodePorts: false` and removes the allocated gateway
`nodePort` in one patch; IP targets continue to route directly to pods, so the
NLB does not need replacement. Helm changes the built-in Redis Service to
`ClusterIP`. Unexpected Service shape, target mode, read failure, or a
remaining NodePort fails closed. This in-place cleanup is separate from the
legacy plaintext endpoint replacement below.

Treat a release created with the previous plaintext port-80 NLB as an explicit
migration. Do not patch its Service, load-balancer scheme, listener, or security
group in place. First inventory the old NLB ARN and every DNS, ALB/proxy, and
endpoint-service consumer.

Stop before replacing the old Service when:

- a Portkey inbound PrivateLink endpoint service references the old NLB ARN;
  coordinate the new NLB association and any connection reapproval with
  Portkey before continuing;
- a manually created ALB or reverse proxy is serving the endpoint; migrate DNS
  and validate native NLB TLS before removing that customer-owned component,
  and do not open inbound `443` to `0.0.0.0/0` or `::/0` as a workaround;
- an existing DNS record or client configuration still names the old endpoint;
  or
- the load-balancer controller or IAM role is shared with another workload.

If the NLB cannot be deleted, resolve its Service, `TargetGroupBinding`, DNS,
and PrivateLink dependencies instead of forcing cleanup. Before Service
deletion, the helper's local port-forward validation remains available while
migration is paused. It forwards the Service, so it is unavailable after
deletion until Helm creates the new Service. Direct pod forwarding in that
interval is a separate manual diagnostic, not a provided Make target.

Schedule a maintenance window and notify durable-endpoint users. The private
HTTPS endpoint is unavailable from legacy Service deletion until the old NLB
is removed, the controller policy is changed, the new NLB is healthy, and DNS
is repointed.

After all stop conditions are resolved, migrate in this order:

1. Populate the TLS inputs and run `make portkey-helm-plan`, including its
   read-only ACM and prefix-list validation. Stop without deleting anything if
   the plan fails.
2. Delete only the legacy Portkey gateway Service and wait until its old NLB is
   fully removed. Helm is not a continuous reconciler, so it will not recreate
   the Service between Helm commands.
3. Run `CONFIRM_AWS_WRITE=1 make portkey-lbc-deploy` to replace the
   walkthrough-managed controller's legacy TCP policy with the reviewed TLS
   policy. Do not switch that policy while the legacy NLB still needs it.
4. Run `CONFIRM_AWS_WRITE=1 make portkey-helm-deploy`, verify the new NLB and
   TLS controls, and only then create or repoint private DNS.

This order retains the old policy until legacy cleanup finishes; switching the
controller policy first can leave a recovery gap if recovery needs to recreate
or modify the old TCP listener.

### Ongoing exposure change control

Managed prefix-list entries remain mutable after deployment. A later edit can
widen NLB ingress without a Helm or Git diff, so restrict prefix-list mutation,
monitor it through the customer's AWS change-control tooling, and rerun
`make portkey-helm-plan` after every approved change. Restrict Kubernetes RBAC
for mutations to the gateway Service as well. Use admission policy to require
the reviewed internal scheme, TLS certificate/listener, prefix-list, health
check, and backend-security-group annotations and to reject conflicting
source-range or custom-security-group settings.

For an intentional certificate-ARN or prefix-list-ID rotation on an existing
reviewed TLS Service, first review `make portkey-helm-plan`, then provide the
confirmation only to the one mutation command (default names shown):

```bash
CONFIRM_PORTKEY_NLB_TLS_UPDATE=portkeyai/portkey-ai-gateway \
  CONFIRM_AWS_WRITE=1 make portkey-helm-deploy
```

Use `<namespace>/<helm-release>-gateway` for custom names. Never persist
`CONFIRM_PORTKEY_NLB_TLS_UPDATE` in `.env.deploy`, a shell profile, or CI.

## 5. Configure Bedrock Mantle

In Portkey Model Catalog:

1. Select **Bedrock Mantle**, not classic **Bedrock**.
2. Select **Service Role (EKS / IRSA)** authentication.
3. Set the AWS region to `BEDROCK_MANTLE_REGION`; no AWS access key or
   assumed-role secret is required because the pod receives the IRSA role.
4. Save the provider and copy its slug without `@` into
   `PORTKEY_PROVIDER_SLUG`.
5. Set `PORTKEY_ALLOWED_MODELS` to the comma-separated bare model IDs that this
   deployment may use.
6. Set `PORTKEY_MODEL=@<slug>/<model-id>`, where `<model-id>` is exactly one
   entry in `PORTKEY_ALLOWED_MODELS`.

Create a separate Portkey provider and an isolated gateway/IRSA deployment for
every Mantle region. Separate clusters are the default. On an intentionally
shared existing cluster, use a unique stack, namespace, service account, and
Helm release for each region plus a pre-existing, compatible AWS Load Balancer
Controller that watches all namespaces. The included namespace-scoped
controller cannot serve two such deployments. A provider slug configured for
one regional endpoint must not be reused with another
`BEDROCK_MANTLE_REGION`. Re-running deployment against the same stack updates
or replaces that stack's regional and model IAM scope; it does not add
simultaneous access to a second Mantle region.

Static preflight verifies the slug's syntax and that the selected model is
allowlisted. It cannot inspect the region configured for that slug in Portkey.
Confirm the provider's region in Model Catalog, then use live CloudTrail
`CreateInference` evidence to prove that requests reached
`BEDROCK_MANTLE_REGION`.

Classic `bedrock` uses Converse/InvokeModel and does not provide the same
stateful Responses continuation. The dedicated `bedrock-mantle` provider
forwards Responses requests to the regional Mantle endpoint and supports EKS
service-role authentication. See [Portkey's Mantle integration](https://portkey.ai/docs/integrations/llms/bedrock-mantle).

## 6. Configure Codex

For durable use, set `PORTKEY_BASE_URL` to the customer-controlled private
hostname that DNS maps to the NLB, including `/v1`, and run:

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
base_url = "https://portkey.corp.example.com/v1"
env_key = "PORTKEY_API_KEY"
wire_api = "responses"
env_http_headers = { "x-portkey-api-key" = "PORTKEY_API_KEY" }
```

The model shown is the default example. Configuration generation rejects a
selected model that is absent from `PORTKEY_ALLOWED_MODELS`; it does not choose
a substitute.

Codex sends the key as both bearer authorization and `x-portkey-api-key`. The
strict and negative-auth probes use the identical header contract, preventing a
probe from passing with a request shape that Codex does not use. The isolated
Codex validation also uses `shell_environment_policy.inherit="core"`, so local
tool subprocesses do not inherit the Portkey key or deployment credentials.

The managed prefix list is a network boundary, not a replacement for Portkey
authentication. Keep the Workspace Service API key and its
`completions.write` scope even when every client uses private routing. Do not
add an ALB merely to terminate TLS and do not expose this endpoint to the
public internet.

## 7. End-to-end validation

When `PORTKEY_BASE_URL` is blank, the validation targets automatically open a
temporary local port-forward to the EKS service:

```bash
make portkey-auth-negative
make portkey-validate
make portkey-codex-validate
```

The tunnel selects the stable Service port named `gateway`. That name maps
both the legacy Service port 80 and the new Service port 443 to the gateway's
plaintext target port 8787, so the tunnel uses
`http://127.0.0.1:18787/v1`. It does not exercise the NLB certificate, TLS
policy, DNS, frontend security group, prefix-list restriction, or private
routing. Those controls require a second run through the configured HTTPS
`PORTKEY_BASE_URL` from an approved routed client.

`portkey-validate` allows up to one minute for Model Catalog synchronization.
The strict contract probes every model in `PORTKEY_ALLOWED_MODELS` through the
configured provider, with no fallback, and requires the Responses shape,
reasoning output, stored `previous_response_id` continuation, completed SSE
events, and a forced function call. Any allowlisted model that fails remains a
failed check. The Codex test uses exactly `PORTKEY_MODEL` in an isolated fixture
repository and requires a file read, local tool use, sentinel-file write, and
exact final answer. Budget roughly one minute per allowlisted model. Each strict
probe exercises `store=true` continuation, so it creates retained state for
every model it tests.

AWS documents stored Mantle Responses as retained for 30 days when
`store=true`. Treat continuation as an explicit data-retention choice; do not
send data that policy forbids AWS or the configured S3/Portkey log stores from
retaining. See [AWS Bedrock Mantle Responses](https://docs.aws.amazon.com/bedrock/latest/userguide/bedrock-mantle.html).

Also verify manually:

- realized state shows an internal IPv4 NLB with a TLS listener only on port
  443, the expected ACM certificate and TLS policy, no plaintext port-80
  listener, healthy port-8787 targets, and frontend ingress only from the
  configured customer-managed prefix list;
- the gateway is the only `LoadBalancer` Service, its
  `allocateLoadBalancerNodePorts` field is `false`, Redis is `ClusterIP`, and
  neither Service has a `nodePort`;
- the controlled hostname resolves to that NLB over the intended private
  resolver path, an approved corporate/VPN client succeeds, and an unapproved
  routed client is rejected;
- a representative long-running SSE response continues successfully. NLB TLS
  listeners have a
  [fixed 350-second idle timeout](https://docs.aws.amazon.com/elasticloadbalancing/latest/network/update-idle-timeout.html),
  so Portkey and the upstream response path must emit data often enough that
  no idle interval reaches that limit; total stream duration may be longer;
- the provider slug is configured for `BEDROCK_MANTLE_REGION` in Portkey Model
  Catalog; static preflight cannot establish this;
- the request is attributed to the evaluation key/workspace in Portkey logs;
- CloudTrail Mantle data events record `CreateInference` for the IRSA role and
  each model in `PORTKEY_ALLOWED_MODELS` in `BEDROCK_MANTLE_REGION`;
- revoking the evaluation Workspace Service API key and exceeding a Portkey
  budget/rate limit each reject inference;
- the IAM role rejects a model outside `PORTKEY_ALLOWED_MODELS`;
- captured evidence contains no licenses, Docker credentials, API keys, tokens,
  Kubernetes secrets, or generated Helm values.

For restricted egress, do not conflate image-pull and application paths.
Worker nodes/container runtimes need Docker Hub registry, authentication, and
content access for the two hard-coded image repositories, unless a reviewed
template customization points them at an approved mirror. Gateway pods need
HTTPS access to Portkey `api`/`albus`, AWS STS, regional S3 in `AWS_REGION`, and
the Mantle endpoint in `BEDROCK_MANTLE_REGION`. Provide NAT or service-specific
VPC endpoints where supported. Full S3-backed log detail in Portkey's managed
control plane also requires the documented control-plane-to-data-plane
integration. With the default internal NLB, complete Portkey's vendor-assisted
AWS PrivateLink onboarding and endpoint-service approval before claiming
dashboard log evidence; basic inference and port-forward validation do not
require that inbound connection.

If a check fails, keep redacted failure evidence and mark it unverified. Do not
weaken the probe or silently change models.

## 8. Cleanup

Revoke the evaluation **Workspace Service** API key and remove the evaluation
Model Catalog provider first. Then:

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

If the controller Deployment is already missing, cleanup still scans
LoadBalancer, Ingress, and Gateway dependencies across every namespace and
probes the deterministic `eksctl` IAM service-account stack. A surviving stack
without the expected managed service account is an orphaned ownership state,
not proof that cleanup is complete. Restore the verifiable service-account and
stack ownership state with the cluster owner, or use an owner-approved manual
cleanup process; the command will not delete the orphan automatically. After
an approved `eksctl` deletion, it also verifies that the IAM stack disappeared.

The gateway cleanup targets the configured stack, Helm release, namespace, and
gateway service-account names. It refuses to remove an unreadable, partial,
unmanaged, or drifted gateway service-account/`eksctl` stack pair. Resolve that
state with its owner, and separately verify the CloudFormation stack and Helm
release before cleanup because those names can still address pre-existing
resources.

The S3 log bucket is retained. Empty and delete it only after confirming the
evidence and retention requirements. The ACM certificate, customer-managed
prefix list, private DNS record, VPN, routes, resolver rules, manually created
ALB or proxy, and Portkey-assisted PrivateLink resources are not owned by this
workflow and are not deleted. Their availability is not required to clean up
the Kubernetes and AWS resources targeted by the walkthrough. Remove or
repoint them separately only after confirming that no other client, workload,
or endpoint service depends on them.

The checked-in automation and tests do not establish a successful live
Portkey/AWS deployment. Every live command and evidence item above remains an
operator-run acceptance requirement.
