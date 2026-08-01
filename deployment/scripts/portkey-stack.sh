#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${PORTKEY_ENV_FILE:-$ROOT_DIR/deployment/portkey/.env.deploy}"
INFRA_TEMPLATE="$ROOT_DIR/deployment/portkey/hybrid-infrastructure.yaml"
CLUSTER_TEMPLATE="$ROOT_DIR/deployment/portkey/eksctl-cluster.yaml.tmpl"
VALUES_TEMPLATE="$ROOT_DIR/deployment/portkey/values.yaml.tmpl"
CONTRACT_PROBE="$ROOT_DIR/deployment/scripts/validate-responses-contract.py"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

AWS_REGION="${AWS_REGION:-us-east-1}"
PORTKEY_CLUSTER_NAME="${PORTKEY_CLUSTER_NAME:-codex-portkey}"
PORTKEY_NAMESPACE="${PORTKEY_NAMESPACE:-portkeyai}"
PORTKEY_SERVICE_ACCOUNT="${PORTKEY_SERVICE_ACCOUNT:-gateway-sa}"
PORTKEY_STACK_NAME="${PORTKEY_STACK_NAME:-codex-portkey-hybrid}"
PORTKEY_HELM_RELEASE="${PORTKEY_HELM_RELEASE:-portkey-ai}"
PORTKEY_HELM_CHART_VERSION="${PORTKEY_HELM_CHART_VERSION:-}"
PORTKEY_GATEWAY_SERVICE="${PORTKEY_HELM_RELEASE}-gateway"
PORTKEY_INTERNAL_NLB="${PORTKEY_INTERNAL_NLB:-true}"
PORTKEY_BASE_URL="${PORTKEY_BASE_URL:-}"
PORTKEY_PROVIDER_SLUG="${PORTKEY_PROVIDER_SLUG:-}"
PORTKEY_MODEL="${PORTKEY_MODEL:-}"
MANTLE_MODEL_ID=openai.gpt-5.5
BEDROCK_MANTLE_PROJECT_ID="${BEDROCK_MANTLE_PROJECT_ID:-*}"
export AWS_REGION

AWS_ARGS=(--region "$AWS_REGION")
[[ -z "${AWS_PROFILE:-}" ]] || AWS_ARGS=(--profile "$AWS_PROFILE" "${AWS_ARGS[@]}")

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
require_command() { command -v "$1" >/dev/null || die "$1 is required"; }
require_value() { local name="$1"; [[ -n "${!name:-}" ]] || die "set $name in $ENV_FILE or the environment"; }
aws_cli() { aws "${AWS_ARGS[@]}" "$@"; }
confirm_write() { [[ "${CONFIRM_AWS_WRITE:-}" == 1 ]] || die 'set CONFIRM_AWS_WRITE=1 for AWS or Kubernetes mutations'; }

help_text() {
  cat <<'EOF'
Portkey Hybrid on Amazon EKS

  cluster-plan      Render and validate the optional eksctl sandbox cluster.
  cluster-deploy    Create the EKS cluster (CONFIRM_AWS_WRITE=1).
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
  [[ "$AWS_REGION" == us-east-1 ]] || die 'AWS_REGION must be us-east-1'
  [[ "$PORTKEY_CLUSTER_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$ ]] || die 'invalid PORTKEY_CLUSTER_NAME'
  [[ "$PORTKEY_NAMESPACE" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] || die 'invalid PORTKEY_NAMESPACE'
  [[ "$PORTKEY_SERVICE_ACCOUNT" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] || die 'invalid PORTKEY_SERVICE_ACCOUNT'
  [[ "$PORTKEY_INTERNAL_NLB" == true || "$PORTKEY_INTERNAL_NLB" == false ]] || die 'PORTKEY_INTERNAL_NLB must be true or false'
  [[ "$BEDROCK_MANTLE_PROJECT_ID" == '*' || "$BEDROCK_MANTLE_PROJECT_ID" =~ ^proj_[A-Za-z0-9_-]+$ ]] || die 'invalid BEDROCK_MANTLE_PROJECT_ID'
}

render_cluster() {
  local output="$1"
  PORTKEY_RENDER_OUTPUT="$output" PORTKEY_CLUSTER_TEMPLATE="$CLUSTER_TEMPLATE" \
    PORTKEY_CLUSTER_NAME="$PORTKEY_CLUSTER_NAME" python3 - <<'PY'
import os
from pathlib import Path
p = Path(os.environ["PORTKEY_CLUSTER_TEMPLATE"]).read_text()
p = p.replace("__CLUSTER_NAME__", os.environ["PORTKEY_CLUSTER_NAME"])
Path(os.environ["PORTKEY_RENDER_OUTPUT"]).write_text(p)
PY
}

cluster_plan() {
  require_command eksctl; require_command python3; validate_common
  local rendered; rendered="$(mktemp)"; trap "rm -f '$rendered'" EXIT
  render_cluster "$rendered"
  eksctl create cluster --config-file "$rendered" --dry-run >/dev/null
  rm -f "$rendered"; trap - EXIT
  printf 'eksctl cluster plan is valid for %s in %s.\n' "$PORTKEY_CLUSTER_NAME" "$AWS_REGION"
}

cluster_deploy() {
  cluster_plan; confirm_write
  local rendered; rendered="$(mktemp)"; trap "rm -f '$rendered'" EXIT
  render_cluster "$rendered"
  eksctl create cluster --config-file "$rendered"
  rm -f "$rendered"; trap - EXIT
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

write_parameters() {
  local output="$1"
  PORTKEY_PARAMETERS_FILE="$output" \
    BEDROCK_MANTLE_PROJECT_ID="$BEDROCK_MANTLE_PROJECT_ID" python3 - <<'PY'
import json, os
values = {
 "MantleProjectId": os.environ["BEDROCK_MANTLE_PROJECT_ID"],
 "MantleModelId": "openai.gpt-5.5",
}
with open(os.environ["PORTKEY_PARAMETERS_FILE"], "w") as f:
 json.dump([{"ParameterKey": k, "ParameterValue": v} for k, v in values.items()], f)
os.chmod(os.environ["PORTKEY_PARAMETERS_FILE"], 0o600)
PY
}

plan() {
  aws_check
  aws_cli cloudformation validate-template --template-body "file://$INFRA_TEMPLATE" >/dev/null
  printf 'CloudFormation plan is valid: S3 logs + EKS IRSA role, model %s.\n' "$MANTLE_MODEL_ID"
}

stack_exists() { aws_cli cloudformation describe-stacks --stack-name "$PORTKEY_STACK_NAME" >/dev/null 2>&1; }

deploy() {
  plan; confirm_write
  require_command eksctl
  eksctl utils associate-iam-oidc-provider --cluster "$PORTKEY_CLUSTER_NAME" --region "$AWS_REGION" --approve
  local parameters; parameters="$(mktemp)"; trap "rm -f '$parameters'" EXIT
  write_parameters "$parameters"
  aws_cli cloudformation deploy --stack-name "$PORTKEY_STACK_NAME" \
    --template-file "$INFRA_TEMPLATE" --parameter-overrides "file://$parameters" \
    --capabilities CAPABILITY_IAM --no-fail-on-empty-changeset \
    --tags Application=guidance-codex-portkey
  local policy_arn
  policy_arn="$(stack_output GatewayManagedPolicyArn)"
  eksctl create iamserviceaccount --cluster "$PORTKEY_CLUSTER_NAME" --region "$AWS_REGION" \
    --namespace "$PORTKEY_NAMESPACE" --name "$PORTKEY_SERVICE_ACCOUNT" \
    --attach-policy-arn "$policy_arn" --approve --override-existing-serviceaccounts
  rm -f "$parameters"; trap - EXIT
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
  PORTKEY_INTERNAL_NLB="$PORTKEY_INTERNAL_NLB" python3 - <<'PY'
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
 "PORTKEY_SERVICE_ACCOUNT": os.environ["PORTKEY_SERVICE_ACCOUNT"],
 "PORTKEY_SERVICE_ROLE_ARN": os.environ["PORTKEY_SERVICE_ROLE_ARN"],
 "PORTKEY_INTERNAL_NLB": os.environ["PORTKEY_INTERNAL_NLB"],
}
for key, value in mapping.items(): text = text.replace(f"__{key}__", json.dumps(value))
if "__PORTKEY_" in text: raise SystemExit("unresolved Portkey values placeholder")
Path(os.environ["PORTKEY_VALUES_OUTPUT"]).write_text(text)
os.chmod(os.environ["PORTKEY_VALUES_OUTPUT"], 0o600)
PY
}

kube_context() { aws_cli eks update-kubeconfig --name "$PORTKEY_CLUSTER_NAME" >/dev/null; }

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
  [[ -z "$host" ]] || printf 'Gateway URL: http://%s/v1\n' "$host"
}

validate_target() {
  require_value PORTKEY_PROVIDER_SLUG; require_value PORTKEY_MODEL; require_value PORTKEY_API_KEY
  [[ "$PORTKEY_PROVIDER_SLUG" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]] || die 'invalid PORTKEY_PROVIDER_SLUG'
  [[ "$PORTKEY_MODEL" == "@$PORTKEY_PROVIDER_SLUG/$MANTLE_MODEL_ID" ]] || die "PORTKEY_MODEL must be @$PORTKEY_PROVIDER_SLUG/$MANTLE_MODEL_ID"
  [[ -z "$PORTKEY_BASE_URL" || "$PORTKEY_BASE_URL" == */v1 ]] || die 'PORTKEY_BASE_URL must end in /v1'
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
EOF
}

validate() {
  check; prepare_runtime_url; trap stop_tunnel EXIT
  GATEWAY_BASE_URL="$RUNTIME_URL" GATEWAY_MODEL="$PORTKEY_MODEL" python3 "$CONTRACT_PROBE" \
    --api-key-env PORTKEY_API_KEY --header-env x-portkey-api-key=PORTKEY_API_KEY \
    --expected-model "$MANTLE_MODEL_ID" --require-model-listed --require-reasoning --include-tool-call
}

codex_validate() {
  check; require_command codex; local fixture output
  prepare_runtime_url; fixture="$(mktemp -d)"; output="$fixture/final.txt"; trap "stop_tunnel; rm -rf '$fixture'" EXIT
  printf 'PORTKEY_E2E_INPUT\n' >"$fixture/input.txt"
  codex exec --ignore-user-config --ephemeral --skip-git-repo-check --cd "$fixture" --sandbox workspace-write \
    --model "$PORTKEY_MODEL" --config 'model_provider="portkey"' \
    --config 'model_providers.portkey.name="Portkey Hybrid on AWS"' \
    --config "model_providers.portkey.base_url=\"$RUNTIME_URL\"" \
    --config 'model_providers.portkey.env_key="PORTKEY_API_KEY"' \
    --config 'model_providers.portkey.wire_api="responses"' --output-last-message "$output" - <<'EOF'
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
r=urllib.request.Request(os.environ['PORTKEY_NEGATIVE_URL'], data=json.dumps({'model':os.environ['PORTKEY_NEGATIVE_MODEL'],'input':'auth check'}).encode(), headers={'Authorization':'Bearer intentionally-invalid','Content-Type':'application/json'}, method='POST')
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
  require_command eksctl; [[ "${CONFIRM_CLUSTER_DELETE:-}" == "$PORTKEY_CLUSTER_NAME" ]] || die "set CONFIRM_CLUSTER_DELETE=$PORTKEY_CLUSTER_NAME"
  eksctl delete cluster --name "$PORTKEY_CLUSTER_NAME" --region "$AWS_REGION" --wait
}

case "${1:-help}" in
  cluster-plan) cluster_plan ;; cluster-deploy) cluster_deploy ;; aws-check) aws_check ;;
  plan) plan ;; deploy) deploy ;; helm-plan) helm_plan ;; helm-deploy) helm_deploy ;;
  status) status ;; check) check ;; codex-config) codex_config ;; validate) validate ;;
  codex-validate) codex_validate ;; auth-negative) auth_negative ;;
  cleanup-plan) cleanup_plan ;; cleanup) cleanup ;; cluster-cleanup) cluster_cleanup ;;
  help|-h|--help) help_text ;; *) die "unknown command: $1" ;;
esac
