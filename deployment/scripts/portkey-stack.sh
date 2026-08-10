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
SUPPORTED_EKSCTL_LBC_RECONCILIATION_VERSION=0.229.0
SUPPORTED_BEDROCK_MANTLE_REGIONS=(
  ap-northeast-1 ap-south-1 ap-southeast-2 ap-southeast-3
  eu-central-1 eu-north-1 eu-south-1 eu-west-1 eu-west-2
  sa-east-1 us-east-1 us-east-2 us-gov-west-1 us-west-2
)
PORTKEY_BASE_URL="${PORTKEY_BASE_URL:-}"
PORTKEY_GATEWAY_HOSTNAME="${PORTKEY_GATEWAY_HOSTNAME:-}"
PORTKEY_NLB_TLS_CERTIFICATE_ARN="${PORTKEY_NLB_TLS_CERTIFICATE_ARN:-}"
PORTKEY_NLB_ALLOWED_PREFIX_LIST_IDS="${PORTKEY_NLB_ALLOWED_PREFIX_LIST_IDS:-}"
PORTKEY_NLB_ALLOWED_PREFIX_LIST_ID_ARRAY=()
CONFIRM_PORTKEY_NLB_TLS_UPDATE="${CONFIRM_PORTKEY_NLB_TLS_UPDATE:-}"
PORTKEY_EXTERNAL_LBC_POLICY_CONFIRMED="${PORTKEY_EXTERNAL_LBC_POLICY_CONFIRMED:-false}"
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

parse_nlb_allowed_prefix_list_ids() {
  local raw="$PORTKEY_NLB_ALLOWED_PREFIX_LIST_IDS" prefix_list_id i j
  [[ ${#raw} -le 1024 ]] || \
    die 'PORTKEY_NLB_ALLOWED_PREFIX_LIST_IDS must not exceed 1024 characters'
  [[ -n "$raw" && "$raw" != ,* && "$raw" != *, && "$raw" != *,,* ]] || \
    die 'PORTKEY_NLB_ALLOWED_PREFIX_LIST_IDS must contain one or more comma-separated prefix-list IDs'
  [[ "$raw" != *[[:space:]]* ]] || \
    die 'PORTKEY_NLB_ALLOWED_PREFIX_LIST_IDS must not contain whitespace'
  IFS=',' read -r -a PORTKEY_NLB_ALLOWED_PREFIX_LIST_ID_ARRAY <<<"$raw"
  (( ${#PORTKEY_NLB_ALLOWED_PREFIX_LIST_ID_ARRAY[@]} >= 1 && \
     ${#PORTKEY_NLB_ALLOWED_PREFIX_LIST_ID_ARRAY[@]} <= 20 )) || \
    die 'PORTKEY_NLB_ALLOWED_PREFIX_LIST_IDS must contain between one and twenty IDs'
  for ((i=0; i<${#PORTKEY_NLB_ALLOWED_PREFIX_LIST_ID_ARRAY[@]}; i++)); do
    prefix_list_id="${PORTKEY_NLB_ALLOWED_PREFIX_LIST_ID_ARRAY[$i]}"
    [[ "$prefix_list_id" =~ ^pl-[0-9a-f]{8,17}$ ]] || \
      die 'PORTKEY_NLB_ALLOWED_PREFIX_LIST_IDS contains an invalid prefix-list ID'
    for ((j=0; j<i; j++)); do
      [[ "$prefix_list_id" != "${PORTKEY_NLB_ALLOWED_PREFIX_LIST_ID_ARRAY[$j]}" ]] || \
        die 'PORTKEY_NLB_ALLOWED_PREFIX_LIST_IDS contains a duplicate prefix-list ID'
    done
  done
}

validate_nlb_tls_static() {
  local expected_partition
  require_command python3
  require_value PORTKEY_GATEWAY_HOSTNAME
  require_value PORTKEY_NLB_TLS_CERTIFICATE_ARN
  require_value PORTKEY_NLB_ALLOWED_PREFIX_LIST_IDS
  expected_partition="$(aws_partition_for_region "$AWS_REGION")"
  [[ "$PORTKEY_NLB_TLS_CERTIFICATE_ARN" =~ ^arn:([^:]+):acm:([^:]+):([0-9]{12}):certificate/([A-Za-z0-9_-]+)$ ]] || \
    die 'PORTKEY_NLB_TLS_CERTIFICATE_ARN must contain exactly one valid ACM certificate ARN'
  [[ "${BASH_REMATCH[1]}" == "$expected_partition" ]] || \
    die 'PORTKEY_NLB_TLS_CERTIFICATE_ARN must use the AWS_REGION partition'
  [[ "${BASH_REMATCH[2]}" == "$AWS_REGION" ]] || \
    die 'PORTKEY_NLB_TLS_CERTIFICATE_ARN must be in AWS_REGION'
  parse_nlb_allowed_prefix_list_ids
  PORTKEY_VALIDATE_GATEWAY_HOSTNAME="$PORTKEY_GATEWAY_HOSTNAME" \
    PORTKEY_VALIDATE_BASE_URL="$PORTKEY_BASE_URL" python3 - <<'PY' || \
    die 'PORTKEY_GATEWAY_HOSTNAME or PORTKEY_BASE_URL is invalid or inconsistent'
import ipaddress
import os
import re
from urllib.parse import urlsplit

hostname = os.environ["PORTKEY_VALIDATE_GATEWAY_HOSTNAME"]
if len(hostname) > 253 or hostname.endswith(".") or "." not in hostname:
    raise SystemExit(1)
try:
    ipaddress.ip_address(hostname)
except ValueError:
    pass
else:
    raise SystemExit(1)
labels = hostname.split(".")
label = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
if any(not label.fullmatch(item) for item in labels):
    raise SystemExit(1)

base_url = os.environ["PORTKEY_VALIDATE_BASE_URL"]
if base_url:
    parsed = urlsplit(base_url)
    try:
        port = parsed.port
    except ValueError:
        raise SystemExit(1)
    if (
        parsed.scheme != "https"
        or parsed.hostname != hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.path != "/v1"
        or parsed.query
        or parsed.fragment
    ):
        raise SystemExit(1)
PY
}

validate_nlb_tls_aws() {
  require_command aws
  validate_nlb_tls_static
  local account_id certificate_account certificate_json prefix_lists_json entries_file
  account_id="$(aws_cli sts get-caller-identity --query Account --output text)" || \
    die 'could not verify the AWS account for NLB TLS resources'
  [[ "$account_id" =~ ^[0-9]{12}$ ]] || \
    die 'could not verify the AWS account for NLB TLS resources'
  [[ "$PORTKEY_NLB_TLS_CERTIFICATE_ARN" =~ ^arn:[^:]+:acm:[^:]+:([0-9]{12}):certificate/ ]] || \
    die 'PORTKEY_NLB_TLS_CERTIFICATE_ARN is malformed'
  certificate_account="${BASH_REMATCH[1]}"
  [[ "$certificate_account" == "$account_id" ]] || \
    die 'PORTKEY_NLB_TLS_CERTIFICATE_ARN must belong to the authenticated AWS account'

  certificate_json="$(aws_cli acm describe-certificate \
    --certificate-arn "$PORTKEY_NLB_TLS_CERTIFICATE_ARN" \
    --query 'Certificate.{Status:Status,DomainName:DomainName,SubjectAlternativeNames:SubjectAlternativeNames}' \
    --output json)" || die 'could not validate the configured ACM certificate'
  PORTKEY_CERTIFICATE_JSON="$certificate_json" \
    PORTKEY_VALIDATE_GATEWAY_HOSTNAME="$PORTKEY_GATEWAY_HOSTNAME" python3 - <<'PY' || \
    die 'the ACM certificate must be ISSUED and cover PORTKEY_GATEWAY_HOSTNAME'
import json
import os

certificate = json.loads(os.environ["PORTKEY_CERTIFICATE_JSON"])
if certificate.get("Status") != "ISSUED":
    raise SystemExit(1)
names = set(certificate.get("SubjectAlternativeNames") or [])
domain_name = certificate.get("DomainName")
if domain_name:
    names.add(domain_name)
hostname = os.environ["PORTKEY_VALIDATE_GATEWAY_HOSTNAME"].lower()


def covers(name):
    name = name.lower()
    if name == hostname:
        return True
    if name.startswith("*."):
        suffix = name[2:]
        return hostname.endswith("." + suffix) and hostname.count(".") == suffix.count(".") + 1
    return False


if not any(covers(name) for name in names if isinstance(name, str)):
    raise SystemExit(1)
PY

  prefix_lists_json="$(aws_cli ec2 describe-managed-prefix-lists \
    --prefix-list-ids "${PORTKEY_NLB_ALLOWED_PREFIX_LIST_ID_ARRAY[@]}" \
    --output json)" || die 'could not validate the configured managed prefix lists'
  PORTKEY_PREFIX_LISTS_JSON="$prefix_lists_json" \
    PORTKEY_PREFIX_LIST_IDS="$PORTKEY_NLB_ALLOWED_PREFIX_LIST_IDS" \
    PORTKEY_AWS_ACCOUNT_ID="$account_id" python3 - <<'PY' || \
    die 'each allowed prefix list must be customer-managed, active, IPv4, and together use at most 60 security-group rule slots by MaxEntries'
import json
import os

payload = json.loads(os.environ["PORTKEY_PREFIX_LISTS_JSON"])
requested = set(os.environ["PORTKEY_PREFIX_LIST_IDS"].split(","))
items = payload.get("PrefixLists")
if not isinstance(items, list) or len(items) != len(requested):
    raise SystemExit(1)
seen = set()
active_states = {"create-complete", "modify-complete", "restore-complete"}
aggregate_weight = 0
for item in items:
    prefix_list_id = item.get("PrefixListId")
    max_entries = item.get("MaxEntries")
    if (
        prefix_list_id not in requested
        or prefix_list_id in seen
        or item.get("OwnerId") != os.environ["PORTKEY_AWS_ACCOUNT_ID"]
        or item.get("AddressFamily") != "IPv4"
        or item.get("State") not in active_states
        or not isinstance(max_entries, int)
        or isinstance(max_entries, bool)
        or max_entries < 1
    ):
        raise SystemExit(1)
    aggregate_weight += max_entries
    seen.add(prefix_list_id)
if seen != requested:
    raise SystemExit(1)
if aggregate_weight > 60:
    raise SystemExit(1)
PY

  (
    entries_file="$(mktemp)"
    chmod 600 "$entries_file"
    trap "rm -f '$entries_file'" EXIT
    local prefix_list_id entries_json
    for prefix_list_id in "${PORTKEY_NLB_ALLOWED_PREFIX_LIST_ID_ARRAY[@]}"; do
      entries_json="$(aws_cli ec2 get-managed-prefix-list-entries \
        --prefix-list-id "$prefix_list_id" --output json)" || \
        die 'could not inspect the entries in an allowed managed prefix list'
      printf '%s\n' "$entries_json" >>"$entries_file"
    done
    python3 - "$entries_file" <<'PY' || \
      die 'allowed prefix-list entries must be valid IPv4 networks and must not cover the entire IPv4 address space'
import ipaddress
import json
import sys

decoder = json.JSONDecoder()
text = open(sys.argv[1], encoding="utf-8").read()
offset = 0
networks = []
documents = 0
while offset < len(text):
    while offset < len(text) and text[offset].isspace():
        offset += 1
    if offset == len(text):
        break
    payload, offset = decoder.raw_decode(text, offset)
    documents += 1
    entries = payload.get("Entries")
    if not isinstance(entries, list) or not entries:
        raise SystemExit(1)
    for entry in entries:
        try:
            network = ipaddress.ip_network(entry.get("Cidr", ""), strict=True)
        except ValueError:
            raise SystemExit(1)
        if network.version != 4:
            raise SystemExit(1)
        networks.append(network)
if documents == 0 or not networks:
    raise SystemExit(1)
collapsed = list(ipaddress.collapse_addresses(networks))
if any(network.version != 4 or network.prefixlen == 0 for network in collapsed):
    raise SystemExit(1)
PY
  )
}
require_eksctl() {
  require_command eksctl; require_command python3
  local version
  version="$(eksctl version 2>/dev/null | head -n 1)"
  PORTKEY_EKSCTL_VERSION="$version" PORTKEY_EKSCTL_MIN_VERSION="$EKSCTL_MIN_VERSION" python3 - <<'PY' || \
    die "eksctl $EKSCTL_MIN_VERSION or newer is required"
import os, re
def parsed(name):
    match = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", os.environ[name].strip())
    if not match:
        raise SystemExit(1)
    return tuple(map(int, match.groups()))
raise SystemExit(
    0
    if parsed("PORTKEY_EKSCTL_VERSION")
    >= parsed("PORTKEY_EKSCTL_MIN_VERSION")
    else 1
)
PY
}

require_eksctl_lbc_reconciliation_version() {
  require_eksctl
  local version
  version="$(eksctl version 2>/dev/null | head -n 1)"
  PORTKEY_EKSCTL_VERSION="$version" \
    PORTKEY_SUPPORTED_EKSCTL_VERSION="$SUPPORTED_EKSCTL_LBC_RECONCILIATION_VERSION" \
    python3 - <<'PY' || \
    die "eksctl $SUPPORTED_EKSCTL_LBC_RECONCILIATION_VERSION exactly is required before this workflow creates or updates its walkthrough-managed controller IAM stack"
import os
import re


def parsed(name):
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", os.environ[name].strip())
    if not match:
        raise SystemExit(1)
    return tuple(map(int, match.groups()))


raise SystemExit(
    0
    if parsed("PORTKEY_EKSCTL_VERSION")
    == parsed("PORTKEY_SUPPORTED_EKSCTL_VERSION")
    else 1
)
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
  [[ "$PORTKEY_INTERNAL_NLB" == true ]] || die 'PORTKEY_INTERNAL_NLB must remain true; this walkthrough does not expose Portkey API keys or prompts through a public load balancer'
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
  cluster_plan; load_balancer_controller_plan
  require_eksctl_lbc_reconciliation_version
  # Validate every external TLS dependency before creating an EKS cluster. This
  # keeps a typo, unusable certificate, or unsafe prefix list fail-before-write.
  validate_nlb_tls_aws
  confirm_write
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
  validate_nlb_tls_aws
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
  PORTKEY_NLB_TLS_CERTIFICATE_ARN="$PORTKEY_NLB_TLS_CERTIFICATE_ARN" \
  PORTKEY_NLB_ALLOWED_PREFIX_LIST_IDS="$PORTKEY_NLB_ALLOWED_PREFIX_LIST_IDS" \
  PORTKEY_LOG_STORE_REGION="$AWS_REGION" python3 - <<'PY'
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
 "PORTKEY_NLB_TLS_CERTIFICATE_ARN": os.environ["PORTKEY_NLB_TLS_CERTIFICATE_ARN"],
 "PORTKEY_NLB_ALLOWED_PREFIX_LIST_IDS": os.environ["PORTKEY_NLB_ALLOWED_PREFIX_LIST_IDS"],
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

load_balancer_controller_role_arn() {
  kubectl -n "$AWS_LBC_NAMESPACE" get serviceaccount "$AWS_LBC_SERVICE_ACCOUNT" \
    -o jsonpath='{.metadata.annotations.eks\.amazonaws\.com/role-arn}' 2>/dev/null || true
}

require_load_balancer_controller_tls_permissions() (
  require_command aws; require_command python3
  local verification_mode="${1:-strict}"
  [[ "$verification_mode" == strict || "$verification_mode" == external ]] || \
    die 'invalid AWS Load Balancer Controller IAM verification mode'
  local account_id attached_arns_json default_version documents_file inline_names_json oidc_issuer role_json
  local policy_arn policy_document policy_name role_account role_arn role_name role_partition
  role_arn="$(load_balancer_controller_role_arn)"
  [[ "$role_arn" =~ ^arn:([^:]+):iam::([0-9]{12}):role/(.+)$ ]] || \
    die 'AWS Load Balancer Controller service account has no valid IAM role annotation'
  role_partition="${BASH_REMATCH[1]}"
  role_account="${BASH_REMATCH[2]}"
  role_name="${BASH_REMATCH[3]##*/}"
  [[ "$role_partition" == "$(aws_partition_for_region "$AWS_REGION")" ]] || \
    die 'AWS Load Balancer Controller IAM role uses the wrong AWS partition'
  account_id="$(aws_cli sts get-caller-identity --query Account --output text)" || \
    die 'could not verify the AWS Load Balancer Controller IAM role account'
  [[ "$role_account" == "$account_id" ]] || \
    die 'AWS Load Balancer Controller IAM role must belong to the authenticated AWS account'
  role_json="$(aws_cli iam get-role --role-name "$role_name" --output json)" || \
    die 'could not inspect the AWS Load Balancer Controller IAM role; no resources were changed'
  oidc_issuer="$(aws_cli eks describe-cluster --name "$PORTKEY_CLUSTER_NAME" \
    --query 'cluster.identity.oidc.issuer' --output text)" || \
    die 'could not resolve the EKS OIDC issuer for the controller role; no resources were changed'
  PORTKEY_LBC_ROLE_JSON="$role_json" PORTKEY_EXPECTED_ROLE_ARN="$role_arn" \
    PORTKEY_LBC_VERIFICATION_MODE="$verification_mode" \
    PORTKEY_EXPECTED_OIDC_ISSUER="$oidc_issuer" \
    PORTKEY_EXPECTED_PARTITION="$role_partition" \
    PORTKEY_EXPECTED_IAM_SERVICE_ACCOUNT="$AWS_LBC_NAMESPACE/$AWS_LBC_SERVICE_ACCOUNT" \
    python3 - <<'PY' || \
    die 'AWS Load Balancer Controller IAM role lacks the required exact IRSA trust statement, or the strict walkthrough-owned check found extra trust or a permissions boundary; no resources were changed'
import json
import os


def singleton(value):
    if isinstance(value, list):
        if len(value) != 1:
            return None
        return value[0]
    return value


role = json.loads(os.environ["PORTKEY_LBC_ROLE_JSON"]).get("Role")
if not isinstance(role, dict) or role.get("Arn") != os.environ["PORTKEY_EXPECTED_ROLE_ARN"]:
    raise SystemExit(1)
mode = os.environ["PORTKEY_LBC_VERIFICATION_MODE"]
if mode == "strict" and role.get("PermissionsBoundary"):
    raise SystemExit(1)
issuer_url = os.environ["PORTKEY_EXPECTED_OIDC_ISSUER"]
if not issuer_url.startswith("https://"):
    raise SystemExit(1)
issuer = issuer_url[len("https://") :]
account = role["Arn"].split(":", 5)[4]
provider = (
    f"arn:{os.environ['PORTKEY_EXPECTED_PARTITION']}:iam::{account}:"
    f"oidc-provider/{issuer}"
)
trust = role.get("AssumeRolePolicyDocument")
statements = trust.get("Statement") if isinstance(trust, dict) else None
if isinstance(statements, dict):
    statements = [statements]
if not isinstance(statements, list) or not statements:
    raise SystemExit(1)
expected_conditions = {
    f"{issuer}:aud": "sts.amazonaws.com",
    f"{issuer}:sub": (
        "system:serviceaccount:"
        + os.environ["PORTKEY_EXPECTED_IAM_SERVICE_ACCOUNT"].replace("/", ":")
    ),
}


def matches_expected_irsa(statement):
    principal = statement.get("Principal")
    condition = statement.get("Condition")
    string_equals = (
        condition.get("StringEquals") if isinstance(condition, dict) else None
    )
    if isinstance(string_equals, dict):
        string_equals = {
            key: singleton(value) for key, value in string_equals.items()
        }
    return (
        statement.get("Effect") == "Allow"
        and singleton(statement.get("Action")) == "sts:AssumeRoleWithWebIdentity"
        and isinstance(principal, dict)
        and set(principal) == {"Federated"}
        and singleton(principal.get("Federated")) == provider
        and isinstance(condition, dict)
        and set(condition) == {"StringEquals"}
        and string_equals == expected_conditions
    )


matches = [statement for statement in statements if matches_expected_irsa(statement)]
if len(matches) != 1 or (mode == "strict" and len(statements) != 1):
    raise SystemExit(1)
PY

  inline_names_json="$(aws_cli iam list-role-policies --role-name "$role_name" \
    --query PolicyNames --output json)" || \
    die 'could not inspect AWS Load Balancer Controller inline IAM policies; no resources were changed'
  attached_arns_json="$(aws_cli iam list-attached-role-policies --role-name "$role_name" \
    --query 'AttachedPolicies[].PolicyArn' --output json)" || \
    die 'could not inspect AWS Load Balancer Controller managed IAM policies; no resources were changed'
  documents_file="$(mktemp)"
  chmod 600 "$documents_file"
  trap "rm -f '$documents_file'" EXIT

  while IFS= read -r policy_name; do
    [[ -n "$policy_name" ]] || continue
    policy_document="$(aws_cli iam get-role-policy --role-name "$role_name" \
      --policy-name "$policy_name" --query PolicyDocument --output json)" || \
      die 'could not inspect an AWS Load Balancer Controller inline IAM policy; no resources were changed'
    printf '%s\n' "$policy_document" >>"$documents_file"
  done < <(PORTKEY_POLICY_NAMES_JSON="$inline_names_json" python3 - <<'PY'
import json
import os

names = json.loads(os.environ["PORTKEY_POLICY_NAMES_JSON"])
if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
    raise SystemExit(1)
print("\n".join(names))
PY
)

  while IFS= read -r policy_arn; do
    [[ -n "$policy_arn" ]] || continue
    default_version="$(aws_cli iam get-policy --policy-arn "$policy_arn" \
      --query 'Policy.DefaultVersionId' --output text)" || \
      die 'could not inspect an AWS Load Balancer Controller managed IAM policy; no resources were changed'
    [[ "$default_version" =~ ^v[1-9][0-9]*$ ]] || \
      die 'AWS Load Balancer Controller managed IAM policy has an invalid default version; no resources were changed'
    policy_document="$(aws_cli iam get-policy-version --policy-arn "$policy_arn" \
      --version-id "$default_version" --query 'PolicyVersion.Document' --output json)" || \
      die 'could not inspect an AWS Load Balancer Controller managed IAM policy version; no resources were changed'
    printf '%s\n' "$policy_document" >>"$documents_file"
  done < <(PORTKEY_POLICY_ARNS_JSON="$attached_arns_json" python3 - <<'PY'
import json
import os

arns = json.loads(os.environ["PORTKEY_POLICY_ARNS_JSON"])
if not isinstance(arns, list) or not all(isinstance(arn, str) for arn in arns):
    raise SystemExit(1)
print("\n".join(arns))
PY
)

  PORTKEY_EXPECTED_AWS_PARTITION="$role_partition" \
    PORTKEY_EXPECTED_AWS_ACCOUNT="$account_id" \
    PORTKEY_EXPECTED_AWS_REGION="$AWS_REGION" \
    PORTKEY_EXPECTED_CLUSTER="$PORTKEY_CLUSTER_NAME" \
    python3 - "$documents_file" <<'PY' || \
    die 'AWS Load Balancer Controller IAM role cannot be proven TLS-capable; update its reviewed policy before retrying (no resources were changed)'
import fnmatch
import json
import os
import sys

decoder = json.JSONDecoder()
text = open(sys.argv[1], encoding="utf-8").read()
offset = 0
statements = []
while offset < len(text):
    while offset < len(text) and text[offset].isspace():
        offset += 1
    if offset == len(text):
        break
    document, offset = decoder.raw_decode(text, offset)
    current = document.get("Statement", []) if isinstance(document, dict) else []
    if isinstance(current, dict):
        current = [current]
    if not isinstance(current, list):
        raise SystemExit(1)
    statements.extend(item for item in current if isinstance(item, dict))

requirements = {
    "elasticloadbalancing:DescribeListenerCertificates": "any",
    "elasticloadbalancing:CreateListener": "loadbalancer",
    "elasticloadbalancing:ModifyListener": "listener",
    "elasticloadbalancing:AddListenerCertificates": "listener",
    "elasticloadbalancing:RemoveListenerCertificates": "listener",
}


def values(value):
    return value if isinstance(value, list) else [value]


def action_matches(pattern, action):
    return isinstance(pattern, str) and fnmatch.fnmatchcase(action.lower(), pattern.lower())


def statement_matches_action(statement, action):
    if "Action" in statement:
        return any(
            action_matches(pattern, action)
            for pattern in values(statement.get("Action", []))
        )
    if "NotAction" in statement:
        return not any(
            action_matches(pattern, action)
            for pattern in values(statement.get("NotAction", []))
        )
    return False


def relevant_resource(statement, resource_kind):
    resources = values(statement.get("Resource", []))
    if resource_kind == "any":
        return "*" in resources
    marker = "loadbalancer/net/" if resource_kind == "loadbalancer" else "listener/net/"
    prefix = (
        f"arn:{os.environ['PORTKEY_EXPECTED_AWS_PARTITION']}:"
        f"elasticloadbalancing:{os.environ['PORTKEY_EXPECTED_AWS_REGION']}:"
        f"{os.environ['PORTKEY_EXPECTED_AWS_ACCOUNT']}:{marker}"
    )
    return any(
        isinstance(resource, str)
        and (
            resource == "*"
            or (
                resource.startswith(prefix)
                and any(character in resource[len(prefix) :] for character in "*?")
            )
        )
        for resource in resources
    )


def condition_allows_expected(statement, action):
    condition = statement.get("Condition", {})
    if not isinstance(condition, dict):
        return False
    expected = {
        "aws:requestedregion": os.environ["PORTKEY_EXPECTED_AWS_REGION"],
        "aws:requesttag/elbv2.k8s.aws/cluster": os.environ[
            "PORTKEY_EXPECTED_CLUSTER"
        ],
        "aws:resourcetag/elbv2.k8s.aws/cluster": os.environ[
            "PORTKEY_EXPECTED_CLUSTER"
        ],
        "elasticloadbalancing:resourcetag/elbv2.k8s.aws/cluster": os.environ[
            "PORTKEY_EXPECTED_CLUSTER"
        ],
        "elasticloadbalancing:listenerprotocol": "TLS",
        "elasticloadbalancing:securitypolicy": (
            "ELBSecurityPolicy-TLS13-1-2-2021-06"
        ),
    }
    action_keys = {
        "elasticloadbalancing:DescribeListenerCertificates": {
            "aws:requestedregion"
        },
        "elasticloadbalancing:CreateListener": {
            "aws:requestedregion",
            "aws:requesttag/elbv2.k8s.aws/cluster",
            "aws:resourcetag/elbv2.k8s.aws/cluster",
            "elasticloadbalancing:resourcetag/elbv2.k8s.aws/cluster",
            "elasticloadbalancing:listenerprotocol",
            "elasticloadbalancing:securitypolicy",
        },
        "elasticloadbalancing:ModifyListener": {
            "aws:requestedregion",
            "aws:resourcetag/elbv2.k8s.aws/cluster",
            "elasticloadbalancing:resourcetag/elbv2.k8s.aws/cluster",
            "elasticloadbalancing:listenerprotocol",
            "elasticloadbalancing:securitypolicy",
        },
        "elasticloadbalancing:AddListenerCertificates": {
            "aws:requestedregion",
            "aws:resourcetag/elbv2.k8s.aws/cluster",
            "elasticloadbalancing:resourcetag/elbv2.k8s.aws/cluster",
        },
        "elasticloadbalancing:RemoveListenerCertificates": {
            "aws:requestedregion",
            "aws:resourcetag/elbv2.k8s.aws/cluster",
            "elasticloadbalancing:resourcetag/elbv2.k8s.aws/cluster",
        },
    }
    for operator, entries in condition.items():
        operator_lower = operator.lower()
        if "not" in operator_lower or not isinstance(entries, dict):
            return False
        for key, raw in entries.items():
            key_lower = key.lower()
            if key_lower not in action_keys[action]:
                return False
            expected_value = expected.get(key_lower)
            if expected_value is None:
                return False
            candidates = values(raw)
            if "like" in operator_lower:
                matches = any(
                    isinstance(candidate, str)
                    and fnmatch.fnmatchcase(expected_value, candidate)
                    for candidate in candidates
                )
            elif "equals" in operator_lower:
                matches = expected_value in candidates
            else:
                return False
            if not matches:
                return False
    return True


def tls_protocol_allowed(statement):
    condition = statement.get("Condition", {})
    if not isinstance(condition, dict):
        return False
    protocol_constraints = []
    for operator, entries in condition.items():
        if not isinstance(entries, dict):
            continue
        for key, value in entries.items():
            if key.lower() == "elasticloadbalancing:listenerprotocol":
                if "notequal" in operator.lower() or "notlike" in operator.lower():
                    return False
                protocol_constraints.extend(values(value))
    if not protocol_constraints:
        return True
    return any(isinstance(value, str) and value.upper() == "TLS" for value in protocol_constraints)


missing = []
for action, resource_kind in requirements.items():
    # Identity-policy Deny evaluation (especially NotResource/NotAction and
    # condition keys) is easy to under-approximate. Fail closed on every Deny
    # that can select a required action; the external-controller owner can then
    # prove/update effective access rather than receiving a false capability
    # result here.
    explicitly_denied = any(
        statement.get("Effect") == "Deny"
        and statement_matches_action(statement, action)
        for statement in statements
    )
    if explicitly_denied:
        missing.append(action + " (explicitly denied)")
        continue
    allowed = False
    for statement in statements:
        if statement.get("Effect") != "Allow" or "NotAction" in statement:
            continue
        if not statement_matches_action(statement, action):
            continue
        if not relevant_resource(statement, resource_kind):
            continue
        if not condition_allows_expected(statement, action):
            continue
        if action.endswith((":CreateListener", ":ModifyListener")) and not tls_protocol_allowed(statement):
            continue
        allowed = True
        break
    if not allowed:
        missing.append(action)
if missing:
    print("missing required TLS actions: " + ", ".join(missing), file=sys.stderr)
    raise SystemExit(1)
PY
)

walkthrough_lbc_stack_name() {
  printf 'eksctl-%s-addon-iamserviceaccount-%s-%s\n' \
    "$PORTKEY_CLUSTER_NAME" "$AWS_LBC_NAMESPACE" "$AWS_LBC_SERVICE_ACCOUNT"
}

probe_walkthrough_lbc_stack() {
  local error_file expected_stack_name observed_stack_name
  PORTKEY_LBC_STACK_PRESENT=false
  expected_stack_name="$(walkthrough_lbc_stack_name)"
  error_file="$(mktemp)"
  chmod 600 "$error_file"
  if observed_stack_name="$(aws_cli cloudformation describe-stacks \
    --stack-name "$expected_stack_name" --query 'Stacks[0].StackName' \
    --output text 2>"$error_file")"; then
    rm -f "$error_file"
    [[ "$observed_stack_name" == "$expected_stack_name" ]] || \
      die 'the walkthrough-managed controller IAM stack lookup returned an unexpected stack'
    PORTKEY_LBC_STACK_PRESENT=true
    return
  fi
  if grep -q 'ValidationError' "$error_file" && \
    grep -qi 'does not exist' "$error_file"; then
    rm -f "$error_file"
    return
  fi
  rm -f "$error_file"
  die 'could not determine whether the walkthrough-managed controller IAM stack exists'
}

validate_walkthrough_lbc_stack() {
  require_command aws; require_command python3
  local expected_stack_name oidc_issuer policy_name role_arn role_json sa_role stack_json template_json
  expected_stack_name="$(walkthrough_lbc_stack_name)"
  sa_role="$(load_balancer_controller_role_arn)"
  [[ "$sa_role" == arn:*:iam::*:role/* ]] || \
    die 'walkthrough-managed AWS Load Balancer Controller service account has no valid IAM role annotation; no resources were changed'
  stack_json="$(aws_cli cloudformation describe-stacks \
    --stack-name "$expected_stack_name" --output json)" || \
    die 'the expected eksctl IAM service-account stack is missing or unreadable; refusing to mutate the controller role'
  template_json="$(aws_cli cloudformation get-template \
    --stack-name "$expected_stack_name" --template-stage Original --output json)" || \
    die 'could not inspect the eksctl IAM service-account stack template; no resources were changed'
  role_json="$(aws_cli iam get-role --role-name "${sa_role##*/}" --output json)" || \
    die 'could not inspect the live walkthrough-managed controller role; no resources were changed'
  oidc_issuer="$(aws_cli eks describe-cluster --name "$PORTKEY_CLUSTER_NAME" \
    --query 'cluster.identity.oidc.issuer' --output text)" || \
    die 'could not resolve the EKS OIDC issuer for the controller role; no resources were changed'
  [[ "$oidc_issuer" == https://* ]] || \
    die 'EKS returned an invalid OIDC issuer for the controller role; no resources were changed'
  role_arn="$(PORTKEY_LBC_STACK_JSON="$stack_json" \
    PORTKEY_LBC_TEMPLATE_JSON="$template_json" \
    PORTKEY_LBC_ROLE_JSON="$role_json" \
    PORTKEY_EXPECTED_OIDC_ISSUER="$oidc_issuer" \
    PORTKEY_EXPECTED_PARTITION="$(aws_partition_for_region "$AWS_REGION")" \
    PORTKEY_EXPECTED_LBC_STACK="$expected_stack_name" \
    PORTKEY_EXPECTED_CLUSTER="$PORTKEY_CLUSTER_NAME" \
    PORTKEY_EXPECTED_IAM_SERVICE_ACCOUNT="$AWS_LBC_NAMESPACE/$AWS_LBC_SERVICE_ACCOUNT" \
    PORTKEY_EXPECTED_ROLE_ARN="$sa_role" python3 - <<'PY'
import json
import os


def reject(message):
    raise SystemExit(message)


payload = json.loads(os.environ["PORTKEY_LBC_STACK_JSON"])
stacks = payload.get("Stacks")
if not isinstance(stacks, list) or len(stacks) != 1:
    reject("expected exactly one IAM service-account stack")
stack = stacks[0]
if stack.get("StackName") != os.environ["PORTKEY_EXPECTED_LBC_STACK"]:
    reject("unexpected IAM service-account stack name")
if stack.get("StackStatus") not in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}:
    reject("IAM service-account stack is not in a stable successful state")

tags = {
    item.get("Key"): item.get("Value")
    for item in stack.get("Tags", [])
    if isinstance(item, dict)
}
expected_tags = {
    "alpha.eksctl.io/cluster-name": os.environ["PORTKEY_EXPECTED_CLUSTER"],
    "alpha.eksctl.io/iamserviceaccount-name": os.environ[
        "PORTKEY_EXPECTED_IAM_SERVICE_ACCOUNT"
    ],
    "Application": "guidance-codex-portkey",
}
if any(tags.get(key) != value for key, value in expected_tags.items()):
    reject("IAM service-account stack ownership tags do not match")

outputs = {
    item.get("OutputKey"): item.get("OutputValue")
    for item in stack.get("Outputs", [])
    if isinstance(item, dict)
}
role_arn = outputs.get("Role1")
if role_arn != os.environ["PORTKEY_EXPECTED_ROLE_ARN"]:
    reject("stack Role1 output does not match the Kubernetes service account")

live_role = json.loads(os.environ["PORTKEY_LBC_ROLE_JSON"]).get("Role")
if not isinstance(live_role, dict) or live_role.get("Arn") != role_arn:
    reject("live IAM role does not match the stack Role1 output")
if live_role.get("PermissionsBoundary"):
    reject("live IAM role has an unsupported permissions boundary")

issuer = os.environ["PORTKEY_EXPECTED_OIDC_ISSUER"][len("https://") :]
account = role_arn.split(":", 5)[4]
provider = (
    f"arn:{os.environ['PORTKEY_EXPECTED_PARTITION']}:iam::{account}:"
    f"oidc-provider/{issuer}"
)
trust = live_role.get("AssumeRolePolicyDocument")
statements = trust.get("Statement") if isinstance(trust, dict) else None
if isinstance(statements, dict):
    statements = [statements]
if not isinstance(statements, list) or len(statements) != 1:
    reject("live IAM role trust policy is not the expected single IRSA statement")
statement = statements[0]
expected_conditions = {
    f"{issuer}:aud": "sts.amazonaws.com",
    f"{issuer}:sub": (
        "system:serviceaccount:"
        + os.environ["PORTKEY_EXPECTED_IAM_SERVICE_ACCOUNT"].replace("/", ":")
    ),
}


def singleton(value):
    if isinstance(value, list):
        if len(value) != 1:
            reject("live IAM role trust policy contains multiple principals or actions")
        return value[0]
    return value


principal = statement.get("Principal")
federated = principal.get("Federated") if isinstance(principal, dict) else None
condition = statement.get("Condition")
string_equals = condition.get("StringEquals") if isinstance(condition, dict) else None
if isinstance(string_equals, dict):
    string_equals = {key: singleton(value) for key, value in string_equals.items()}
if (
    statement.get("Effect") != "Allow"
    or singleton(statement.get("Action")) != "sts:AssumeRoleWithWebIdentity"
    or singleton(federated) != provider
    or string_equals != expected_conditions
    or set(condition or {}) != {"StringEquals"}
    or set(principal or {}) != {"Federated"}
):
    reject("live IAM role trust policy does not match the exact controller IRSA subject")

template_payload = json.loads(os.environ["PORTKEY_LBC_TEMPLATE_JSON"])
template = template_payload.get("TemplateBody")
if isinstance(template, str):
    template = json.loads(template)
if not isinstance(template, dict):
    reject("IAM service-account stack template is malformed")
expected_description = (
    'IAM role for serviceaccount "'
    + os.environ["PORTKEY_EXPECTED_IAM_SERVICE_ACCOUNT"]
    + '" [created and managed by eksctl]'
)
if (
    set(template) != {
        "AWSTemplateFormatVersion",
        "Description",
        "Outputs",
        "Resources",
    }
    or template.get("AWSTemplateFormatVersion") != "2010-09-09"
    or template.get("Description") != expected_description
):
    reject("IAM service-account stack template has unexpected top-level state")
resources = template.get("Resources")
if not isinstance(resources, dict) or set(resources) != {"Role1", "Policy1"}:
    reject("IAM service-account stack contains unexpected resources")
role = resources["Role1"]
policy = resources["Policy1"]
if role.get("Type") != "AWS::IAM::Role" or policy.get("Type") != "AWS::IAM::Policy":
    reject("IAM service-account stack has an unexpected resource type")
role_properties = role.get("Properties", {})
expected_template_trust = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": ["sts:AssumeRoleWithWebIdentity"],
            "Principal": {"Federated": provider},
            "Condition": {"StringEquals": expected_conditions},
        }
    ],
}
if role_properties != {"AssumeRolePolicyDocument": expected_template_trust}:
    reject("IAM service-account role template has custom or unexpected properties")
policy_properties = policy.get("Properties", {})
if set(policy_properties) != {"PolicyDocument", "PolicyName", "Roles"}:
    reject("IAM inline policy template has custom or missing properties")
if policy_properties.get("PolicyName") != {
    "Fn::Sub": "${AWS::StackName}-Policy1"
}:
    reject("IAM inline policy name is not the eksctl-managed name")
policy_roles = policy_properties.get("Roles")
if policy_roles != [{"Ref": "Role1"}]:
    reject("IAM inline policy is not attached only to Role1")
if not isinstance(policy_properties.get("PolicyDocument"), dict):
    reject("IAM inline policy document is malformed")
if template.get("Outputs") != {
    "Role1": {"Value": {"Fn::GetAtt": "Role1.Arn"}}
}:
    reject("IAM service-account stack output is not the eksctl Role1 ARN")
print(role_arn)
PY
  )" || die 'walkthrough-managed controller IAM stack validation failed; no resources were changed'
  policy_name="$(aws_cli cloudformation describe-stack-resource \
    --stack-name "$expected_stack_name" --logical-resource-id Policy1 \
    --query 'StackResourceDetail.PhysicalResourceId' --output text)" || \
    die 'could not resolve the walkthrough-managed controller inline policy; no resources were changed'
  [[ -n "$policy_name" && "$policy_name" != None && "$policy_name" != null ]] || \
    die 'walkthrough-managed controller inline policy has no physical identifier; no resources were changed'

  PORTKEY_LBC_STACK_NAME="$expected_stack_name"
  PORTKEY_LBC_STACK_ROLE_ARN="$role_arn"
  PORTKEY_LBC_STACK_ROLE_NAME="${role_arn##*/}"
  PORTKEY_LBC_STACK_POLICY_NAME="$policy_name"
  PORTKEY_LBC_STACK_TEMPLATE_JSON="$template_json"
}

validate_walkthrough_lbc_policy_ownership() {
  local actual_policy attached_arns_json inline_names_json
  inline_names_json="$(aws_cli iam list-role-policies \
    --role-name "$PORTKEY_LBC_STACK_ROLE_NAME" --query PolicyNames --output json)" || \
    die 'could not inspect walkthrough-managed controller inline policies; no resources were changed'
  attached_arns_json="$(aws_cli iam list-attached-role-policies \
    --role-name "$PORTKEY_LBC_STACK_ROLE_NAME" \
    --query 'AttachedPolicies[].PolicyArn' --output json)" || \
    die 'could not inspect walkthrough-managed controller managed policies; no resources were changed'
  PORTKEY_LBC_INLINE_NAMES_JSON="$inline_names_json" \
    PORTKEY_LBC_ATTACHED_ARNS_JSON="$attached_arns_json" \
    PORTKEY_EXPECTED_POLICY_NAME="$PORTKEY_LBC_STACK_POLICY_NAME" python3 - <<'PY' || \
    die 'walkthrough-managed controller role contains unexpected IAM policies; refusing to update it'
import json
import os

inline_names = json.loads(os.environ["PORTKEY_LBC_INLINE_NAMES_JSON"])
attached_arns = json.loads(os.environ["PORTKEY_LBC_ATTACHED_ARNS_JSON"])
if inline_names != [os.environ["PORTKEY_EXPECTED_POLICY_NAME"]]:
    raise SystemExit(1)
if attached_arns != []:
    raise SystemExit(1)
PY
  actual_policy="$(aws_cli iam get-role-policy \
    --role-name "$PORTKEY_LBC_STACK_ROLE_NAME" \
    --policy-name "$PORTKEY_LBC_STACK_POLICY_NAME" \
    --query PolicyDocument --output json)" || \
    die 'could not inspect the walkthrough-managed controller inline policy; no resources were changed'
  PORTKEY_ACTUAL_LBC_POLICY="$actual_policy" \
    PORTKEY_LBC_TEMPLATE_JSON="$PORTKEY_LBC_STACK_TEMPLATE_JSON" python3 - <<'PY' || \
    die 'walkthrough-managed controller inline policy has drifted from its CloudFormation stack; refusing to overwrite it'
import json
import os


def normalize(value):
    if isinstance(value, dict):
        return {key: normalize(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        normalized = [normalize(item) for item in value]
        return sorted(normalized, key=lambda item: json.dumps(item, sort_keys=True))
    return value


actual = json.loads(os.environ["PORTKEY_ACTUAL_LBC_POLICY"])
template_payload = json.loads(os.environ["PORTKEY_LBC_TEMPLATE_JSON"])
template = template_payload.get("TemplateBody")
if isinstance(template, str):
    template = json.loads(template)
expected = template["Resources"]["Policy1"]["Properties"]["PolicyDocument"]
if normalize(actual) != normalize(expected):
    raise SystemExit(1)
PY
}

walkthrough_lbc_policy_matches() {
  local actual_policy rendered="$1"
  actual_policy="$(aws_cli iam get-role-policy \
    --role-name "$PORTKEY_LBC_STACK_ROLE_NAME" \
    --policy-name "$PORTKEY_LBC_STACK_POLICY_NAME" \
    --query PolicyDocument --output json)" || \
    die 'could not inspect the walkthrough-managed controller policy before comparison; no resources were changed'
  PORTKEY_ACTUAL_LBC_POLICY="$actual_policy" \
    PORTKEY_RENDERED_LBC_CONFIG="$rendered" python3 - <<'PY'
import json
import os


def normalize(value):
    if isinstance(value, dict):
        return {key: normalize(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        normalized = [normalize(item) for item in value]
        return sorted(normalized, key=lambda item: json.dumps(item, sort_keys=True))
    return value


actual = json.loads(os.environ["PORTKEY_ACTUAL_LBC_POLICY"])
rendered = json.loads(
    open(os.environ["PORTKEY_RENDERED_LBC_CONFIG"], encoding="utf-8").read()
)
expected = rendered["iam"]["serviceAccounts"][0]["attachPolicy"]
if normalize(actual) != normalize(expected):
    raise SystemExit(1)
PY
}

validate_walkthrough_lbc_policy_matches() {
  walkthrough_lbc_policy_matches "$1" || \
    die 'updated AWS Load Balancer Controller IAM policy does not match the reviewed template'
}

walkthrough_lbc_policy_matches_known_legacy() {
  local actual_policy rendered="$1"
  actual_policy="$(aws_cli iam get-role-policy \
    --role-name "$PORTKEY_LBC_STACK_ROLE_NAME" \
    --policy-name "$PORTKEY_LBC_STACK_POLICY_NAME" \
    --query PolicyDocument --output json)" || \
    die 'could not inspect the walkthrough-managed controller policy before legacy comparison; no resources were changed'
  PORTKEY_ACTUAL_LBC_POLICY="$actual_policy" \
    PORTKEY_RENDERED_LBC_CONFIG="$rendered" python3 - <<'PY'
import copy
import json
import os


def normalize(value):
    if isinstance(value, dict):
        return {key: normalize(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        normalized = [normalize(item) for item in value]
        return sorted(normalized, key=lambda item: json.dumps(item, sort_keys=True))
    return value


actual = json.loads(os.environ["PORTKEY_ACTUAL_LBC_POLICY"])
rendered = json.loads(
    open(os.environ["PORTKEY_RENDERED_LBC_CONFIG"], encoding="utf-8").read()
)
legacy = copy.deepcopy(rendered["iam"]["serviceAccounts"][0]["attachPolicy"])
statements = legacy.get("Statement")
if not isinstance(statements, list):
    raise SystemExit(1)


def one(sid):
    matches = [statement for statement in statements if statement.get("Sid") == sid]
    if len(matches) != 1:
        raise SystemExit(1)
    return matches[0]


read_state = one("ReadNetworkLoadBalancerStateInDeploymentRegion")
read_actions = read_state.get("Action")
if not isinstance(read_actions, list) or "elasticloadbalancing:DescribeListenerCertificates" not in read_actions:
    raise SystemExit(1)
read_actions.remove("elasticloadbalancing:DescribeListenerCertificates")

create_listener = one("CreateTaggedTlsListenersOnControllerLoadBalancers")
create_listener["Sid"] = "CreateTaggedTcpListenersOnControllerLoadBalancers"
condition = create_listener.get("Condition")
if not isinstance(condition, dict) or "ForAnyValue:StringEquals" not in condition:
    raise SystemExit(1)
condition.pop("ForAnyValue:StringEquals")
string_equals = condition.get("StringEquals")
if not isinstance(string_equals, dict):
    raise SystemExit(1)
string_equals["elasticloadbalancing:ListenerProtocol"] = "TCP"

modify_tls = one("ModifyOnlyTlsListenersOnControllerLoadBalancers")
statements.remove(modify_tls)
listener_lifecycle = one("ManageControllerNetworkLoadBalancerListeners")
lifecycle_actions = listener_lifecycle.get("Action")
if not isinstance(lifecycle_actions, list):
    raise SystemExit(1)
for action in (
    "elasticloadbalancing:AddListenerCertificates",
    "elasticloadbalancing:RemoveListenerCertificates",
):
    if action not in lifecycle_actions:
        raise SystemExit(1)
    lifecycle_actions.remove(action)
lifecycle_actions.append("elasticloadbalancing:ModifyListener")

if normalize(actual) != normalize(legacy):
    raise SystemExit(1)
PY
}

validate_reviewed_walkthrough_lbc_policy() (
  local account_id partition rendered vpc_id
  account_id="$(aws_cli sts get-caller-identity --query Account --output text)" || \
    die 'could not resolve the AWS account for controller policy validation'
  [[ "$account_id" =~ ^[0-9]{12}$ ]] || \
    die 'could not resolve the AWS account for controller policy validation'
  vpc_id="$(aws_cli eks describe-cluster --name "$PORTKEY_CLUSTER_NAME" \
    --query 'cluster.resourcesVpcConfig.vpcId' --output text)" || \
    die 'could not resolve the EKS VPC for controller policy validation'
  [[ "$vpc_id" =~ ^vpc-[0-9a-f]+$ ]] || \
    die 'could not resolve the EKS VPC for controller policy validation'
  partition="$(aws_partition_for_region "$AWS_REGION")"
  rendered="$(mktemp)"
  chmod 600 "$rendered"
  trap "rm -f '$rendered'" EXIT
  render_load_balancer_controller_service_account \
    "$rendered" "$account_id" "$vpc_id" "$partition"
  validate_walkthrough_lbc_stack
  validate_walkthrough_lbc_policy_ownership
  validate_walkthrough_lbc_policy_matches "$rendered"
)

load_balancer_controller_has_exact_portkey_watch() {
  local deployment_json
  deployment_json="$(kubectl -n "$AWS_LBC_NAMESPACE" get deployment \
    "$AWS_LBC_HELM_RELEASE" -o json)" || return 1
  PORTKEY_LBC_DEPLOYMENT_JSON="$deployment_json" \
    PORTKEY_EXPECTED_WATCH_NAMESPACE="$PORTKEY_NAMESPACE" python3 - <<'PY'
import json
import os

deployment = json.loads(os.environ["PORTKEY_LBC_DEPLOYMENT_JSON"])
containers = (
    deployment.get("spec", {})
    .get("template", {})
    .get("spec", {})
    .get("containers", [])
)
container = next(
    (item for item in containers if item.get("name") == "aws-load-balancer-controller"),
    None,
)
if not container:
    raise SystemExit(1)
args = container.get("args")
if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
    raise SystemExit(1)
values = []
for index, argument in enumerate(args):
    if argument.startswith("--watch-namespace="):
        values.append(argument.split("=", 1)[1])
    elif argument == "--watch-namespace":
        if index + 1 >= len(args):
            raise SystemExit(1)
        values.append(args[index + 1])
if values != [os.environ["PORTKEY_EXPECTED_WATCH_NAMESPACE"]]:
    raise SystemExit(1)
PY
}

require_exclusive_walkthrough_lbc_scope() {
  local bindings_json gateway_api_resources gateways namespace_objects tgb_crd
  local -a scope_args

  # A healthy walkthrough controller watches only the Portkey namespace. When
  # recovering a missing or malformed Deployment, scan the whole cluster so a
  # damaged watch flag cannot hide another dependency from this policy change.
  scope_args=(--all-namespaces)
  if load_balancer_controller_exists && \
    load_balancer_controller_has_exact_portkey_watch; then
    scope_args=(-n "$PORTKEY_NAMESPACE")
  fi

  namespace_objects="$(kubectl get services,ingresses.networking.k8s.io \
    "${scope_args[@]}" --ignore-not-found -o json)" || \
    die 'could not inspect controller dependents before its IAM policy update'
  PORTKEY_NAMESPACE_OBJECTS="$namespace_objects" python3 - <<'PY' || \
    die 'an Ingress or LoadBalancer Service still depends on the controller; for the legacy Portkey Service, complete the documented Service/NLB removal before replacing its IAM policy'
import json
import os

items = json.loads(os.environ["PORTKEY_NAMESPACE_OBJECTS"]).get("items")
if not isinstance(items, list):
    raise SystemExit(1)
for item in items:
    kind = item.get("kind")
    if kind == "Ingress":
        raise SystemExit(1)
    if kind == "Service" and item.get("spec", {}).get("type") == "LoadBalancer":
        raise SystemExit(1)
PY

  tgb_crd="$(kubectl get crd targetgroupbindings.elbv2.k8s.aws \
    --ignore-not-found -o name)" || \
    die 'could not determine whether TargetGroupBinding resources exist before the controller IAM update'
  if [[ -n "$tgb_crd" ]]; then
    bindings_json="$(kubectl get targetgroupbindings.elbv2.k8s.aws \
      "${scope_args[@]}" --ignore-not-found -o json)" || \
      die 'could not inspect TargetGroupBinding dependents before the controller IAM update'
    PORTKEY_TARGET_GROUP_BINDINGS="$bindings_json" python3 - <<'PY' || \
      die 'a TargetGroupBinding still depends on the controller; wait for NLB cleanup before replacing its IAM policy'
import json
import os

items = json.loads(os.environ["PORTKEY_TARGET_GROUP_BINDINGS"]).get("items")
if not isinstance(items, list) or items:
    raise SystemExit(1)
PY
  fi

  gateway_api_resources="$(kubectl api-resources \
    --api-group=gateway.networking.k8s.io --namespaced=true -o name)" || \
    die 'could not inspect Gateway API availability before the controller IAM update'
  if [[ "$gateway_api_resources" == *gateways.gateway.networking.k8s.io* ]]; then
    gateways="$(kubectl get gateways.gateway.networking.k8s.io \
      "${scope_args[@]}" -o name)" || \
      die 'could not inspect Gateway API dependents before the controller IAM update'
    [[ -z "$gateways" ]] || \
      die 'the walkthrough-managed controller has Gateway API dependents; do not replace its IAM policy'
  fi
}

reconcile_walkthrough_lbc_policy() {
  local rendered="$1" original_role
  validate_walkthrough_lbc_stack
  validate_walkthrough_lbc_policy_ownership
  # Do not disturb a live controller when the exact reviewed policy is already
  # installed. A real TCP-to-TLS policy transition requires zero dependents so
  # the legacy listener cannot be stranded between the two configurations.
  if walkthrough_lbc_policy_matches "$rendered"; then
    return
  fi
  walkthrough_lbc_policy_matches_known_legacy "$rendered" || \
    die 'walkthrough-managed controller policy is stack-consistent but is neither the exact reviewed legacy TCP policy nor the desired TLS policy; refusing to overwrite custom state'
  require_exclusive_walkthrough_lbc_scope
  require_eksctl_lbc_reconciliation_version
  original_role="$PORTKEY_LBC_STACK_ROLE_ARN"
  eksctl update iamserviceaccount --config-file "$rendered" \
    --include "$AWS_LBC_NAMESPACE/$AWS_LBC_SERVICE_ACCOUNT" --approve
  validate_walkthrough_lbc_stack
  [[ "$PORTKEY_LBC_STACK_ROLE_ARN" == "$original_role" ]] || \
    die 'eksctl changed the AWS Load Balancer Controller role unexpectedly'
  validate_walkthrough_lbc_policy_ownership
  validate_walkthrough_lbc_policy_matches "$rendered"
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
  local require_exact_watch="${1:-false}" deployment_json expected_vpc_id
  expected_vpc_id="$(aws_cli eks describe-cluster --name "$PORTKEY_CLUSTER_NAME" \
    --query 'cluster.resourcesVpcConfig.vpcId' --output text)" || \
    die 'could not resolve the EKS VPC for controller compatibility validation'
  [[ "$expected_vpc_id" =~ ^vpc-[0-9a-f]+$ ]] || \
    die 'could not resolve the EKS VPC for controller compatibility validation'
  deployment_json="$(kubectl -n "$AWS_LBC_NAMESPACE" get deployment \
    "$AWS_LBC_HELM_RELEASE" -o json)" || \
    die 'could not inspect the AWS Load Balancer Controller deployment'
  PORTKEY_LBC_DEPLOYMENT_JSON="$deployment_json" \
  PORTKEY_LBC_EXPECTED_VERSION="v$PORTKEY_LBC_HELM_CHART_VERSION" \
    PORTKEY_LBC_EXPECTED_IMAGE_REPOSITORY="public.ecr.aws/eks/aws-load-balancer-controller" \
    PORTKEY_CLUSTER_NAME="$PORTKEY_CLUSTER_NAME" \
    PORTKEY_AWS_REGION="$AWS_REGION" \
    PORTKEY_EXPECTED_VPC_ID="$expected_vpc_id" \
    PORTKEY_LBC_SERVICE_ACCOUNT="$AWS_LBC_SERVICE_ACCOUNT" \
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
if pod_spec.get("serviceAccountName") != os.environ["PORTKEY_LBC_SERVICE_ACCOUNT"]:
    reject("the controller Deployment uses an unexpected service account")
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
image_repository = image_without_digest.rsplit(":", 1)[0] if ":" in image_without_digest else image_without_digest
if image_tag != expected_version:
    reject(
        f"expected image tag {expected_version}, found {image_tag!r}; "
        "Deployment labels cannot substitute for the running image version"
    )
if label_version and label_version != expected_version:
    reject(f"expected version label {expected_version}, found {label_version!r}")
if (
    os.environ["PORTKEY_REQUIRE_EXACT_WATCH"] == "true"
    and image_repository != os.environ["PORTKEY_LBC_EXPECTED_IMAGE_REPOSITORY"]
):
    reject("walkthrough-managed controller uses an unexpected image repository")

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


def parse_go_bool(value, flag):
    normalized = value.strip().lower()
    if normalized in {"1", "t", "true"}:
        return True
    if normalized in {"0", "f", "false"}:
        return False
    reject(f"{flag} has an invalid boolean value")


cluster_values = flag_values("--cluster-name")
if cluster_values != [os.environ["PORTKEY_CLUSTER_NAME"]]:
    reject(
        f"--cluster-name must be exactly {os.environ['PORTKEY_CLUSTER_NAME']!r}"
    )

region_values = flag_values("--aws-region")
if os.environ["PORTKEY_REQUIRE_EXACT_WATCH"] == "true":
    region_valid = region_values == [os.environ["PORTKEY_AWS_REGION"]]
else:
    region_valid = region_values in ([], [os.environ["PORTKEY_AWS_REGION"]])
if not region_valid:
    qualifier = "exactly" if os.environ["PORTKEY_REQUIRE_EXACT_WATCH"] == "true" else "absent or exactly"
    reject(f"--aws-region must be {qualifier} {os.environ['PORTKEY_AWS_REGION']!r}")

vpc_values = flag_values("--aws-vpc-id")
if os.environ["PORTKEY_REQUIRE_EXACT_WATCH"] == "true":
    vpc_valid = vpc_values == [os.environ["PORTKEY_EXPECTED_VPC_ID"]]
else:
    vpc_valid = vpc_values in ([], [os.environ["PORTKEY_EXPECTED_VPC_ID"]])
if not vpc_valid:
    qualifier = "exactly" if os.environ["PORTKEY_REQUIRE_EXACT_WATCH"] == "true" else "absent or exactly"
    reject(f"--aws-vpc-id must be {qualifier} {os.environ['PORTKEY_EXPECTED_VPC_ID']!r}")

watch_values = flag_values("--watch-namespace")
expected_namespace = os.environ["PORTKEY_NAMESPACE"]
if os.environ["PORTKEY_REQUIRE_EXACT_WATCH"] == "true":
    if watch_values != [expected_namespace]:
        reject(f"cleanup requires --watch-namespace={expected_namespace}")
elif watch_values not in ([], [expected_namespace]):
    reject(
        f"controller watches {watch_values!r}, not the Portkey namespace "
        f"{expected_namespace!r} or all namespaces"
    )

feature_sets = flag_values("--feature-gates")
listener_tagging_occurrences = 0
for feature_set in feature_sets:
    for feature in feature_set.split(","):
        name, separator, value = feature.partition("=")
        name = name.strip()
        if name not in {
            "NLBSecurityGroup",
            "ListenerRulesTagging",
            "EnableServiceController",
            "EnableIPTargetType",
        }:
            continue
        if not separator:
            reject(f"{name} has no boolean value")
        if not parse_go_bool(value, name):
            reject(f"{name} must not be disabled")
        if name == "ListenerRulesTagging":
            listener_tagging_occurrences += 1
if (
    os.environ["PORTKEY_REQUIRE_EXACT_WATCH"] == "true"
    and listener_tagging_occurrences != 1
):
    reject("walkthrough-managed controller must explicitly enable ListenerRulesTagging once")

for flag in ("--enable-shield", "--enable-waf", "--enable-wafv2"):
    flag_settings = flag_values(flag)
    if flag_settings and (
        len(flag_settings) != 1 or parse_go_bool(flag_settings[0], flag)
    ):
        reject(f"{flag} must remain false")
    if os.environ["PORTKEY_REQUIRE_EXACT_WATCH"] == "true" and flag_settings != ["false"]:
        reject(f"walkthrough-managed controller must set {flag}=false exactly")

backend_security_group_values = flag_values("--enable-backend-security-group")
if backend_security_group_values and (
    len(backend_security_group_values) != 1
    or not parse_go_bool(
        backend_security_group_values[0], "--enable-backend-security-group"
    )
):
    reject("--enable-backend-security-group must remain true")

unrestricted_rule_values = flag_values("--disable-restricted-sg-rules")
if unrestricted_rule_values and (
    len(unrestricted_rule_values) != 1
    or parse_go_bool(
        unrestricted_rule_values[0], "--disable-restricted-sg-rules"
    )
):
    reject("--disable-restricted-sg-rules must remain false")
PY
}

require_load_balancer_controller() {
  require_command kubectl; require_command python3; validate_common; kube_context
  load_balancer_controller_exists || die 'AWS Load Balancer Controller is missing; run CONFIRM_AWS_WRITE=1 make portkey-lbc-deploy'
  load_balancer_controller_is_ready || die 'AWS Load Balancer Controller is not ready or its CRDs are missing'
  if load_balancer_controller_service_account_is_managed; then
    load_balancer_controller_is_helm_owned || \
      die 'walkthrough-managed AWS Load Balancer Controller is not owned by the expected Helm release'
    load_balancer_controller_is_compatible true || \
      die 'walkthrough-managed AWS Load Balancer Controller is ready but has drifted from its reviewed image, service account, cluster, region, VPC, namespace, or feature configuration'
    validate_reviewed_walkthrough_lbc_policy
    require_load_balancer_controller_tls_permissions
  else
    load_balancer_controller_is_compatible false || \
      die 'externally managed AWS Load Balancer Controller is ready but incompatible with this Portkey deployment'
    [[ "$PORTKEY_EXTERNAL_LBC_POLICY_CONFIRMED" == true ]] || \
      die 'an externally managed controller requires command-scoped PORTKEY_EXTERNAL_LBC_POLICY_CONFIRMED=true after the cluster owner verifies its complete NLB, security-group, TLS, IRSA, boundary, and organization-policy permissions'
    require_load_balancer_controller_tls_permissions external
  fi
  printf 'AWS Load Balancer Controller is ready; the walkthrough-managed policy is exact or the external controller owner explicitly confirmed its base permissions, and the role policies include the required NLB TLS actions.\n'
}

require_safe_nlb_service_upgrade() {
  local mode="${1:-pre}" gateway_api_resources gateways ingresses service_json service_resource
  [[ "$mode" == pre || "$mode" == post ]] || die 'invalid NLB Service verification mode'
  ingresses="$(kubectl -n "$PORTKEY_NAMESPACE" get ingresses.networking.k8s.io \
    --ignore-not-found -o name)" || die 'could not inspect Ingress resources during Portkey NLB safety verification'
  if [[ -n "$ingresses" ]]; then
    if [[ "$mode" == post ]]; then
      die 'the Helm release changed, but an Ingress now exists in the Portkey namespace; inspect the release and any ALB dependencies, then correct it through the reviewed TLS path. Do not roll back to a revision containing the legacy plaintext Service'
    fi
    die 'a manual Ingress exists in the Portkey namespace; remove it only through its owning process and verify any ALB dependencies before retrying (no resources were changed)'
  fi
  gateway_api_resources="$(kubectl api-resources \
    --api-group=gateway.networking.k8s.io --namespaced=true -o name)" || \
    die 'could not inspect Gateway API availability during Portkey NLB safety verification'
  if [[ "$gateway_api_resources" == *gateways.gateway.networking.k8s.io* ]]; then
    gateways="$(kubectl -n "$PORTKEY_NAMESPACE" get \
      gateways.gateway.networking.k8s.io -o name)" || \
      die 'could not inspect Gateway resources during Portkey NLB safety verification'
    if [[ -n "$gateways" ]]; then
      if [[ "$mode" == post ]]; then
        die 'the Helm release changed, but a Gateway API exposure now exists in the Portkey namespace; inspect the release, then correct it through the reviewed TLS path. Do not roll back to a revision containing the legacy plaintext Service'
      fi
      die 'a Gateway API exposure exists in the Portkey namespace; remove it only through its owning process and verify its dependencies before retrying (no resources were changed)'
    fi
  fi
  service_resource="$(kubectl -n "$PORTKEY_NAMESPACE" get service "$PORTKEY_GATEWAY_SERVICE" \
    --ignore-not-found -o name)" || die 'could not query the existing Portkey gateway Service'
  if [[ -z "$service_resource" ]]; then
    if [[ "$mode" == post ]]; then
      die 'the Helm release changed, but it did not produce the expected gateway Service; inspect the release, then correct it through the reviewed TLS path. Do not roll back to a revision containing the legacy plaintext Service'
    fi
    return
  fi
  service_json="$(kubectl -n "$PORTKEY_NAMESPACE" get service "$PORTKEY_GATEWAY_SERVICE" \
    -o json)" || die 'could not inspect the existing Portkey gateway Service'
  if ! PORTKEY_EXISTING_SERVICE_JSON="$service_json" \
    PORTKEY_EXPECTED_CERTIFICATE_ARN="$PORTKEY_NLB_TLS_CERTIFICATE_ARN" \
    PORTKEY_EXPECTED_PREFIX_LIST_IDS="$PORTKEY_NLB_ALLOWED_PREFIX_LIST_IDS" \
    PORTKEY_EXPECTED_TLS_UPDATE_CONFIRMATION="$PORTKEY_NAMESPACE/$PORTKEY_GATEWAY_SERVICE" \
    PORTKEY_TLS_UPDATE_CONFIRMATION="$CONFIRM_PORTKEY_NLB_TLS_UPDATE" \
    PORTKEY_SERVICE_VERIFY_MODE="$mode" python3 - <<'PY'
import json
import os

service = json.loads(os.environ["PORTKEY_EXISTING_SERVICE_JSON"])
metadata = service.get("metadata", {})
annotations = metadata.get("annotations", {})
spec = service.get("spec", {})
if not isinstance(annotations, dict) or spec.get("type") != "LoadBalancer":
    raise SystemExit(1)

expected_annotations = {
    "service.beta.kubernetes.io/aws-load-balancer-type": "external",
    "service.beta.kubernetes.io/aws-load-balancer-scheme": "internal",
    "service.beta.kubernetes.io/aws-load-balancer-ip-address-type": "ipv4",
    "service.beta.kubernetes.io/aws-load-balancer-nlb-target-type": "ip",
    "service.beta.kubernetes.io/aws-load-balancer-ssl-ports": "443",
    "service.beta.kubernetes.io/aws-load-balancer-ssl-negotiation-policy": (
        "ELBSecurityPolicy-TLS13-1-2-2021-06"
    ),
    "service.beta.kubernetes.io/aws-load-balancer-backend-protocol": "tcp",
    "service.beta.kubernetes.io/aws-load-balancer-healthcheck-path": "/v1/health",
    "service.beta.kubernetes.io/aws-load-balancer-healthcheck-protocol": "http",
    "service.beta.kubernetes.io/aws-load-balancer-healthcheck-port": "8787",
    "service.beta.kubernetes.io/aws-load-balancer-manage-backend-security-group-rules": (
        "true"
    ),
}
if any(annotations.get(key) != value for key, value in expected_annotations.items()):
    raise SystemExit(1)

conflicting_annotations = {
    "service.beta.kubernetes.io/aws-load-balancer-security-groups",
    "service.beta.kubernetes.io/aws-load-balancer-disable-nlb-sg",
    "service.beta.kubernetes.io/aws-load-balancer-alpn-policy",
    "service.beta.kubernetes.io/aws-load-balancer-proxy-protocol",
    "service.beta.kubernetes.io/aws-load-balancer-proxy-protocol-per-target-group",
    "service.beta.kubernetes.io/aws-load-balancer-target-group-attributes",
    "service.beta.kubernetes.io/load-balancer-source-ranges",
    (
        "service.beta.kubernetes.io/"
        "aws-load-balancer-inbound-sg-rules-on-private-link-traffic"
    ),
}
if any(annotations.get(key) not in (None, "") for key in conflicting_annotations):
    raise SystemExit(1)

if spec.get("loadBalancerSourceRanges") not in (None, []):
    raise SystemExit(1)
if spec.get("loadBalancerClass") not in (None, "", "service.k8s.aws/nlb"):
    raise SystemExit(1)
if spec.get("externalIPs") not in (None, []):
    raise SystemExit(1)
if spec.get("ipFamilies") not in (None, ["IPv4"]):
    raise SystemExit(1)
if spec.get("ipFamilyPolicy") not in (None, "SingleStack"):
    raise SystemExit(1)

prefix_annotation = annotations.get(
    "service.beta.kubernetes.io/aws-load-balancer-security-group-prefix-lists", ""
)
actual_prefix_lists = prefix_annotation.split(",") if prefix_annotation else []
expected_prefix_lists = os.environ["PORTKEY_EXPECTED_PREFIX_LIST_IDS"].split(",")
if (
    not actual_prefix_lists
    or len(actual_prefix_lists) != len(set(actual_prefix_lists))
):
    raise SystemExit(1)

ports = spec.get("ports")
if not isinstance(ports, list) or len(ports) != 1:
    raise SystemExit(1)
port = ports[0]
if (
    port.get("name") != "gateway"
    or port.get("port") != 443
    or port.get("protocol") != "TCP"
    or port.get("targetPort") != "gateway"
):
    raise SystemExit(1)

current_certificate = annotations.get(
    "service.beta.kubernetes.io/aws-load-balancer-ssl-cert", ""
)
if not current_certificate:
    raise SystemExit(1)
certificate_changed = current_certificate != os.environ[
    "PORTKEY_EXPECTED_CERTIFICATE_ARN"
]
prefix_lists_changed = set(actual_prefix_lists) != set(expected_prefix_lists)
if certificate_changed or prefix_lists_changed:
    if os.environ["PORTKEY_SERVICE_VERIFY_MODE"] == "post":
        raise SystemExit(1)
    if os.environ["PORTKEY_TLS_UPDATE_CONFIRMATION"] != os.environ[
        "PORTKEY_EXPECTED_TLS_UPDATE_CONFIRMATION"
    ]:
        raise SystemExit(1)
PY
  then
    if [[ "$mode" == post ]]; then
      die 'the Helm release changed, but the resulting gateway Service is not the reviewed private NLB TLS shape; inspect the release and Service, then correct it through the reviewed TLS path. Do not roll back to a revision containing the legacy plaintext Service'
    fi
    die "existing Portkey exposure cannot be updated safely in place; for a legacy port-80 Service, verify ALB and PrivateLink dependencies, delete only $PORTKEY_NAMESPACE/$PORTKEY_GATEWAY_SERVICE through the documented migration, wait for its old NLB to disappear, then continue the documented lbc-deploy followed by helm-deploy sequence. For a reviewed TLS certificate or prefix-list rotation, set CONFIRM_PORTKEY_NLB_TLS_UPDATE=$PORTKEY_NAMESPACE/$PORTKEY_GATEWAY_SERVICE (no resources were changed)"
  fi
}

require_safe_walkthrough_lbc_start_scope() {
  local bindings_json gateway_api_resources gateway_resource gateways namespace_objects tgb_crd
  local -a scope_args

  # The controller that this workflow installs will watch Portkey only. If an
  # existing damaged Deployment does not prove that same scope, inspect all
  # namespaces before narrowing it so unrelated dependents cannot be orphaned.
  scope_args=(-n "$PORTKEY_NAMESPACE")
  if load_balancer_controller_exists && \
    ! load_balancer_controller_has_exact_portkey_watch; then
    scope_args=(--all-namespaces)
  fi

  # This validates the known gateway if present and rejects every legacy or
  # drifted exposure, including the PR #29 plaintext Service.
  require_safe_nlb_service_upgrade pre
  gateway_resource="$(kubectl -n "$PORTKEY_NAMESPACE" get service \
    "$PORTKEY_GATEWAY_SERVICE" --ignore-not-found -o name)" || \
    die 'could not determine whether the reviewed Portkey gateway Service exists'
  namespace_objects="$(kubectl get services,ingresses.networking.k8s.io \
    "${scope_args[@]}" --ignore-not-found -o json)" || \
    die 'could not inspect controller dependents before starting the walkthrough-managed controller'
  PORTKEY_NAMESPACE_OBJECTS="$namespace_objects" \
    PORTKEY_EXPECTED_NAMESPACE="$PORTKEY_NAMESPACE" \
    PORTKEY_GATEWAY_SERVICE="$PORTKEY_GATEWAY_SERVICE" python3 - <<'PY' || \
    die 'another Ingress or LoadBalancer Service would be orphaned or reconciled by the walkthrough-managed controller; no resources were changed'
import json
import os

items = json.loads(os.environ["PORTKEY_NAMESPACE_OBJECTS"]).get("items")
if not isinstance(items, list):
    raise SystemExit(1)
for item in items:
    kind = item.get("kind")
    metadata = item.get("metadata", {})
    if kind == "Ingress":
        raise SystemExit(1)
    if kind == "Service" and item.get("spec", {}).get("type") == "LoadBalancer":
        if (
            metadata.get("namespace") != os.environ["PORTKEY_EXPECTED_NAMESPACE"]
            or metadata.get("name") != os.environ["PORTKEY_GATEWAY_SERVICE"]
        ):
            raise SystemExit(1)
PY

  tgb_crd="$(kubectl get crd targetgroupbindings.elbv2.k8s.aws \
    --ignore-not-found -o name)" || \
    die 'could not determine whether TargetGroupBinding resources exist before starting the controller'
  if [[ -n "$tgb_crd" ]]; then
    bindings_json="$(kubectl get targetgroupbindings.elbv2.k8s.aws \
      "${scope_args[@]}" --ignore-not-found -o json)" || \
      die 'could not inspect TargetGroupBinding dependents before starting the controller'
    PORTKEY_TARGET_GROUP_BINDINGS="$bindings_json" \
      PORTKEY_GATEWAY_EXISTS="$([[ -n "$gateway_resource" ]] && printf true || printf false)" \
      PORTKEY_EXPECTED_NAMESPACE="$PORTKEY_NAMESPACE" \
      PORTKEY_GATEWAY_SERVICE="$PORTKEY_GATEWAY_SERVICE" python3 - <<'PY' || \
      die 'a stale or unrelated TargetGroupBinding would be reconciled by the walkthrough-managed controller; no resources were changed'
import json
import os

items = json.loads(os.environ["PORTKEY_TARGET_GROUP_BINDINGS"]).get("items")
if not isinstance(items, list) or len(items) > 1:
    raise SystemExit(1)
for item in items:
    metadata = item.get("metadata", {})
    service_ref = item.get("spec", {}).get("serviceRef", {})
    labels = metadata.get("labels", {})
    if (
        os.environ["PORTKEY_GATEWAY_EXISTS"] != "true"
        or metadata.get("namespace") != os.environ["PORTKEY_EXPECTED_NAMESPACE"]
        or service_ref.get("name") != os.environ["PORTKEY_GATEWAY_SERVICE"]
        or service_ref.get("port") != 443
        or labels.get("service.k8s.aws/stack-namespace")
        != os.environ["PORTKEY_EXPECTED_NAMESPACE"]
        or labels.get("service.k8s.aws/stack-name")
        != os.environ["PORTKEY_GATEWAY_SERVICE"]
    ):
        raise SystemExit(1)
PY
  fi

  gateway_api_resources="$(kubectl api-resources \
    --api-group=gateway.networking.k8s.io --namespaced=true -o name)" || \
    die 'could not inspect Gateway API availability before starting the controller'
  if [[ "$gateway_api_resources" == *gateways.gateway.networking.k8s.io* ]]; then
    gateways="$(kubectl get gateways.gateway.networking.k8s.io \
      "${scope_args[@]}" -o name)" || \
      die 'could not inspect Gateway API dependents before starting the controller'
    [[ -z "$gateways" ]] || \
      die 'Gateway API dependents would be orphaned or reconciled by the walkthrough-managed controller; no resources were changed'
  fi
}

load_balancer_controller_plan() (
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
)

install_load_balancer_controller() {
  confirm_write; require_command aws; require_eksctl; require_command helm; require_command kubectl
  aws_check; kube_context
  validate_nlb_tls_aws
  local controller_present=false controller_ready=false
  if load_balancer_controller_exists; then
    controller_present=true
    if load_balancer_controller_is_ready; then
      if load_balancer_controller_service_account_is_managed; then
        load_balancer_controller_is_helm_owned || \
          die 'refusing to reuse a walkthrough-managed controller that is not owned by the expected Helm release'
        load_balancer_controller_is_compatible true || \
          die 'refusing to reuse a drifted walkthrough-managed AWS Load Balancer Controller deployment'
      else
        load_balancer_controller_is_compatible false || \
          die 'refusing to reuse an incompatible externally managed AWS Load Balancer Controller deployment'
      fi
      controller_ready=true
    elif ! load_balancer_controller_is_helm_owned || \
      ! load_balancer_controller_service_account_is_managed; then
      die 'an unready AWS Load Balancer Controller deployment exists but is not owned by this walkthrough; repair it with the cluster owner before retrying'
    else
      printf 'Retrying the existing walkthrough-managed AWS Load Balancer Controller release.\n'
    fi
  fi

  if [[ "$controller_ready" == false ]]; then
    require_safe_walkthrough_lbc_start_scope
    load_balancer_controller_plan
  fi

  local account_id partition rendered vpc_id
  account_id="$(aws_cli sts get-caller-identity --query Account --output text)"
  [[ "$account_id" =~ ^[0-9]{12}$ ]] || die 'could not resolve the AWS account ID'
  vpc_id="$(aws_cli eks describe-cluster --name "$PORTKEY_CLUSTER_NAME" \
    --query 'cluster.resourcesVpcConfig.vpcId' --output text)"
  [[ "$vpc_id" =~ ^vpc-[0-9a-f]+$ ]] || die 'could not resolve the EKS cluster VPC ID'
  partition="$(aws_partition_for_region "$AWS_REGION")"
  rendered="$(mktemp)"; trap "rm -f '$rendered'" EXIT
  render_load_balancer_controller_service_account "$rendered" "$account_id" "$vpc_id" "$partition"
  if load_balancer_controller_service_account_exists; then
    if load_balancer_controller_service_account_is_managed; then
      reconcile_walkthrough_lbc_policy "$rendered"
    elif [[ "$controller_ready" == true ]]; then
      [[ "$PORTKEY_EXTERNAL_LBC_POLICY_CONFIRMED" == true ]] || \
        die 'refusing to reuse an externally managed controller until its owner runs this command with PORTKEY_EXTERNAL_LBC_POLICY_CONFIRMED=true after verifying its complete NLB, security-group, TLS, IRSA, boundary, and organization-policy permissions'
      require_load_balancer_controller_tls_permissions external
      printf '%s\n' 'Using an existing externally managed, ready AWS Load Balancer Controller whose attached role policies contain the required NLB TLS actions; permissions boundaries and organization policies remain the cluster owner responsibility.'
      rm -f "$rendered"; trap - EXIT
      return
    else
      die 'refusing to overwrite an existing AWS Load Balancer Controller service account that is not managed by guidance-codex'
    fi
  else
    [[ "$controller_present" == false ]] || \
      die 'the AWS Load Balancer Controller deployment exists without its expected service account; repair it with the cluster owner before retrying'
    require_eksctl_lbc_reconciliation_version
    eksctl utils associate-iam-oidc-provider \
      --cluster "$PORTKEY_CLUSTER_NAME" --region "$AWS_REGION" --approve
    eksctl create iamserviceaccount --config-file "$rendered" \
      --include "$AWS_LBC_NAMESPACE/$AWS_LBC_SERVICE_ACCOUNT" \
      --approve
    validate_walkthrough_lbc_stack
    validate_walkthrough_lbc_policy_ownership
    validate_walkthrough_lbc_policy_matches "$rendered"
  fi

  if [[ "$controller_ready" == true ]]; then
    rm -f "$rendered"; trap - EXIT
    require_load_balancer_controller_tls_permissions
    printf 'Updated and verified the existing walkthrough-managed AWS Load Balancer Controller IAM policy for NLB TLS.\n'
    return
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
  load_balancer_controller_is_helm_owned || \
    die 'the controller release changed, but the resulting Deployment is not owned by the expected Helm release; inspect and correct the release'
  load_balancer_controller_is_compatible true || \
    die 'the controller release changed, but the resulting Deployment does not match the reviewed image, service account, cluster, region, VPC, namespace, or feature configuration; inspect and correct the release'
  rm -f "$rendered"; trap - EXIT
  require_load_balancer_controller_tls_permissions
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
  local scope="${1:-namespace}" namespace_objects gateway_api_resources gateways
  local -a scope_args
  [[ "$scope" == namespace || "$scope" == all ]] || \
    die 'invalid AWS Load Balancer Controller dependency scope'
  if [[ "$scope" == all ]]; then
    scope_args=(--all-namespaces)
  else
    scope_args=(-n "$PORTKEY_NAMESPACE")
  fi
  namespace_objects="$(kubectl get services,ingresses.networking.k8s.io \
    "${scope_args[@]}" --ignore-not-found -o json)" || \
    die 'could not inspect Services and Ingresses in the required controller dependency scope'
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
    gateways="$(kubectl get gateways.gateway.networking.k8s.io \
      "${scope_args[@]}" -o name)" || \
      die 'could not inspect Gateways in the required controller dependency scope'
    [[ -z "$gateways" ]] || \
      die 'Portkey namespace still contains Gateway API resources that may depend on the controller'
  fi
}

load_balancer_controller_cleanup_plan() {
  require_command aws; require_eksctl; require_command helm; require_command kubectl
  aws_check; kube_context

  local bindings dependency_scope=namespace deployment_present=false release_present=false
  local service_account_present=false
  probe_walkthrough_lbc_stack
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
  else
    dependency_scope=all
  fi
  if [[ "$service_account_present" == true ]] && ! load_balancer_controller_service_account_is_managed; then
    die 'refusing to remove an AWS Load Balancer Controller service account that is not managed by guidance-codex'
  fi
  if [[ "$service_account_present" == true ]]; then
    validate_walkthrough_lbc_stack
    validate_walkthrough_lbc_policy_ownership
  fi
  if [[ "$release_present" == true && "$service_account_present" == false ]]; then
    die 'refusing to remove an AWS Load Balancer Controller release without the walkthrough-managed service account ownership marker'
  fi
  if [[ "$release_present" == false && "$service_account_present" == false ]]; then
    [[ "$PORTKEY_LBC_STACK_PRESENT" == false ]] || \
      die 'the walkthrough-managed controller IAM stack still exists without its Kubernetes service account; automated cleanup refuses this orphaned ownership state'
    printf 'No walkthrough-managed AWS Load Balancer Controller resources were found.\n'
    return
  fi

  kubectl get crd targetgroupbindings.elbv2.k8s.aws >/dev/null 2>&1 || \
    die 'cannot prove controller cleanup is safe because the TargetGroupBinding CRD is missing or unreadable'
  bindings="$(kubectl get targetgroupbindings.elbv2.k8s.aws --all-namespaces -o name)" || \
    die 'could not verify AWS Load Balancer Controller dependencies'
  [[ -z "$bindings" ]] || \
    die 'AWS Load Balancer Controller still has TargetGroupBinding dependencies; remove their Services or Ingresses and wait for AWS cleanup before retrying'
  require_no_load_balancer_controller_dependents "$dependency_scope"
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
    validate_walkthrough_lbc_stack
    validate_walkthrough_lbc_policy_ownership
    eksctl delete iamserviceaccount --cluster "$PORTKEY_CLUSTER_NAME" --region "$AWS_REGION" \
      --namespace "$AWS_LBC_NAMESPACE" --name "$AWS_LBC_SERVICE_ACCOUNT" --wait
    probe_walkthrough_lbc_stack
    [[ "$PORTKEY_LBC_STACK_PRESENT" == false ]] || \
      die 'eksctl reported success, but the walkthrough-managed controller IAM stack still exists'
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
  require_safe_nlb_service_upgrade pre
  local values; values="$(mktemp)"; trap "rm -f '$values'" EXIT
  render_values "$values"
  if ! helm upgrade --install "$PORTKEY_HELM_RELEASE" portkey-ai/gateway --version "$PORTKEY_HELM_CHART_VERSION" \
    --namespace "$PORTKEY_NAMESPACE" --create-namespace -f "$values" --wait --timeout 15m; then
    die 'Helm reported failure after it may have changed the Portkey release; inspect the Service, Ingress, and Gateway resources and correct them through the reviewed TLS path. Do not roll back to a revision containing the legacy plaintext Service'
  fi
  require_safe_nlb_service_upgrade post
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
  if [[ -n "$host" ]]; then
    printf 'Internal NLB DNS target (not a certificate-valid client URL): %s\n' "$host"
  fi
  if [[ -n "$PORTKEY_GATEWAY_HOSTNAME" ]]; then
    printf 'Private gateway hostname: %s\n' "$PORTKEY_GATEWAY_HOSTNAME"
    printf 'Expected private Codex base URL: https://%s/v1\n' "$PORTKEY_GATEWAY_HOSTNAME"
  fi
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
  [[ -z "$PORTKEY_BASE_URL" ]] || validate_nlb_tls_static
}

check() { require_command python3; validate_common; validate_target; printf 'Portkey Hybrid Codex configuration is valid.\n'; }

prepare_runtime_url() {
  if [[ -n "$PORTKEY_BASE_URL" ]]; then RUNTIME_URL="$PORTKEY_BASE_URL"; return; fi
  require_command kubectl; kube_context
  kubectl -n "$PORTKEY_NAMESPACE" port-forward "service/$PORTKEY_GATEWAY_SERVICE" 18787:gateway >/dev/null 2>&1 &
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
