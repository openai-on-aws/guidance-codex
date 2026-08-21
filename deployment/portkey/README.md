# Portkey Hybrid on Amazon EKS

This directory deploys Portkey's licensed Enterprise gateway into an AWS
account. Codex sends Responses API traffic to the AWS load balancer; the
gateway uses its EKS service role to call Bedrock Mantle in the configured
Mantle region and writes request logs to S3. Portkey continues to operate the
control plane that distributes Model Catalog configuration to the gateway.

The included durable client path is an internal IPv4 NLB with an ACM-backed
TLS listener on port 443. It is reachable only through customer-provided
private routing and DNS, with frontend access limited to the corporate/VPN
networks in a customer-managed IPv4 prefix list. The NLB terminates TLS and
forwards TCP to the gateway on port 8787 inside the VPC; it never creates a
public or world-open listener.

This is different from the hosted path: `api.portkey.ai` is not the Codex data
endpoint. It is also not fully air-gapped; an air-gapped control plane requires
separate Portkey Enterprise artifacts.

## Portkey-supplied and manual prerequisites

The repository provisions the AWS data plane; it does not create the Portkey
Enterprise entitlement or managed control-plane configuration. Confirm access
and ownership for these dependencies before starting, then complete each at the
noted deployment stage:

- **Enterprise deployment artifacts:** client-auth license, organization ID,
  Docker registry credentials, a supported Helm chart version, and approved
  gateway and Redis image tag/digest pairs. The gateway tag must be a
  Portkey-supported release rather than `latest`, `edge`, or `main-latest`;
  Redis remains patch-tagged. Each digest must be the matching
  `sha256:<64-lowercase-hex>` registry digest. The sample cluster uses
  `t4g.medium` nodes, so each digest must identify a multi-architecture index
  containing `linux/arm64` or a compatible `linux/arm64` manifest.
- **Worker-node image pulls:** node/container-runtime access to the Docker Hub
  registry, authentication, and content endpoints required by the hard-coded
  `docker.io/portkeyai/gateway_enterprise` and `docker.io/redis` repositories.
  An organization-approved mirror is valid, but the current values template
  does not expose repository overrides; using one requires a reviewed template
  customization.
- **Pod outbound access:** a Portkey organization/workspace enabled for Hybrid
  deployment and pod HTTPS egress to `api.portkey.ai` and `albus.portkey.ai`
  for configuration sync, AWS STS for IRSA, the regional S3 service in
  `AWS_REGION` for logs, and
  `bedrock-mantle.<BEDROCK_MANTLE_REGION>.api.aws` for inference. Restricted
  clusters need NAT or service-specific VPC endpoints where the service and
  region support them. Portkey outbound PrivateLink requires vendor-assisted
  onboarding and additional `ALBUS_BASEPATH`, `CONTROL_PLANE_BASEPATH`,
  `SOURCE_SYNC_API_BASEPATH`, and `CONFIG_READER_PATH` settings that this
  repository does not expose; treat it as a separate customization.
- **Private client path and TLS:** private connectivity from Codex clients to
  the EKS VPC (for example, corporate VPN, Direct Connect, or connected VPC
  routing); a private DNS resolver path; a customer-controlled hostname; an
  issued ACM certificate in the authenticated AWS account and `AWS_REGION`
  whose SAN covers that hostname; and an active customer-managed IPv4 prefix
  list in the same account and region containing only the approved
  corporate/VPN source networks. Entries that individually or together cover
  all IPv4 addresses are rejected. The sum of the lists' `MaxEntries` values
  must be at most 60; this is a conservative security-group quota guard because
  AWS charges `MaxEntries`, not the current entry count. The repository does
  not create the certificate, DNS record, prefix list, VPN, routes, or resolver
  rules. If the certificate uses a private CA, every Codex client must trust
  that CA.
- **Inbound managed access (control plane to data plane):** full dashboard log
  visibility through this guide's internal NLB requires a distinct,
  Portkey-assisted PrivateLink endpoint-service and connection-approval flow.
  Basic inference and local port-forward checks do not require this inbound
  connection. The NLB continues to enforce its frontend security group for
  PrivateLink traffic; the vendor-assisted design must account for that source
  path instead of disabling enforcement or widening ingress ad hoc.
- **Model Catalog configuration:** after the gateway IRSA role exists, manually
  create a **Bedrock Mantle** provider using **Service Role (EKS / IRSA)**,
  select `BEDROCK_MANTLE_REGION`, and record the provider slug in the ignored
  `.env.deploy` file. Configure `PORTKEY_ALLOWED_MODELS` locally; the generated
  IAM policy and strict probes enforce that allowlist.
- **Workspace authentication:** create a Portkey **Workspace Service** API key
  with `completions.write` for the Codex checks and evaluation. Admin API keys
  cannot call inference endpoints. Playground, Prompt Studio, and Model Catalog
  test requests instead require a **Workspace User** API key with
  `completions.write`. Store keys only in `.env.deploy` or an approved secret
  store, and revoke the evaluation Workspace Service API key when the
  evaluation ends.
- **Exclusive deployment names:** verify that `PORTKEY_STACK_NAME`, the
  `PORTKEY_NAMESPACE`/`PORTKEY_SERVICE_ACCOUNT` pair, and
  `PORTKEY_HELM_RELEASE` are unused or explicitly dedicated to this deployment.
  The helper never adopts an existing gateway service account. It creates one
  only when both the account and its deterministic `eksctl` IAM stack are
  absent, and permits an idempotent rerun only after verifying the existing
  pair's stack, role, trust, policy attachment, and service-account annotation.
  A partial, unmanaged, drifted, or unreadable pair fails before any write and
  must be resolved with its owner or replaced with unique names. A same-named
  CloudFormation stack or Helm release can still be updated, so verify their
  ownership separately.

These are required dependencies, not resources created by CloudFormation,
`eksctl`, Helm, or the Make targets in this repository.

## Files

- `.env.deploy.example` — non-secret settings and names of required secrets.
- `eksctl-cluster.yaml.tmpl` — optional two-node sandbox EKS cluster.
- `lbc-iam-policy.json.tmpl` — reviewed, NLB-only controller policy scoped to
  `AWS_REGION`, the selected AWS account/VPC, and exact cluster tags. It is
  version-matched to the fixed controller chart release and includes only the
  listener-certificate operations required for the NLB TLS listener.
- `hybrid-infrastructure.yaml` — retained S3 log bucket and scoped IAM policy;
  the driver uses `eksctl` to bind that policy to an IRSA service account.
- `values.yaml.tmpl` — vendor-supported Helm configuration with placeholders.
- `portkey-post-renderer.sh` — fail-closed Helm post-rendering for the exact
  gateway Service field that chart 1.7.7 does not expose.
- `../scripts/portkey-stack.sh` — plan, deploy, validate, and teardown driver.

The driver requires `.env.deploy` mode `0600` or `0400`, does not blanket-export
its values, and renders secret values only into mode-`0600` temporary files. Do
not commit `.env.deploy`, rendered Helm values, Portkey image credentials,
client-auth licenses, or workspace API keys.

The general minimum is `eksctl` 0.229.0. Exact version 0.229.0 is enforced at
`cluster-deploy`'s pre-write gate and when `lbc-deploy` will create or update
this workflow's walkthrough-managed controller IAM stack, because that
mutation is validated against 0.229.0's generated CloudFormation shape.
External-controller reuse and status or cleanup flows retain the `>=0.229.0`
requirement; they do not require the exact pin.

Use Helm CLI 3; CI and the reference render test use 3.21.4. Helm 4 changes
`--post-renderer` to plugin semantics and is unsupported because this workflow
uses a checked-in executable post-renderer.

The AWS deployer needs read access for the fail-closed preflight in addition to
the documented deployment mutations: `sts:GetCallerIdentity`,
`eks:DescribeCluster`, `acm:DescribeCertificate`,
`ec2:DescribeManagedPrefixLists`, `ec2:GetManagedPrefixListEntries`,
`cloudformation:ValidateTemplate`, `cloudformation:DescribeStacks`,
`cloudformation:GetTemplate`, `cloudformation:DescribeStackResource`,
`iam:GetRole`, `iam:GetRolePolicy`, `iam:GetPolicy`,
`iam:GetPolicyVersion`, `iam:ListRolePolicies`, and
`iam:ListAttachedRolePolicies`. A denied read stops the plan or deployment; the
helper does not skip the corresponding ownership or exposure check.

## Regions and models

`AWS_REGION` selects the region for EKS, the log bucket, CloudFormation, and
the AWS Load Balancer Controller. `BEDROCK_MANTLE_REGION` independently selects
the regional Mantle endpoint used by the Portkey provider and by the gateway
IAM policy. They may differ.

The helper and CloudFormation template accept only regions in AWS's current
[Bedrock Mantle region list](https://docs.aws.amazon.com/bedrock/latest/userguide/bedrock-mantle.html).
Model availability remains region-specific and must pass the live strict probe.

`PORTKEY_ALLOWED_MODELS` is an explicit comma-separated allowlist of bare
Mantle model IDs. `PORTKEY_MODEL` selects one of those IDs in
`@<provider-slug>/<model-id>` form for Codex. The checked-in offline tests and
examples use `us-east-1` and `openai.gpt-5.5` as the default target; this is not
a claim that a live deployment has been validated. The workflow never falls
back to another model.

A Portkey Bedrock Mantle provider belongs to one Mantle region. For multiple
regions, create a separate provider and an isolated gateway/IRSA deployment for
each region. Separate clusters are the default. On an intentionally shared
existing cluster, use a unique stack, namespace, service account, and Helm
release per region plus a pre-existing, compatible AWS Load Balancer Controller
that watches all namespaces. The included namespace-scoped controller cannot
serve two such deployments. Reusing the same stack updates or replaces that
deployment's regional and model IAM scope; it does not add simultaneous access
to a second Mantle region.

Static preflight can validate provider-slug syntax and model membership, but it
cannot inspect the region configured for that slug in Portkey Model Catalog.
Confirm the provider's region in Portkey and prove the live request path with
CloudTrail `CreateInference` evidence in `BEDROCK_MANTLE_REGION`.

## Deployment sequence

1. Arrange the Portkey Enterprise entitlement, deployment artifacts,
   internet-egress control-plane connectivity, private client routing and DNS,
   issued same-region ACM certificate, controlled hostname, and approved
   corporate/VPN prefix list above. Record the supported Helm version and
   approved gateway and Redis tag/digest pairs in `.env.deploy`. Defer the
   Model Catalog provider, provider slug, selected model, and inference API key
   until step 7, after the gateway IRSA role exists.
2. Run
   `install -m 600 deployment/portkey/.env.deploy.example deployment/portkey/.env.deploy`
   and populate the pre-provider settings in the resulting file. Leave
   `PORTKEY_PROVIDER_SLUG`, `PORTKEY_MODEL`, and `PORTKEY_API_KEY` for step 7.
   Set `AWS_REGION` for EKS/log/load-balancer resources,
   `BEDROCK_MANTLE_REGION` for inference, and an explicit
   `PORTKEY_ALLOWED_MODELS` list. Set `PORTKEY_GATEWAY_HOSTNAME`,
   `PORTKEY_NLB_TLS_CERTIFICATE_ARN`, and
   `PORTKEY_NLB_ALLOWED_PREFIX_LIST_IDS` for the durable private endpoint. Set
   `PORTKEY_BASE_URL=https://<PORTKEY_GATEWAY_HOSTNAME>/v1`, or leave it empty
   only when validation will use the temporary local port-forward.
3. Use an existing EKS cluster or run `make portkey-cluster-plan` followed by
   `CONFIRM_AWS_WRITE=1 make portkey-cluster-deploy`. The included cluster path
   creates controller IRSA with the checked-in policy and installs the pinned
   AWS Load Balancer Controller. That controller watches only the Portkey
   namespace and does not become the default mutator for other LoadBalancer
   Services. Listener tagging is explicit, while ALB-only Shield and WAF
   integrations are disabled to match the NLB-only policy.
4. Run `make portkey-aws-check`, `make portkey-aws-plan`, and
   `CONFIRM_AWS_WRITE=1 make portkey-aws-deploy`. On a fresh deployment the
   gateway service account and deterministic `eksctl` IAM stack must both be
   absent. A later project/model-policy tightening rerun is idempotent only
   when both existing resources pass the ownership checks; all collision and
   partial-state cases stop before mutation.
5. For an existing cluster, run `make portkey-lbc-plan`,
   `CONFIRM_AWS_WRITE=1 make portkey-lbc-deploy`, and
   `make portkey-lbc-status`. A ready existing controller is reused only when
   its pinned version, cluster name, and watch namespace are compatible.
   `lbc-deploy` validates the configured hostname, issued ACM certificate, and
   customer-managed prefix lists before it updates any walkthrough-managed IAM
   policy.
6. Run `make portkey-helm-plan` and
   `CONFIRM_AWS_WRITE=1 make portkey-helm-deploy`. After the NLB hostname is
   available, create the customer-owned private DNS record for
   `PORTKEY_GATEWAY_HOSTNAME` and point it at that NLB. Confirm that it resolves
   through the intended corporate/VPN resolver path. This workflow does not
   create or delete Route 53 or enterprise DNS records.
7. Confirm that the gateway synchronizes configuration over the documented
   internet-egress path. Then, in Portkey Model Catalog, create a **Bedrock
   Mantle** provider using **Service Role (EKS / IRSA)** and
   `BEDROCK_MANTLE_REGION`; set `PORTKEY_PROVIDER_SLUG` and an allowlisted
   `PORTKEY_MODEL` locally. Add a Workspace Service `PORTKEY_API_KEY` with
   `completions.write`. Do not reuse that provider slug for a different Mantle
   region.
8. Run `make portkey-validate` and `make portkey-codex-validate`.

Helm plan/deploy requires both approved image pairs. It renders each workload
as `repository:<tag>@<configured-digest>`; Kubernetes uses the digest as the
content identity even if a registry later moves the tag. This is an integrity
pin, not proof of publisher signature or provenance. The workflow validates
reference syntax but does not resolve private registry tags; obtain and approve
each matching pair through Portkey or the organization's registry-promotion
process. An existing `.env.deploy` without both digest values fails before Helm
writes to Kubernetes. Roll back by restoring a previously approved tag/digest
pair and running the normal Helm deployment, not by selecting a Helm revision
that predates digest enforcement.

The rendered chart keeps Redis cluster-internal with a `ClusterIP` Service.
The gateway remains the only `LoadBalancer` Service, uses IP targets, and sets
`allocateLoadBalancerNodePorts: false`; neither Service may contain a
`nodePort`. The checked-in post-renderer supplies the gateway field that the
pinned chart does not expose, and the same renderer is mandatory for plan and
deploy.

Reusing an externally managed, ready controller requires a fresh cluster-owner
attestation on every command that relies on it:

```bash
PORTKEY_EXTERNAL_LBC_POLICY_CONFIRMED=true \
  CONFIRM_AWS_WRITE=1 make portkey-lbc-deploy
PORTKEY_EXTERNAL_LBC_POLICY_CONFIRMED=true make portkey-lbc-status
PORTKEY_EXTERNAL_LBC_POLICY_CONFIRMED=true \
  CONFIRM_AWS_WRITE=1 make portkey-helm-deploy
```

Before giving that attestation, the owner must verify the controller's complete
version-matched base NLB and security-group permissions. For an external role,
the helper proves that exactly one trust statement matches the expected EKS
OIDC issuer, audience, and service account, but it permits other trust
statements and principals and a permissions boundary. The owner must review and
accept each of those additions, plus the effective restrictions from SCPs and
other organization policies. The helper checks the TLS-listener permission
subset across inline and attached policies; it does not prove sole trust,
complete base permissions, or effective organization-level authorization.
Keep `PORTKEY_EXTERNAL_LBC_POLICY_CONFIRMED=true` command-scoped; never add it
to `.env.deploy`, a shell profile, or CI configuration.

The strict validation target waits up to one minute for Model Catalog sync and
probes every entry in `PORTKEY_ALLOWED_MODELS` through the configured provider.
The real `codex exec` uses exactly `PORTKEY_MODEL`, which must name one of those
allowed model IDs. No model fallback is permitted. A failed model remains a
failed validation; remove it from the allowlist only when it is intentionally
out of deployment scope. Budget roughly one minute per allowlisted model; the
continuation check creates `store=true` state for each model, subject to the
documented 30-day Mantle retention period.

Before distributing the durable URL, inspect realized AWS and Kubernetes state,
not only the rendered Helm plan. Confirm that the load balancer is internal,
has only a TLS listener on port 443 with the expected certificate and TLS
policy, has no plaintext port-80 listener, and limits frontend ingress to the
configured customer-managed prefix list. Confirm healthy port-8787 targets,
that the gateway is the sole `LoadBalancer`, that Redis is `ClusterIP`, and
that neither Service has a NodePort allocation. Also confirm the expected
private DNS answer, successful access from an approved routed
client, and rejection from an unapproved routed client. Run a long-lived SSE
request as an acceptance check: an NLB TLS listener has a
[fixed 350-second idle timeout](https://docs.aws.amazon.com/elasticloadbalancing/latest/network/update-idle-timeout.html),
so the gateway and upstream path must emit data often enough that no idle
interval reaches that limit. The limit is not a cap on total stream duration.

For an existing cluster where this walkthrough installed the controller solely
for Portkey, remove the Portkey release first, then run
`make portkey-lbc-cleanup-plan` and
`CONFIRM_LBC_DELETE=<cluster-name> make portkey-lbc-cleanup`. The cleanup
refuses controllers it does not own and refuses to proceed while any
`TargetGroupBinding`, LoadBalancer Service, Ingress, or Gateway dependency
remains. Never remove a shared controller.

Controller-cleanup troubleshooting is fail-closed. If the controller Deployment
is already missing, the plan still scans LoadBalancer, Ingress, and Gateway
dependencies across all namespaces and probes the deterministic `eksctl` IAM
service-account stack. A stack that remains without the expected managed
service account is treated as orphaned, not clean: restore a verifiable
walkthrough-owned service-account/stack state with the cluster owner, or use an
owner-approved manual cleanup process. The command will not delete the orphan
automatically, and after an approved `eksctl` deletion it verifies that the IAM
stack actually disappeared.

When `PORTKEY_BASE_URL` is empty, live validation uses `kubectl port-forward`.
That tunnel selects the stable Service port named `gateway`, which maps both
the legacy Service port 80 and the new Service port 443 to the plaintext
gateway target on port 8787. Its loopback URL therefore remains
`http://127.0.0.1:18787/v1`; TLS termination exists only on the AWS NLB data
path. Set `PORTKEY_BASE_URL` to the private HTTPS hostname, including `/v1`,
before printing the durable Codex configuration. The helper rejects a
non-HTTPS configured URL.

The prefix-list IDs are checked during plan/deploy, but their entries remain
mutable after the NLB is created. Put prefix-list edits under network change
control and monitoring, and rerun `make portkey-helm-plan` after every change;
an in-place prefix-list edit can widen ingress without a Helm or Git diff.
Likewise, restrict Kubernetes RBAC for mutations to the gateway Service and
use admission policy to require the reviewed internal scheme, TLS listener,
certificate, prefix-list, and backend-security-group annotations. Read access
alone is sufficient for operators who only inspect status.

For an intentional certificate-ARN or prefix-list-ID rotation on an existing
reviewed TLS Service, first review `make portkey-helm-plan`, then scope the
confirmation to that single command (the example uses default names):

```bash
CONFIRM_PORTKEY_NLB_TLS_UPDATE=portkeyai/portkey-ai-gateway \
  CONFIRM_AWS_WRITE=1 make portkey-helm-deploy
```

Substitute `<namespace>/<helm-release>-gateway` when names differ. Never add
`CONFIRM_PORTKEY_NLB_TLS_UPDATE` to `.env.deploy`, a shell profile, or CI
configuration.

## Existing deployments and migration stops

An existing release may already have NodePorts allocated by chart defaults.
Before changing it, the helper proves that the gateway is the expected
single-port, IP-target `LoadBalancer` Service. Under the normal write
confirmation it then disables future allocation and removes the existing
gateway `nodePort` in one patch; IP targets route directly to pods, so this does
not require replacing the NLB. Helm also changes the built-in Redis Service to
`ClusterIP`, removing its NodePort surface. Any unexpected Service shape,
target mode, lookup failure, or post-deployment NodePort stops the workflow
instead of being patched speculatively. This in-place NodePort migration is
separate from the legacy plaintext endpoint replacement below.

Do not turn a legacy plaintext NLB into this endpoint by editing its Service or
security groups in place. Inventory the current NLB ARN, DNS consumers,
endpoint services, and any manually added ALB before replacing the legacy
Service.

Stop and plan the migration explicitly when any of these conditions applies:

- a Portkey inbound PrivateLink endpoint service references the old NLB ARN;
  replacing the NLB requires Portkey coordination, endpoint-service updates,
  and potentially connection reapproval;
- a manually created ALB, reverse proxy, or DNS record is serving the current
  endpoint; move DNS and validate the native NLB TLS path before removing that
  customer-owned component, and never use `0.0.0.0/0` or `::/0` as the fix;
- another workload shares the load-balancer controller or its IAM role; update
  only with the cluster owner's approval and do not replace a shared
  controller; or
- the old NLB cannot be deleted cleanly. Resolve its Service,
  `TargetGroupBinding`, DNS, and PrivateLink dependencies before continuing.

Before the legacy Service is deleted, the helper's local port-forward
validation remains available while migration is paused. It forwards that
Service, so it is unavailable after deletion until Helm creates the new
Service. Direct pod forwarding during that interval would be a separate manual
diagnostic, not a path provided by these Make targets.

Schedule a maintenance window and notify durable-endpoint users before this
migration. The private HTTPS endpoint is unavailable after the legacy Service
is deleted and remains unavailable while the old NLB is removed, the
controller policy is changed, the new NLB becomes healthy, and DNS is
repointed.

After every stop condition is resolved, use this order:

1. Populate the new TLS inputs and run `make portkey-helm-plan`, including its
   read-only certificate and prefix-list validation. Do not delete anything if
   that plan fails.
2. Delete only the legacy gateway Service and wait until its old NLB is fully
   removed. Helm is not a continuous reconciler, so it will not recreate the
   Service between Helm commands.
3. Run `CONFIRM_AWS_WRITE=1 make portkey-lbc-deploy` to replace the
   walkthrough-managed controller's legacy TCP policy with the reviewed TLS
   policy. Do not switch that policy while the legacy NLB still needs it.
4. Run `CONFIRM_AWS_WRITE=1 make portkey-helm-deploy`, then create or repoint
   private DNS only after the new NLB and TLS checks pass.

This ordering preserves the TCP policy until the old NLB is gone and avoids a
recovery gap in which the controller cannot recreate or modify the legacy TCP
listener if recovery is required.

Before cleanup, revoke the evaluation Workspace Service API key and remove the
evaluation Model Catalog provider. The cleanup targets the configured stack,
release, namespace, and gateway service-account names. It refuses to remove an
unreadable, partial, unmanaged, or drifted gateway service-account/`eksctl`
stack pair; resolve that state with its owner. Verify the independently named
CloudFormation stack and Helm release before cleanup because their names can
still address pre-existing resources. The separate load-balancer-controller
cleanup performs its own ownership checks as described above.

The S3 log bucket is intentionally retained when the CloudFormation stack is
deleted. Review and remove it separately after evidence retention requirements
have been satisfied. The ACM certificate, customer-managed prefix list,
private DNS record, VPN, routes, resolver rules, manually created ALB or proxy,
and Portkey-assisted PrivateLink resources remain customer- or vendor-owned;
this workflow neither creates nor deletes them. Their availability is not a
prerequisite for cleaning up the resources targeted by the walkthrough.
Remove or repoint them separately only after confirming that no other client
or endpoint service depends on them.

The repository's automated coverage for this change is offline. These live
deployment and acceptance steps remain operator-run requirements; this guide
does not claim that they have passed in a Portkey workspace or AWS account.
