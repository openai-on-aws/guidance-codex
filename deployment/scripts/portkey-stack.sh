#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${PORTKEY_ENV_FILE:-$ROOT_DIR/deployment/portkey/.env.deploy}"
INFRA_TEMPLATE="$ROOT_DIR/deployment/portkey/hybrid-infrastructure.yaml"
CLUSTER_TEMPLATE="$ROOT_DIR/deployment/portkey/eksctl-cluster.yaml.tmpl"
LBC_POLICY_TEMPLATE="$ROOT_DIR/deployment/portkey/lbc-iam-policy.json.tmpl"
VALUES_TEMPLATE="$ROOT_DIR/deployment/portkey/values.yaml.tmpl"
CONTRACT_PROBE="$ROOT_DIR/deployment/scripts/validate-responses-contract.py"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

if [[ -f "$ENV_FILE" ]]; then
  env_file_mode="$(python3 - "$ENV_FILE" <<'PY'
import os
import stat
import sys
print(f"{stat.S_IMODE(os.stat(sys.argv[1]).st_mode):03o}")
PY
)" || \
    die "could not inspect permissions on $ENV_FILE"
  [[ "$env_file_mode" == 600 || "$env_file_mode" == 400 ]] || \
    die "$ENV_FILE must have mode 0600 or 0400; run chmod 600 before continuing"
  # shellcheck disable=SC1090
  source "$ENV_FILE"
fi

AWS_REGION="${AWS_REGION:-us-east-1}"
BEDROCK_MANTLE_REGION="${BEDROCK_MANTLE_REGION:-$AWS_REGION}"
PORTKEY_CLUSTER_NAME="${PORTKEY_CLUSTER_NAME:-codex-portkey}"
PORTKEY_NAMESPACE="${PORTKEY_NAMESPACE:-portkeyai}"
PORTKEY_SERVICE_ACCOUNT="${PORTKEY_SERVICE_ACCOUNT:-gateway-sa}"
PORTKEY_STACK_NAME="${PORTKEY_STACK_NAME:-codex-portkey-hybrid}"
PORTKEY_HELM_RELEASE="${PORTKEY_HELM_RELEASE:-portkey-ai}"
PORTKEY_HELM_CHART_VERSION="${PORTKEY_HELM_CHART_VERSION:-}"
PORTKEY_GATEWAY_SERVICE="${PORTKEY_HELM_RELEASE}-gateway"
PORTKEY_INTERNAL_NLB="${PORTKEY_INTERNAL_NLB:-true}"
SUPPORTED_LBC_HELM_CHART_VERSION=3.4.2
PORTKEY_LBC_HELM_CHART_VERSION="${PORTKEY_LBC_HELM_CHART_VERSION:-$SUPPORTED_LBC_HELM_CHART_VERSION}"
AWS_LBC_HELM_RELEASE=aws-load-balancer-controller
AWS_LBC_NAMESPACE=kube-system
AWS_LBC_SERVICE_ACCOUNT=aws-load-balancer-controller
EKSCTL_MIN_VERSION=0.229.0
SUPPORTED_BEDROCK_MANTLE_REGIONS=(
  ap-northeast-1 ap-south-1 ap-southeast-2 ap-southeast-3
  eu-central-1 eu-north-1 eu-south-1 eu-west-1 eu-west-2
  sa-east-1 us-east-1 us-east-2 us-gov-west-1 us-west-2
)
PORTKEY_BASE_URL="${PORTKEY_BASE_URL:-}"
PORTKEY_PROVIDER_SLUG="${PORTKEY_PROVIDER_SLUG:-}"
PORTKEY_MODEL="${PORTKEY_MODEL:-}"
PORTKEY_ALLOWED_MODELS="${PORTKEY_ALLOWED_MODELS-openai.gpt-5.5}"
PORTKEY_ALLOWED_MODEL_IDS=()
BEDROCK_MANTLE_PROJECT_ID="${BEDROCK_MANTLE_PROJECT_ID:-*}"
export AWS_REGION
[[ -z "${AWS_PROFILE:-}" ]] || export AWS_PROFILE
[[ -z "${KUBECONFIG:-}" ]] || export KUBECONFIG
for secret_name in PORTKEY_DOCKER_USERNAME PORTKEY_DOCKER_PASSWORD PORTKEY_CLIENT_AUTH PORTKEY_ORGANIZATION_ID PORTKEY_API_KEY; do
  export -n "$secret_name" 2>/dev/null || true
done

AWS_ARGS=(--region "$AWS_REGION")
[[ -z "${AWS_PROFILE:-}" ]] || AWS_ARGS=(--profile "$AWS_PROFILE" "${AWS_ARGS[@]}")

require_command() { command -v "$1" >/dev/null || die "$1 is required"; }
require_value() { local name="$1"; [[ -n "${!name:-}" ]] || die "set $name in $ENV_FILE or the environment"; }
aws_cli() { aws "${AWS_ARGS[@]}" "$@"; }
confirm_write() { [[ "${CONFIRM_AWS_WRITE:-}" == 1 ]] || die 'set CONFIRM_AWS_WRITE=1 for AWS or Kubernetes mutations'; }
aws_partition_for_region() {
  case "$1" in
    cn-*) printf 'aws-cn\n' ;;
    us-gov-*) printf 'aws-us-gov\n' ;;
    us-iso-*) printf 'aws-iso\n' ;;
    us-isob-*) printf 'aws-iso-b\n' ;;
    eu-isoe-*) printf 'aws-iso-e\n' ;;
    us-isof-*) printf 'aws-iso-f\n' ;;
    *) printf 'aws\n' ;;
  esac
}
validate_region_name() {
  local name="$1" value="$2"
  [[ ${#value} -le 32 && "$value" =~ ^[a-z]{2}(-[a-z0-9]+)+-[0-9]+$ ]] || \
    die "$name must be a valid AWS Region identifier"
}
bedrock_mantle_region_is_supported() {
  local candidate="$1" region
  for region in "${SUPPORTED_BEDROCK_MANTLE_REGIONS[@]}"; do
    [[ "$candidate" != "$region" ]] || return 0
  done
  return 1
}
parse_allowed_models() {
  local raw="$PORTKEY_ALLOWED_MODELS" model i j
  [[ ${#raw} -le 4096 ]] || \
    die 'PORTKEY_ALLOWED_MODELS must not exceed 4096 characters'
  [[ -n "$raw" && "$raw" != ,* && "$raw" != *, && "$raw" != *,,* ]] || \
    die 'PORTKEY_ALLOWED_MODELS must contain one or more comma-separated model IDs'
  [[ "$raw" != *[[:space:]]* ]] || \
    die 'PORTKEY_ALLOWED_MODELS must not contain whitespace'
  IFS=',' read -r -a PORTKEY_ALLOWED_MODEL_IDS <<<"$raw"
  (( ${#PORTKEY_ALLOWED_MODEL_IDS[@]} >= 1 && ${#PORTKEY_ALLOWED_MODEL_IDS[@]} <= 20 )) || \
    die 'PORTKEY_ALLOWED_MODELS must contain between one and twenty model IDs'
  for ((i=0; i<${#PORTKEY_ALLOWED_MODEL_IDS[@]}; i++)); do
    model="${PORTKEY_ALLOWED_MODEL_IDS[$i]}"
    [[ "$model" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$ ]] || \
      die "invalid model ID in PORTKEY_ALLOWED_MODELS: $model"
    for ((j=0; j<i; j++)); do
      [[ "$model" != "${PORTKEY_ALLOWED_MODEL_IDS[$j]}" ]] || \
        die "duplicate model ID in PORTKEY_ALLOWED_MODELS: $model"
    done
  done
}
model_is_allowed() {
  local candidate="$1" model
  for model in "${PORTKEY_ALLOWED_MODEL_IDS[@]}"; do
    [[ "$candidate" != "$model" ]] || return 0
  done
  return 1
}
require_eksctl() {
  require_command eksctl; require_command python3
  local version
  version="$(eksctl version 2>/dev/null | head -n 1)"
  PORTKEY_EKSCTL_VERSION="$version" PORTKEY_EKSCTL_MIN_VERSION="$EKSCTL_MIN_VERSION" python3 - <<'PY' || \
    die "eksctl $EKSCTL_MIN_VERSION or newer is required"
import os, re
def parsed(name):
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", os.environ[name])
    if not match:
        raise SystemExit(1)
    return tuple(map(int, match.groups()))
raise SystemExit(0 if parsed("PORTKEY_EKSCTL_VERSION") >= parsed("PORTKEY_EKSCTL_MIN_VERSION") else 1)
PY
}

help_text() {
  cat <<'EOF'
Portkey Hybrid on Amazon EKS

  cluster-plan      Render and validate the optional eksctl sandbox cluster.
  cluster-deploy    Create EKS and install its load-balancer controller (CONFIRM_AWS_WRITE=1).
  lbc-plan/deploy   Render or install the AWS Load Balancer Controller.
  lbc-status        Require a ready AWS Load Balancer Controller deployment.
  lbc-cleanup-plan  Verify that a walkthrough-managed controller is safe to remove.
  lbc-cleanup       Remove that controller after exact confirmation.
  aws-check         Verify AWS identity and the target EKS cluster.
  plan/deploy       Validate/deploy S3 logs and the gateway IRSA role.
  helm-plan         Render and lint the secret-bearing Helm release locally.
  helm-deploy       Install/upgrade Portkey in EKS (CONFIRM_AWS_WRITE=1).
  status            Show CloudFormation, pods, service, and gateway endpoint.
  check             Validate Codex/Portkey settings without printing secrets.
  codex-config      Print Codex config for the AWS-hosted gateway.
  validate          Run strict Responses/Mantle contract checks.
  codex-validate    Run an isolated Codex file/tool/write workflow.
  auth-negative     Confirm an invalid Portkey key is rejected.
  cleanup-plan      Show the Helm and CloudFormation resources to remove.
  cleanup           Uninstall Helm/delete the stack; retained S3 is reported.
  cluster-cleanup   Delete the optional cluster after exact confirmation.
EOF
}

validate_common() {
  validate_region_name AWS_REGION "$AWS_REGION"
  validate_region_name BEDROCK_MANTLE_REGION "$BEDROCK_MANTLE_REGION"
  bedrock_mantle_region_is_supported "$BEDROCK_MANTLE_REGION" || \
    die 'BEDROCK_MANTLE_REGION is not an AWS-documented Bedrock Mantle region'
  [[ "$(aws_partition_for_region "$AWS_REGION")" == "$(aws_partition_for_region "$BEDROCK_MANTLE_REGION")" ]] || \
    die 'AWS_REGION and BEDROCK_MANTLE_REGION must use the same AWS partition'
  parse_allowed_models
  [[ "$PORTKEY_CLUSTER_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$ ]] || die 'invalid PORTKEY_CLUSTER_NAME'
  [[ "$PORTKEY_NAMESPACE" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] || die 'invalid PORTKEY_NAMESPACE'
  [[ "$PORTKEY_SERVICE_ACCOUNT" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] || die 'invalid PORTKEY_SERVICE_ACCOUNT'
  [[ "$PORTKEY_INTERNAL_NLB" == true ]] || die 'PORTKEY_INTERNAL_NLB must remain true; this walkthrough does not expose Portkey API keys or prompts through a public plaintext NLB'
  [[ "$PORTKEY_LBC_HELM_CHART_VERSION" == "$SUPPORTED_LBC_HELM_CHART_VERSION" ]] || die "PORTKEY_LBC_HELM_CHART_VERSION must remain $SUPPORTED_LBC_HELM_CHART_VERSION because the checked-in IAM policy is version-matched"
  [[ ${#BEDROCK_MANTLE_PROJECT_ID} -le 256 ]] || die 'BEDROCK_MANTLE_PROJECT_ID must not exceed 256 characters'
  [[ "$BEDROCK_MANTLE_PROJECT_ID" == '*' || "$BEDROCK_MANTLE_PROJECT_ID" == default || "$BEDROCK_MANTLE_PROJECT_ID" =~ ^proj_[A-Za-z0-9_-]+$ ]] || die 'invalid BEDROCK_MANTLE_PROJECT_ID'
}

render_cluster() {
  local output="$1"
  PORTKEY_RENDER_OUTPUT="$output" PORTKEY_CLUSTER_TEMPLATE="$CLUSTER_TEMPLATE" \
    PORTKEY_CLUSTER_NAME="$PORTKEY_CLUSTER_NAME" AWS_REGION="$AWS_REGION" python3 - <<'PY'
import os
import re
from pathlib import Path
p = Path(os.environ["PORTKEY_CLUSTER_TEMPLATE"]).read_text()
replacements = {
    "__CLUSTER_NAME__": os.environ["PORTKEY_CLUSTER_NAME"],
    "__AWS_REGION__": os.environ["AWS_REGION"],
}
if set(re.findall(r"__[A-Z0-9_]+__", p)) != set(replacements):
    raise SystemExit("unexpected eksctl cluster template placeholders")
for placeholder, value in replacements.items():
    p = p.replace(placeholder, value)
Path(os.environ["PORTKEY_RENDER_OUTPUT"]).write_text(p)
PY
}

render_load_balancer_controller_service_account() {
  local output="$1" account_id="$2" vpc_id="$3" partition="$4"
  PORTKEY_RENDER_OUTPUT="$output" PORTKEY_LBC_POLICY_TEMPLATE="$LBC_POLICY_TEMPLATE" \
    PORTKEY_CLUSTER_NAME="$PORTKEY_CLUSTER_NAME" AWS_REGION="$AWS_REGION" \
    PORTKEY_AWS_ACCOUNT_ID="$account_id" PORTKEY_VPC_ID="$vpc_id" \
    PORTKEY_AWS_PARTITION="$partition" python3 - <<'PY'
import json
import os
import re
from pathlib import Path

policy_text = Path(os.environ["PORTKEY_LBC_POLICY_TEMPLATE"]).read_text()
replacements = {
    "__AWS_ACCOUNT_ID__": os.environ["PORTKEY_AWS_ACCOUNT_ID"],
    "__AWS_PARTITION__": os.environ["PORTKEY_AWS_PARTITION"],
    "__AWS_REGION__": os.environ["AWS_REGION"],
    "__VPC_ID__": os.environ["PORTKEY_VPC_ID"],
    "__CLUSTER_NAME__": os.environ["PORTKEY_CLUSTER_NAME"],
}
found_placeholders = set(re.findall(r"__[A-Z0-9_]+__", policy_text))
if found_placeholders != set(replacements):
    raise SystemExit("unexpected AWS Load Balancer Controller IAM placeholders")
for placeholder, value in replacements.items():
    policy_text = policy_text.replace(placeholder, value)
policy = json.loads(policy_text)
config = {
    "apiVersion": "eksctl.io/v1alpha5",
    "kind": "ClusterConfig",
    "metadata": {
        "name": os.environ["PORTKEY_CLUSTER_NAME"],
        "region": os.environ["AWS_REGION"],
    },
    "iam": {
        "withOIDC": True,
        "serviceAccounts": [
            {
                "metadata": {
                    "name": "aws-load-balancer-controller",
                    "namespace": "kube-system",
                    "labels": {
                        "app.kubernetes.io/managed-by": "guidance-codex",
                    },
                },
                "attachPolicy": policy,
                "tags": {"Application": "guidance-codex-portkey"},
            }
        ],
    },
}
output = Path(os.environ["PORTKEY_RENDER_OUTPUT"])
output.write_text(json.dumps(config, indent=2) + "\n")
output.chmod(0o600)
PY
}

cluster_plan() {
  require_eksctl; validate_common
  local rendered; rendered="$(mktemp)"; trap "rm -f '$rendered'" EXIT
  render_cluster "$rendered"
  eksctl create cluster --config-file "$rendered" --dry-run >/dev/null
  rm -f "$rendered"; trap - EXIT
  printf 'eksctl cluster plan is valid for %s in %s.\n' "$PORTKEY_CLUSTER_NAME" "$AWS_REGION"
}

cluster_deploy() {
  cluster_plan; load_balancer_controller_plan; confirm_write
  local rendered; rendered="$(mktemp)"; trap "rm -f '$rendered'" EXIT
  render_cluster "$rendered"
  eksctl create cluster --config-file "$rendered"
  rm -f "$rendered"; trap - EXIT
  install_load_balancer_controller
}

cluster_identity() {
  aws_cli eks describe-cluster --name "$PORTKEY_CLUSTER_NAME" --query 'cluster.identity.oidc.issuer' --output text
}

aws_check() {
  require_command aws; require_command kubectl; require_command python3; validate_common
  aws_cli sts get-caller-identity >/dev/null || die 'AWS credentials are not authenticated'
  local issuer; issuer="$(cluster_identity)"
  [[ "$issuer" == https://* ]] || die 'EKS cluster has no OIDC issuer'
  printf 'AWS identity and EKS cluster %s are available in %s.\n' "$PORTKEY_CLUSTER_NAME" "$AWS_REGION"
}

plan() {
  aws_check
  aws_cli cloudformation validate-template --template-body "file://$INFRA_TEMPLATE" >/dev/null
  printf 'CloudFormation plan is valid: S3 logs in %s + EKS IRSA access to Mantle in %s for models %s.\n' \
    "$AWS_REGION" "$BEDROCK_MANTLE_REGION" "$PORTKEY_ALLOWED_MODELS"
}

stack_exists() { aws_cli cloudformation describe-stacks --stack-name "$PORTKEY_STACK_NAME" >/dev/null 2>&1; }

deploy() {
  plan; confirm_write
  require_eksctl
  eksctl utils associate-iam-oidc-provider --cluster "$PORTKEY_CLUSTER_NAME" --region "$AWS_REGION" --approve
  aws_cli cloudformation deploy --stack-name "$PORTKEY_STACK_NAME" \
    --template-file "$INFRA_TEMPLATE" --parameter-overrides \
      MantleProjectId="$BEDROCK_MANTLE_PROJECT_ID" \
      BedrockMantleRegion="$BEDROCK_MANTLE_REGION" \
      MantleModelIds="$PORTKEY_ALLOWED_MODELS" \
    --capabilities CAPABILITY_IAM --no-fail-on-empty-changeset \
    --tags Application=guidance-codex-portkey
  local policy_arn
  policy_arn="$(stack_output GatewayManagedPolicyArn)"
  eksctl create iamserviceaccount --cluster "$PORTKEY_CLUSTER_NAME" --region "$AWS_REGION" \
    --namespace "$PORTKEY_NAMESPACE" --name "$PORTKEY_SERVICE_ACCOUNT" \
    --attach-policy-arn "$policy_arn" --approve --override-existing-serviceaccounts
}

stack_output() {
  aws_cli cloudformation describe-stacks --stack-name "$PORTKEY_STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue|[0]" --output text
}

validate_helm_secrets() {
  for name in PORTKEY_DOCKER_USERNAME PORTKEY_DOCKER_PASSWORD PORTKEY_CLIENT_AUTH PORTKEY_ORGANIZATION_ID PORTKEY_HELM_CHART_VERSION PORTKEY_GATEWAY_IMAGE_TAG PORTKEY_REDIS_IMAGE_TAG; do require_value "$name"; done
  [[ "$PORTKEY_GATEWAY_IMAGE_TAG" != latest ]] || die 'pin the Portkey gateway image tag; latest is not accepted'
  [[ "$PORTKEY_REDIS_IMAGE_TAG" =~ ^[0-9]+\.[0-9]+\.[0-9]+-alpine([0-9.]*)?$ ]] || die 'PORTKEY_REDIS_IMAGE_TAG must pin a patch release such as 7.2.10-alpine'
}

render_values() {
  local output="$1" role bucket
  validate_helm_secrets
  kube_context
  role="$(kubectl -n "$PORTKEY_NAMESPACE" get serviceaccount "$PORTKEY_SERVICE_ACCOUNT" -o jsonpath='{.metadata.annotations.eks\.amazonaws\.com/role-arn}')"
  [[ "$role" == arn:*:iam::*:role/* ]] || die 'IRSA role annotation is missing from the Portkey service account'
  bucket="$(stack_output GatewayLogBucketName)"
  PORTKEY_VALUES_OUTPUT="$output" PORTKEY_VALUES_TEMPLATE="$VALUES_TEMPLATE" \
  PORTKEY_SERVICE_ROLE_ARN="$role" PORTKEY_LOG_BUCKET="$bucket" \
  PORTKEY_NAMESPACE="$PORTKEY_NAMESPACE" PORTKEY_SERVICE_ACCOUNT="$PORTKEY_SERVICE_ACCOUNT" \
  PORTKEY_DOCKER_USERNAME="$PORTKEY_DOCKER_USERNAME" PORTKEY_DOCKER_PASSWORD="$PORTKEY_DOCKER_PASSWORD" \
  PORTKEY_CLIENT_AUTH="$PORTKEY_CLIENT_AUTH" PORTKEY_ORGANIZATION_ID="$PORTKEY_ORGANIZATION_ID" \
  PORTKEY_GATEWAY_IMAGE_TAG="$PORTKEY_GATEWAY_IMAGE_TAG" PORTKEY_REDIS_IMAGE_TAG="$PORTKEY_REDIS_IMAGE_TAG" \
  PORTKEY_INTERNAL_NLB="$PORTKEY_INTERNAL_NLB" PORTKEY_LOG_STORE_REGION="$AWS_REGION" python3 - <<'PY'
import json, os
from pathlib import Path
text = Path(os.environ["PORTKEY_VALUES_TEMPLATE"]).read_text()
mapping = {
 "PORTKEY_DOCKER_USERNAME": os.environ["PORTKEY_DOCKER_USERNAME"],
 "PORTKEY_DOCKER_PASSWORD": os.environ["PORTKEY_DOCKER_PASSWORD"],
 "PORTKEY_CLIENT_AUTH": os.environ["PORTKEY_CLIENT_AUTH"],
 "PORTKEY_ORGANIZATION_ID": os.environ["PORTKEY_ORGANIZATION_ID"],
 "PORTKEY_GATEWAY_IMAGE_TAG": os.environ["PORTKEY_GATEWAY_IMAGE_TAG"],
 "PORTKEY_REDIS_IMAGE_TAG": os.environ["PORTKEY_REDIS_IMAGE_TAG"],
 "PORTKEY_LOG_BUCKET": os.environ["PORTKEY_LOG_BUCKET"],
 "PORTKEY_LOG_STORE_REGION": os.environ["PORTKEY_LOG_STORE_REGION"],
 "PORTKEY_SERVICE_ACCOUNT": os.environ["PORTKEY_SERVICE_ACCOUNT"],
 "PORTKEY_SERVICE_ROLE_ARN": os.environ["PORTKEY_SERVICE_ROLE_ARN"],
 "PORTKEY_NLB_SCHEME": "internal" if os.environ["PORTKEY_INTERNAL_NLB"] == "true" else "internet-facing",
}
for key, value in mapping.items(): text = text.replace(f"__{key}__", json.dumps(value))
if "__PORTKEY_" in text: raise SystemExit("unresolved Portkey values placeholder")
Path(os.environ["PORTKEY_VALUES_OUTPUT"]).write_text(text)
os.chmod(os.environ["PORTKEY_VALUES_OUTPUT"], 0o600)
PY
}

kube_context() { aws_cli eks update-kubeconfig --name "$PORTKEY_CLUSTER_NAME" >/dev/null; }

load_balancer_controller_exists() {
  local resource
  resource="$(kubectl -n "$AWS_LBC_NAMESPACE" get deployment "$AWS_LBC_HELM_RELEASE" \
    --ignore-not-found -o name)" || \
    die 'could not query the AWS Load Balancer Controller deployment'
  [[ -n "$resource" ]]
}

load_balancer_controller_release_exists() {
  helm status "$AWS_LBC_HELM_RELEASE" --namespace "$AWS_LBC_NAMESPACE" >/dev/null 2>&1
}

load_balancer_controller_service_account_exists() {
  local resource
  resource="$(kubectl -n "$AWS_LBC_NAMESPACE" get serviceaccount "$AWS_LBC_SERVICE_ACCOUNT" \
    --ignore-not-found -o name)" || \
    die 'could not query the AWS Load Balancer Controller service account'
  [[ -n "$resource" ]]
}

load_balancer_controller_is_helm_owned() {
  local release_name release_namespace
  release_name="$(kubectl -n "$AWS_LBC_NAMESPACE" get deployment "$AWS_LBC_HELM_RELEASE" \
    -o jsonpath='{.metadata.annotations.meta\.helm\.sh/release-name}' 2>/dev/null || true)"
  release_namespace="$(kubectl -n "$AWS_LBC_NAMESPACE" get deployment "$AWS_LBC_HELM_RELEASE" \
    -o jsonpath='{.metadata.annotations.meta\.helm\.sh/release-namespace}' 2>/dev/null || true)"
  [[ "$release_name" == "$AWS_LBC_HELM_RELEASE" && "$release_namespace" == "$AWS_LBC_NAMESPACE" ]] && \
    load_balancer_controller_release_exists
}

load_balancer_controller_is_ready() {
  load_balancer_controller_exists && \
    kubectl -n "$AWS_LBC_NAMESPACE" rollout status \
      "deployment/$AWS_LBC_HELM_RELEASE" --timeout=2m >/dev/null 2>&1 && \
    kubectl get crd targetgroupbindings.elbv2.k8s.aws >/dev/null 2>&1
}

load_balancer_controller_is_compatible() {
  local require_exact_watch="${1:-false}" deployment_json
  deployment_json="$(kubectl -n "$AWS_LBC_NAMESPACE" get deployment \
    "$AWS_LBC_HELM_RELEASE" -o json)" || \
    die 'could not inspect the AWS Load Balancer Controller deployment'
  PORTKEY_LBC_DEPLOYMENT_JSON="$deployment_json" \
    PORTKEY_LBC_EXPECTED_VERSION="v$PORTKEY_LBC_HELM_CHART_VERSION" \
    PORTKEY_CLUSTER_NAME="$PORTKEY_CLUSTER_NAME" \
    PORTKEY_NAMESPACE="$PORTKEY_NAMESPACE" \
    PORTKEY_REQUIRE_EXACT_WATCH="$require_exact_watch" python3 - <<'PY'
import json
import os
import sys


def reject(message):
    print(f"controller compatibility check failed: {message}", file=sys.stderr)
    raise SystemExit(1)


deployment = json.loads(os.environ["PORTKEY_LBC_DEPLOYMENT_JSON"])
pod_spec = deployment.get("spec", {}).get("template", {}).get("spec", {})
containers = pod_spec.get("containers", [])
container = next(
    (item for item in containers if item.get("name") == "aws-load-balancer-controller"),
    None,
)
if not container:
    reject("the expected controller container is missing")

expected_version = os.environ["PORTKEY_LBC_EXPECTED_VERSION"]
labels = deployment.get("metadata", {}).get("labels", {})
label_version = labels.get("app.kubernetes.io/version", "")
image = container.get("image", "")
image_without_digest = image.split("@", 1)[0]
image_tag = image_without_digest.rsplit(":", 1)[-1] if ":" in image_without_digest else ""
if image_tag != expected_version:
    reject(
        f"expected image tag {expected_version}, found {image_tag!r}; "
        "Deployment labels cannot substitute for the running image version"
    )
if label_version and label_version != expected_version:
    reject(f"expected version label {expected_version}, found {label_version!r}")

args = container.get("args", [])
if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
    reject("controller arguments are malformed")


def flag_values(flag):
    values = []
    for index, argument in enumerate(args):
        if argument.startswith(flag + "="):
            values.append(argument.split("=", 1)[1])
        elif argument == flag:
            if index + 1 >= len(args):
                reject(f"{flag} has no value")
            values.append(args[index + 1])
    return values


cluster_values = flag_values("--cluster-name")
if cluster_values != [os.environ["PORTKEY_CLUSTER_NAME"]]:
    reject(
        f"--cluster-name must be exactly {os.environ['PORTKEY_CLUSTER_NAME']!r}"
    )

watch_values = [value for value in flag_values("--watch-namespace") if value]
expected_namespace = os.environ["PORTKEY_NAMESPACE"]
if os.environ["PORTKEY_REQUIRE_EXACT_WATCH"] == "true":
    if watch_values != [expected_namespace]:
        reject(f"cleanup requires --watch-namespace={expected_namespace}")
elif watch_values and watch_values != [expected_namespace]:
    reject(
        f"controller watches {watch_values!r}, not the Portkey namespace "
        f"{expected_namespace!r} or all namespaces"
    )
PY
}

require_load_balancer_controller() {
  require_command kubectl; require_command python3; validate_common; kube_context
  load_balancer_controller_exists || die 'AWS Load Balancer Controller is missing; run CONFIRM_AWS_WRITE=1 make portkey-lbc-deploy'
  load_balancer_controller_is_ready || die 'AWS Load Balancer Controller is not ready or its CRDs are missing'
  load_balancer_controller_is_compatible false || \
    die 'AWS Load Balancer Controller is ready but incompatible with this Portkey deployment'
  printf 'AWS Load Balancer Controller is ready.\n'
}

require_safe_nlb_service_upgrade() {
  local current_scheme current_type service_resource
  service_resource="$(kubectl -n "$PORTKEY_NAMESPACE" get service "$PORTKEY_GATEWAY_SERVICE" \
    --ignore-not-found -o name)" || die 'could not query the existing Portkey gateway Service'
  if [[ -z "$service_resource" ]]; then
    return
  fi
  current_type="$(kubectl -n "$PORTKEY_NAMESPACE" get service "$PORTKEY_GATEWAY_SERVICE" \
    -o jsonpath='{.metadata.annotations.service\.beta\.kubernetes\.io/aws-load-balancer-type}')" || \
    die 'could not read the existing Portkey gateway Service load-balancer type'
  current_scheme="$(kubectl -n "$PORTKEY_NAMESPACE" get service "$PORTKEY_GATEWAY_SERVICE" \
    -o jsonpath='{.metadata.annotations.service\.beta\.kubernetes\.io/aws-load-balancer-scheme}')" || \
    die 'could not read the existing Portkey gateway Service load-balancer scheme'
  if [[ "$current_type" != external || "$current_scheme" != internal ]]; then
    die "existing service $PORTKEY_NAMESPACE/$PORTKEY_GATEWAY_SERVICE uses load-balancer type '$current_type' and scheme '$current_scheme'; delete that Service, confirm its old load balancer is gone, then rerun helm-deploy"
  fi
}

load_balancer_controller_plan() {
  require_command helm; require_command python3; validate_common
  local rendered; rendered="$(mktemp)"; trap "rm -f '$rendered'" EXIT
  render_load_balancer_controller_service_account \
    "$rendered" 123456789012 vpc-0123456789abcdef0 \
    "$(aws_partition_for_region "$AWS_REGION")"
  python3 -m json.tool "$rendered" >/dev/null
  helm repo add eks https://aws.github.io/eks-charts --force-update >/dev/null
  helm repo update eks >/dev/null
  helm template "$AWS_LBC_HELM_RELEASE" eks/aws-load-balancer-controller \
    --version "$PORTKEY_LBC_HELM_CHART_VERSION" --namespace "$AWS_LBC_NAMESPACE" \
    --set clusterName="$PORTKEY_CLUSTER_NAME" \
    --set watchNamespace="$PORTKEY_NAMESPACE" \
    --set enableServiceMutatorWebhook=false \
    --set controllerConfig.featureGates.ListenerRulesTagging=true \
    --set enableShield=false --set enableWaf=false --set enableWafv2=false \
    --set serviceAccount.create=false \
    --set serviceAccount.name=aws-load-balancer-controller >/dev/null
  rm -f "$rendered"; trap - EXIT
  printf 'AWS Load Balancer Controller chart %s renders successfully.\n' "$PORTKEY_LBC_HELM_CHART_VERSION"
}

install_load_balancer_controller() {
  confirm_write; require_command aws; require_eksctl; require_command helm; require_command kubectl
  aws_check; kube_context
  if load_balancer_controller_exists; then
    if load_balancer_controller_is_ready; then
      load_balancer_controller_is_compatible false || \
        die 'refusing to reuse an incompatible AWS Load Balancer Controller deployment'
      printf 'Using the existing ready AWS Load Balancer Controller deployment.\n'
      return
    fi
    if ! load_balancer_controller_is_helm_owned || \
      ! load_balancer_controller_service_account_is_managed; then
      die 'an unready AWS Load Balancer Controller deployment exists but is not owned by this walkthrough; repair it with the cluster owner before retrying'
    fi
    printf 'Retrying the existing walkthrough-managed AWS Load Balancer Controller release.\n'
  fi

  local account_id partition rendered vpc_id
  load_balancer_controller_plan
  account_id="$(aws_cli sts get-caller-identity --query Account --output text)"
  [[ "$account_id" =~ ^[0-9]{12}$ ]] || die 'could not resolve the AWS account ID'
  vpc_id="$(aws_cli eks describe-cluster --name "$PORTKEY_CLUSTER_NAME" \
    --query 'cluster.resourcesVpcConfig.vpcId' --output text)"
  [[ "$vpc_id" =~ ^vpc-[0-9a-f]+$ ]] || die 'could not resolve the EKS cluster VPC ID'
  partition="$(aws_partition_for_region "$AWS_REGION")"
  rendered="$(mktemp)"; trap "rm -f '$rendered'" EXIT
  render_load_balancer_controller_service_account "$rendered" "$account_id" "$vpc_id" "$partition"
  if load_balancer_controller_service_account_exists; then
    load_balancer_controller_service_account_is_managed || \
      die 'refusing to overwrite an existing AWS Load Balancer Controller service account that is not managed by guidance-codex'
  else
    eksctl utils associate-iam-oidc-provider \
      --cluster "$PORTKEY_CLUSTER_NAME" --region "$AWS_REGION" --approve
    eksctl create iamserviceaccount --config-file "$rendered" \
      --include "$AWS_LBC_NAMESPACE/$AWS_LBC_SERVICE_ACCOUNT" \
      --approve
  fi

  helm upgrade --install "$AWS_LBC_HELM_RELEASE" eks/aws-load-balancer-controller \
    --version "$PORTKEY_LBC_HELM_CHART_VERSION" --namespace "$AWS_LBC_NAMESPACE" \
    --set clusterName="$PORTKEY_CLUSTER_NAME" --set region="$AWS_REGION" \
    --set vpcId="$vpc_id" --set watchNamespace="$PORTKEY_NAMESPACE" \
    --set enableServiceMutatorWebhook=false \
    --set controllerConfig.featureGates.ListenerRulesTagging=true \
    --set enableShield=false --set enableWaf=false --set enableWafv2=false \
    --set serviceAccount.create=false \
    --set serviceAccount.name=aws-load-balancer-controller --wait --timeout 10m
  kubectl -n "$AWS_LBC_NAMESPACE" rollout status \
    "deployment/$AWS_LBC_HELM_RELEASE" --timeout=5m
  kubectl get crd targetgroupbindings.elbv2.k8s.aws >/dev/null
  rm -f "$rendered"; trap - EXIT
}

load_balancer_controller_service_account_is_managed() {
  local managed_by role
  managed_by="$(kubectl -n "$AWS_LBC_NAMESPACE" get serviceaccount "$AWS_LBC_SERVICE_ACCOUNT" \
    -o jsonpath='{.metadata.labels.app\.kubernetes\.io/managed-by}' 2>/dev/null || true)"
  role="$(kubectl -n "$AWS_LBC_NAMESPACE" get serviceaccount "$AWS_LBC_SERVICE_ACCOUNT" \
    -o jsonpath='{.metadata.annotations.eks\.amazonaws\.com/role-arn}' 2>/dev/null || true)"
  [[ "$managed_by" == guidance-codex && "$role" == arn:*:iam::*:role/* ]]
}

require_no_load_balancer_controller_dependents() {
  local namespace_objects gateway_api_resources gateways
  namespace_objects="$(kubectl -n "$PORTKEY_NAMESPACE" get \
    services,ingresses.networking.k8s.io --ignore-not-found -o json)" || \
    die 'could not inspect Portkey namespace Services and Ingresses'
  PORTKEY_NAMESPACE_OBJECTS="$namespace_objects" python3 - <<'PY' || \
    die 'Portkey namespace still contains AWS Load Balancer Controller dependencies'
import json
import os
import sys

payload = json.loads(os.environ["PORTKEY_NAMESPACE_OBJECTS"])
items = payload.get("items")
if not isinstance(items, list):
    raise SystemExit("Kubernetes dependency list is malformed")
dependencies = []
for item in items:
    kind = item.get("kind", "")
    metadata = item.get("metadata", {})
    name = metadata.get("name", "unknown")
    if kind == "Service" and item.get("spec", {}).get("type") == "LoadBalancer":
        dependencies.append(f"Service/{name}")
    elif kind == "Ingress":
        dependencies.append(f"Ingress/{name}")
if dependencies:
    print("controller dependencies remain: " + ", ".join(dependencies), file=sys.stderr)
    raise SystemExit(1)
PY

  gateway_api_resources="$(kubectl api-resources \
    --api-group=gateway.networking.k8s.io --namespaced=true -o name)" || \
    die 'could not inspect Gateway API resource availability'
  if [[ "$gateway_api_resources" == *gateways.gateway.networking.k8s.io* ]]; then
    gateways="$(kubectl -n "$PORTKEY_NAMESPACE" get \
      gateways.gateway.networking.k8s.io -o name)" || \
      die 'could not inspect Portkey namespace Gateways'
    [[ -z "$gateways" ]] || \
      die 'Portkey namespace still contains Gateway API resources that may depend on the controller'
  fi
}

load_balancer_controller_cleanup_plan() {
  require_command aws; require_eksctl; require_command helm; require_command kubectl
  aws_check; kube_context

  local bindings deployment_present=false release_present=false service_account_present=false
  if load_balancer_controller_exists; then deployment_present=true; fi
  if load_balancer_controller_release_exists; then release_present=true; fi
  if load_balancer_controller_service_account_exists; then
    service_account_present=true
  fi

  if [[ "$deployment_present" == true ]] && ! load_balancer_controller_is_helm_owned; then
    die 'refusing to remove an AWS Load Balancer Controller deployment that is not owned by the expected Helm release'
  fi
  if [[ "$deployment_present" == true ]]; then
    load_balancer_controller_is_compatible true || \
      die 'refusing to remove a controller that is not scoped exactly to the Portkey namespace'
  fi
  if [[ "$service_account_present" == true ]] && ! load_balancer_controller_service_account_is_managed; then
    die 'refusing to remove an AWS Load Balancer Controller service account that is not managed by guidance-codex'
  fi
  if [[ "$release_present" == true && "$service_account_present" == false ]]; then
    die 'refusing to remove an AWS Load Balancer Controller release without the walkthrough-managed service account ownership marker'
  fi
  if [[ "$release_present" == false && "$service_account_present" == false ]]; then
    printf 'No walkthrough-managed AWS Load Balancer Controller resources were found.\n'
    return
  fi

  kubectl get crd targetgroupbindings.elbv2.k8s.aws >/dev/null 2>&1 || \
    die 'cannot prove controller cleanup is safe because the TargetGroupBinding CRD is missing or unreadable'
  bindings="$(kubectl get targetgroupbindings.elbv2.k8s.aws --all-namespaces -o name)" || \
    die 'could not verify AWS Load Balancer Controller dependencies'
  [[ -z "$bindings" ]] || \
    die 'AWS Load Balancer Controller still has TargetGroupBinding dependencies; remove their Services or Ingresses and wait for AWS cleanup before retrying'
  require_no_load_balancer_controller_dependents
  printf 'AWS Load Balancer Controller cleanup is safe for cluster %s; no controller dependencies remain.\n' "$PORTKEY_CLUSTER_NAME"
}

load_balancer_controller_cleanup() {
  load_balancer_controller_cleanup_plan
  [[ "${CONFIRM_LBC_DELETE:-}" == "$PORTKEY_CLUSTER_NAME" ]] || \
    die "set CONFIRM_LBC_DELETE=$PORTKEY_CLUSTER_NAME to remove the walkthrough-managed controller"
  if load_balancer_controller_release_exists; then
    helm uninstall "$AWS_LBC_HELM_RELEASE" --namespace "$AWS_LBC_NAMESPACE" --wait --timeout 10m
  fi
  if load_balancer_controller_service_account_is_managed; then
    eksctl delete iamserviceaccount --cluster "$PORTKEY_CLUSTER_NAME" --region "$AWS_REGION" \
      --namespace "$AWS_LBC_NAMESPACE" --name "$AWS_LBC_SERVICE_ACCOUNT" --wait
  fi
}

helm_plan() {
  require_command helm; stack_exists || die 'deploy the Portkey AWS stack first'
  local values; values="$(mktemp)"; trap "rm -f '$values'" EXIT
  render_values "$values"
  helm repo add portkey-ai https://portkey-ai.github.io/helm --force-update >/dev/null
  helm repo update portkey-ai >/dev/null
  helm template "$PORTKEY_HELM_RELEASE" portkey-ai/gateway --version "$PORTKEY_HELM_CHART_VERSION" --namespace "$PORTKEY_NAMESPACE" -f "$values" >/dev/null
  rm -f "$values"; trap - EXIT
  printf 'Portkey Helm release renders successfully; secrets were held in a mode-0600 temporary file.\n'
}

helm_deploy() {
  helm_plan; confirm_write; require_command kubectl; kube_context
  require_load_balancer_controller
  require_safe_nlb_service_upgrade
  local values; values="$(mktemp)"; trap "rm -f '$values'" EXIT
  render_values "$values"
  helm upgrade --install "$PORTKEY_HELM_RELEASE" portkey-ai/gateway --version "$PORTKEY_HELM_CHART_VERSION" \
    --namespace "$PORTKEY_NAMESPACE" --create-namespace -f "$values" --wait --timeout 15m
  rm -f "$values"; trap - EXIT
}

gateway_hostname() {
  kubectl -n "$PORTKEY_NAMESPACE" get service "$PORTKEY_GATEWAY_SERVICE" \
    -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true
}

status() {
  aws_check; kube_context
  stack_exists || die 'Portkey AWS stack is not deployed'
  aws_cli cloudformation describe-stacks --stack-name "$PORTKEY_STACK_NAME" --query 'Stacks[0].StackStatus' --output text
  kubectl -n "$PORTKEY_NAMESPACE" get pods,service
  local host; host="$(gateway_hostname)"
  [[ -z "$host" ]] || printf 'Internal NLB (plaintext; not accepted as a Codex base URL): http://%s/v1\n' "$host"
}

validate_target() {
  local selected_model_id
  require_value PORTKEY_PROVIDER_SLUG; require_value PORTKEY_MODEL; require_value PORTKEY_API_KEY
  [[ "$PORTKEY_PROVIDER_SLUG" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]] || die 'invalid PORTKEY_PROVIDER_SLUG'
  [[ "$PORTKEY_MODEL" == "@$PORTKEY_PROVIDER_SLUG/"* ]] || \
    die 'PORTKEY_MODEL must use @<PORTKEY_PROVIDER_SLUG>/<allowed-model-id>'
  selected_model_id="${PORTKEY_MODEL#@$PORTKEY_PROVIDER_SLUG/}"
  [[ "$selected_model_id" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$ ]] || \
    die 'PORTKEY_MODEL contains an invalid upstream model ID'
  model_is_allowed "$selected_model_id" || \
    die 'PORTKEY_MODEL must select a model listed in PORTKEY_ALLOWED_MODELS'
  [[ -z "$PORTKEY_BASE_URL" || "$PORTKEY_BASE_URL" == */v1 ]] || die 'PORTKEY_BASE_URL must end in /v1'
  [[ -z "$PORTKEY_BASE_URL" || "$PORTKEY_BASE_URL" == https://* ]] || die 'PORTKEY_BASE_URL must use https; leave it empty to validate through local kubectl port-forward'
}

check() { require_command python3; validate_common; validate_target; printf 'Portkey Hybrid Codex configuration is valid.\n'; }

prepare_runtime_url() {
  if [[ -n "$PORTKEY_BASE_URL" ]]; then RUNTIME_URL="$PORTKEY_BASE_URL"; return; fi
  require_command kubectl; kube_context
  kubectl -n "$PORTKEY_NAMESPACE" port-forward "service/$PORTKEY_GATEWAY_SERVICE" 18787:80 >/dev/null 2>&1 &
  TUNNEL_PID=$!; sleep 2
  kill -0 "$TUNNEL_PID" >/dev/null 2>&1 || die 'kubectl port-forward failed'
  RUNTIME_URL=http://127.0.0.1:18787/v1
}
stop_tunnel() { [[ -z "${TUNNEL_PID:-}" ]] || kill "$TUNNEL_PID" >/dev/null 2>&1 || true; }

codex_config() {
  check; require_value PORTKEY_BASE_URL
  cat <<EOF
model_provider = "portkey"
model = "$PORTKEY_MODEL"

[model_providers.portkey]
name = "Portkey Hybrid on AWS"
base_url = "$PORTKEY_BASE_URL"
env_key = "PORTKEY_API_KEY"
wire_api = "responses"
env_http_headers = { "x-portkey-api-key" = "PORTKEY_API_KEY" }
EOF
}

validate() {
  local model_id qualified_model
  check; prepare_runtime_url; trap stop_tunnel EXIT
  for model_id in "${PORTKEY_ALLOWED_MODEL_IDS[@]}"; do
    qualified_model="@$PORTKEY_PROVIDER_SLUG/$model_id"
    printf 'Validating Codex Responses contract for %s.\n' "$qualified_model"
    PORTKEY_API_KEY="$PORTKEY_API_KEY" GATEWAY_BASE_URL="$RUNTIME_URL" \
      GATEWAY_MODEL="$qualified_model" python3 "$CONTRACT_PROBE" \
      --api-key-env PORTKEY_API_KEY --header-env x-portkey-api-key=PORTKEY_API_KEY \
      --expected-model "$model_id" --require-model-listed \
      --model-list-attempts 7 --model-list-delay 10 \
      --require-reasoning --include-tool-call
  done
}

codex_validate() {
  check; require_command codex; local fixture output
  prepare_runtime_url; fixture="$(mktemp -d)"; output="$fixture/final.txt"; trap "stop_tunnel; rm -rf '$fixture'" EXIT
  printf 'PORTKEY_E2E_INPUT\n' >"$fixture/input.txt"
  PORTKEY_API_KEY="$PORTKEY_API_KEY" codex exec \
    --ignore-user-config --ephemeral --skip-git-repo-check --cd "$fixture" --sandbox workspace-write \
    --model "$PORTKEY_MODEL" --config 'model_provider="portkey"' \
    --config 'model_providers.portkey.name="Portkey Hybrid on AWS"' \
    --config "model_providers.portkey.base_url=\"$RUNTIME_URL\"" \
    --config 'model_providers.portkey.env_key="PORTKEY_API_KEY"' \
    --config 'model_providers.portkey.env_http_headers={"x-portkey-api-key"="PORTKEY_API_KEY"}' \
    --config 'model_providers.portkey.wire_api="responses"' \
    --config 'shell_environment_policy.inherit="core"' \
    --config 'shell_environment_policy.ignore_default_excludes=false' \
    --output-last-message "$output" - <<'EOF'
Read input.txt with a local tool. Create sentinel.txt containing exactly
PORTKEY_CODEX_E2E_OK followed by a newline, then reply with exactly PORTKEY_CODEX_E2E_OK.
EOF
  [[ "$(cat "$fixture/sentinel.txt" 2>/dev/null)" == PORTKEY_CODEX_E2E_OK ]] || die 'Codex did not create the sentinel'
  [[ "$(tr -d '\r\n' <"$output")" == PORTKEY_CODEX_E2E_OK ]] || die 'Codex final response was unexpected'
}

auth_negative() {
  check; prepare_runtime_url; trap stop_tunnel EXIT
  PORTKEY_NEGATIVE_URL="$RUNTIME_URL/responses" PORTKEY_NEGATIVE_MODEL="$PORTKEY_MODEL" python3 - <<'PY'
import json, os, urllib.error, urllib.request
r=urllib.request.Request(os.environ['PORTKEY_NEGATIVE_URL'], data=json.dumps({'model':os.environ['PORTKEY_NEGATIVE_MODEL'],'input':'auth check'}).encode(), headers={'Authorization':'Bearer intentionally-invalid','x-portkey-api-key':'intentionally-invalid','Content-Type':'application/json'}, method='POST')
try: urllib.request.urlopen(r, timeout=30)
except urllib.error.HTTPError as e:
  if e.code not in (401,403): raise SystemExit(f'unexpected HTTP status: {e.code}')
  print(f'Invalid Portkey key rejected with HTTP {e.code}.')
else: raise SystemExit('invalid Portkey key was accepted')
PY
}

cleanup_plan() { status; printf 'Cleanup removes Helm release %s and stack %s; the S3 log bucket is retained.\n' "$PORTKEY_HELM_RELEASE" "$PORTKEY_STACK_NAME"; }
cleanup() {
  cleanup_plan; [[ "${CONFIRM_STACK_DELETE:-}" == "$PORTKEY_STACK_NAME" ]] || die "set CONFIRM_STACK_DELETE=$PORTKEY_STACK_NAME"
  helm uninstall "$PORTKEY_HELM_RELEASE" -n "$PORTKEY_NAMESPACE" --wait
  eksctl delete iamserviceaccount --cluster "$PORTKEY_CLUSTER_NAME" --region "$AWS_REGION" \
    --namespace "$PORTKEY_NAMESPACE" --name "$PORTKEY_SERVICE_ACCOUNT" --wait
  aws_cli cloudformation delete-stack --stack-name "$PORTKEY_STACK_NAME"
  aws_cli cloudformation wait stack-delete-complete --stack-name "$PORTKEY_STACK_NAME"
}
cluster_cleanup() {
  require_eksctl; [[ "${CONFIRM_CLUSTER_DELETE:-}" == "$PORTKEY_CLUSTER_NAME" ]] || die "set CONFIRM_CLUSTER_DELETE=$PORTKEY_CLUSTER_NAME"
  eksctl delete cluster --name "$PORTKEY_CLUSTER_NAME" --region "$AWS_REGION" --wait
}

case "${1:-help}" in
  cluster-plan) cluster_plan ;; cluster-deploy) cluster_deploy ;; aws-check) aws_check ;;
  lbc-plan) load_balancer_controller_plan ;; lbc-deploy) install_load_balancer_controller ;; lbc-status) require_load_balancer_controller ;;
  lbc-cleanup-plan) load_balancer_controller_cleanup_plan ;; lbc-cleanup) load_balancer_controller_cleanup ;;
  plan) plan ;; deploy) deploy ;; helm-plan) helm_plan ;; helm-deploy) helm_deploy ;;
  status) status ;; check) check ;; codex-config) codex_config ;; validate) validate ;;
  codex-validate) codex_validate ;; auth-negative) auth_negative ;;
  cleanup-plan) cleanup_plan ;; cleanup) cleanup ;; cluster-cleanup) cluster_cleanup ;;
  help|-h|--help) help_text ;; *) die "unknown command: $1" ;;
esac
