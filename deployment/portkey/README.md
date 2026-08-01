# Portkey Hybrid on Amazon EKS

This directory deploys Portkey's licensed Enterprise gateway into an AWS
account. Codex sends Responses API traffic to the AWS load balancer; the
gateway uses its EKS service role to call Bedrock Mantle and writes request logs
to S3. Portkey continues to operate the control plane that distributes Model
Catalog configuration to the gateway.

This is different from the hosted path: `api.portkey.ai` is not the Codex data
endpoint. It is also not fully air-gapped; an air-gapped control plane requires
separate Portkey Enterprise artifacts.

## Files

- `.env.deploy.example` — non-secret settings and names of required secrets.
- `eksctl-cluster.yaml.tmpl` — optional two-node sandbox EKS cluster.
- `hybrid-infrastructure.yaml` — retained S3 log bucket and scoped IAM policy;
  the driver uses `eksctl` to bind that policy to an IRSA service account.
- `values.yaml.tmpl` — vendor-supported Helm configuration with placeholders.
- `../scripts/portkey-stack.sh` — plan, deploy, validate, and teardown driver.

The driver renders secret values only into mode-`0600` temporary files. Do not
commit `.env.deploy`, rendered Helm values, Portkey image credentials, client
auth licenses, or workspace API keys.

## Deployment sequence

1. Obtain an immutable Enterprise gateway image tag, Docker credentials, a client auth
   license, and the organization ID from Portkey. Pin the tested Helm chart
   version in `.env.deploy` as well.
2. Copy `.env.deploy.example` to `.env.deploy` and populate it.
3. Use an existing EKS cluster or run `make portkey-cluster-plan` followed by
   `CONFIRM_AWS_WRITE=1 make portkey-cluster-deploy`.
4. Run `make portkey-aws-check`, `make portkey-aws-plan`, and
   `CONFIRM_AWS_WRITE=1 make portkey-aws-deploy`.
5. Install the AWS Load Balancer Controller in the cluster. The Portkey chart
   uses its NLB service annotations and will not become externally reachable
   without a compatible controller.
6. Run `make portkey-helm-plan` and
   `CONFIRM_AWS_WRITE=1 make portkey-helm-deploy`.
7. In Portkey Model Catalog, create a **Bedrock Mantle** provider using
   **Service Role (EKS / IRSA)** and `us-east-1`; record its slug locally.
8. Run `make portkey-validate` and `make portkey-codex-validate`.

When `PORTKEY_BASE_URL` is empty, live validation uses `kubectl port-forward`.
Set it to the reachable NLB or approved TLS endpoint, including `/v1`, before
printing the durable Codex configuration.

The S3 log bucket is intentionally retained when the CloudFormation stack is
deleted. Review and remove it separately after evidence retention requirements
have been satisfied.
