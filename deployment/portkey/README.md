# Portkey Hybrid on Amazon EKS

This directory deploys Portkey's licensed Enterprise gateway into an AWS
account. Codex sends Responses API traffic to the AWS load balancer; the
gateway uses its EKS service role to call Bedrock Mantle in the configured
Mantle region and writes request logs to S3. Portkey continues to operate the
control plane that distributes Model Catalog configuration to the gateway.

This is different from the hosted path: `api.portkey.ai` is not the Codex data
endpoint. It is also not fully air-gapped; an air-gapped control plane requires
separate Portkey Enterprise artifacts.

## Files

- `.env.deploy.example` — non-secret settings and names of required secrets.
- `eksctl-cluster.yaml.tmpl` — optional two-node sandbox EKS cluster.
- `lbc-iam-policy.json.tmpl` — reviewed, NLB-only controller policy scoped to
  `AWS_REGION`, the selected AWS account/VPC, and exact cluster tags. It is
  version-matched to the fixed controller chart release.
- `hybrid-infrastructure.yaml` — retained S3 log bucket and scoped IAM policy;
  the driver uses `eksctl` to bind that policy to an IRSA service account.
- `values.yaml.tmpl` — vendor-supported Helm configuration with placeholders.
- `../scripts/portkey-stack.sh` — plan, deploy, validate, and teardown driver.

The driver requires `.env.deploy` mode `0600` or `0400`, does not blanket-export
its values, and renders secret values only into mode-`0600` temporary files. Do
not commit `.env.deploy`, rendered Helm values, Portkey image credentials,
client-auth licenses, or workspace API keys.

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

1. Obtain an immutable Enterprise gateway image tag, Docker credentials, a client auth
   license, and the organization ID from Portkey. Pin the tested Helm chart
   version in `.env.deploy` as well.
2. Run
   `install -m 600 deployment/portkey/.env.deploy.example deployment/portkey/.env.deploy`
   and populate the resulting file. Set `AWS_REGION` for EKS/log/load-balancer
   resources, `BEDROCK_MANTLE_REGION` for inference, and an explicit
   `PORTKEY_ALLOWED_MODELS` list.
3. Use an existing EKS cluster or run `make portkey-cluster-plan` followed by
   `CONFIRM_AWS_WRITE=1 make portkey-cluster-deploy`. The included cluster path
   creates controller IRSA with the checked-in policy and installs the pinned
   AWS Load Balancer Controller. That controller watches only the Portkey
   namespace and does not become the default mutator for other LoadBalancer
   Services. Listener tagging is explicit, while ALB-only Shield and WAF
   integrations are disabled to match the NLB-only policy.
4. Run `make portkey-aws-check`, `make portkey-aws-plan`, and
   `CONFIRM_AWS_WRITE=1 make portkey-aws-deploy`.
5. For an existing cluster, run `make portkey-lbc-plan`,
   `CONFIRM_AWS_WRITE=1 make portkey-lbc-deploy`, and
   `make portkey-lbc-status`. A ready existing controller is reused only when
   its pinned version, cluster name, and watch namespace are compatible.
6. Run `make portkey-helm-plan` and
   `CONFIRM_AWS_WRITE=1 make portkey-helm-deploy`.
7. In Portkey Model Catalog, create a **Bedrock Mantle** provider using
   **Service Role (EKS / IRSA)** and `BEDROCK_MANTLE_REGION`; record its slug
   locally. Do not reuse that provider slug for a different Mantle region.
8. Run `make portkey-validate` and `make portkey-codex-validate`.

The strict validation target waits up to one minute for Model Catalog sync and
probes every entry in `PORTKEY_ALLOWED_MODELS` through the configured provider.
The real `codex exec` uses exactly `PORTKEY_MODEL`, which must name one of those
allowed model IDs. No model fallback is permitted. A failed model remains a
failed validation; remove it from the allowlist only when it is intentionally
out of deployment scope. Budget roughly one minute per allowlisted model; the
continuation check creates `store=true` state for each model, subject to the
documented 30-day Mantle retention period.

For an existing cluster where this walkthrough installed the controller solely
for Portkey, remove the Portkey release first, then run
`make portkey-lbc-cleanup-plan` and
`CONFIRM_LBC_DELETE=<cluster-name> make portkey-lbc-cleanup`. The cleanup
refuses controllers it does not own and refuses to proceed while any
`TargetGroupBinding`, LoadBalancer Service, Ingress, or Gateway dependency
remains. Never remove a shared controller.

When `PORTKEY_BASE_URL` is empty, live validation uses `kubectl port-forward`.
Set it to an approved TLS endpoint, including `/v1`, before printing the
durable Codex configuration. The helper rejects a non-HTTPS configured URL;
the example internal plaintext NLB is intended only for the local port-forward
path until TLS termination is added.

The S3 log bucket is intentionally retained when the CloudFormation stack is
deleted. Review and remove it separately after evidence retention requirements
have been satisfied.
