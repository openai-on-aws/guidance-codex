#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
ENV_FILE="${LITELLM_ENV_FILE:-$REPO_ROOT/deployment/litellm/.env.deploy}"
STATE_FILE="${LITELLM_STATE_FILE:-$REPO_ROOT/deployment/litellm/.deploy-state}"
SECRET_AUTH_HELPER="$SCRIPT_DIR/aws-secret-auth.py"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi
if [[ -f "$STATE_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$STATE_FILE"
  set +a
fi

AWS_REGION="${AWS_REGION:-us-east-1}"
BEDROCK_REGION="${BEDROCK_REGION:-$AWS_REGION}"
NETWORKING_STACK="${NETWORKING_STACK:-codex-networking}"
GATEWAY_STACK="${GATEWAY_STACK:-codex-litellm-gateway}"
LITELLM_REPO="${LITELLM_REPO:-codex-litellm}"
BUILDX_BUILDER="${BUILDX_BUILDER:-codex-builder}"
DESIRED_COUNT="${DESIRED_COUNT:-1}"
MIN_TASK_COUNT="${MIN_TASK_COUNT:-1}"
MAX_TASK_COUNT="${MAX_TASK_COUNT:-2}"
ENABLE_WAF="${ENABLE_WAF:-true}"
ENABLE_TLS="${ENABLE_TLS:-true}"
DB_MULTI_AZ="${DB_MULTI_AZ:-true}"
ASSIGN_PUBLIC_IP="${ASSIGN_PUBLIC_IP:-ENABLED}"

usage() {
  cat <<'EOF'
Usage: litellm-stack.sh <command>

Commands:
  check         Read-only local, AWS identity, Docker, and template checks.
  build         Create/reuse ECR, build the image, and push an immutable tag.
  plan          Create CloudFormation change sets without executing them.
  deploy        Deploy networking and the LiteLLM gateway.
  status        Show stack outputs and ECS service state.
  provision-key Create a scoped LiteLLM key and store it in Secrets Manager.
  codex-config  Print a Codex provider config using runtime secret resolution.
  validate      Run the Responses contract without exposing the admin key.
  cleanup-plan  List resources and retention behavior before stack deletion.
  cleanup       Delete the gateway stack after exact-name confirmation.

Required configuration:
  Copy deployment/litellm/.env.deploy.example to .env.deploy and set:
  AWS_PROFILE, AWS_REGION, LITELLM_BASE_IMAGE, and ALLOWED_CIDR.
  With ENABLE_TLS=true, also set GATEWAY_DOMAIN_NAME and either
  ROUTE53_HOSTED_ZONE_ID or ALB_CERTIFICATE_ARN.
  To avoid creating a VPC, also set EXISTING_VPC_ID and two existing public
  subnet IDs in different availability zones.

Safety:
  build and deploy require CONFIRM_AWS_WRITE=1.
  cleanup requires CONFIRM_STACK_DELETE to exactly match GATEWAY_STACK.
  Networking deletion is opt-in and requires DELETE_NETWORKING=1 plus
  CONFIRM_NETWORKING_DELETE matching NETWORKING_STACK.
  Secret values stay in the authentication command or child-process environment.
  Nothing in this script pushes git commits or branches.
EOF
}

log() { printf '[litellm] %s\n' "$*" >&2; }
die() { printf '[litellm] ERROR: %s\n' "$*" >&2; exit 1; }
require() {
  local name="$1"
  [[ -n "${!name:-}" ]] || die "$name is required; configure $ENV_FILE"
}

resolve_aws_cli() {
  local candidate version
  for candidate in "${AWS_CLI:-}" "$(command -v aws 2>/dev/null || true)" \
    /usr/local/bin/aws /opt/homebrew/bin/aws; do
    [[ -n "$candidate" && -x "$candidate" ]] || continue
    version="$("$candidate" --version 2>&1 || true)"
    if [[ "$version" == aws-cli/2.* ]]; then
      AWS_CLI="$candidate"
      export AWS_CLI
      return
    fi
  done
  die "AWS CLI v2 is required. Install it or set AWS_CLI to its path."
}

aws_cli() {
  "$AWS_CLI" "$@"
}

check_identity() {
  resolve_aws_cli
  local identity
  identity="$(aws_cli sts get-caller-identity --region "$AWS_REGION" --output json)" \
    || die "AWS credentials are unavailable. Refresh AWS_PROFILE=${AWS_PROFILE:-default}."
  AWS_ACCOUNT_ID="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["Account"])' <<<"$identity")"
  export AWS_ACCOUNT_ID
  log "AWS account $AWS_ACCOUNT_ID, profile ${AWS_PROFILE:-default}, region $AWS_REGION"
}

check_docker() {
  command -v docker >/dev/null 2>&1 || die "Docker is not installed."
  python3 - <<'PY' || die "Docker daemon is unavailable or did not respond within 10 seconds."
import subprocess
subprocess.run(
    ["docker", "info"],
    check=True,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    timeout=10,
)
PY
  docker buildx version >/dev/null 2>&1 || die "Docker buildx is unavailable."
}

confirm_write() {
  [[ "${CONFIRM_AWS_WRITE:-0}" == "1" ]] || die \
    "This command writes AWS resources. Re-run with CONFIRM_AWS_WRITE=1."
}

run_check() {
  require LITELLM_BASE_IMAGE
  check_identity
  check_docker
  AWS_CLI="$AWS_CLI" python3 "$SCRIPT_DIR/preflight-litellm.py" --stage build
  python3 "$REPO_ROOT/deployment/scripts/validate-doc-links.py" >/dev/null
  log "Mantle model access is verified after deployment with the Responses contract."
  if command -v cfn-lint >/dev/null 2>&1; then
    cfn-lint \
      "$REPO_ROOT/deployment/infrastructure/networking.yaml" \
      "$REPO_ROOT/deployment/litellm/ecs/litellm-ecs.yaml" \
      --ignore-checks W6001
  else
    log "cfn-lint is not on PATH; CI still enforces template linting."
  fi
  log "Read-only checks passed."
}

run_build() {
  confirm_write
  require LITELLM_BASE_IMAGE
  check_identity
  check_docker
  AWS_CLI="$AWS_CLI" python3 "$SCRIPT_DIR/preflight-litellm.py" --stage build

  local repository_mutability
  if repository_mutability="$(aws_cli ecr describe-repositories \
    --repository-names "$LITELLM_REPO" \
    --region "$AWS_REGION" \
    --query 'repositories[0].imageTagMutability' \
    --output text 2>/dev/null)"; then
    [[ "$repository_mutability" == "IMMUTABLE" ]] || die \
      "Existing ECR repository $LITELLM_REPO is $repository_mutability; use an immutable repository."
  else
    log "Creating immutable ECR repository $LITELLM_REPO"
    aws_cli ecr create-repository \
      --repository-name "$LITELLM_REPO" \
      --image-tag-mutability IMMUTABLE \
      --image-scanning-configuration scanOnPush=true \
      --region "$AWS_REGION" >/dev/null
  fi

  local registry image_tag tagged_image digest
  registry="$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"
  image_tag="${IMAGE_TAG:-$(git -C "$REPO_ROOT" rev-parse --short HEAD)-$(date -u +%Y%m%d%H%M%S)}"
  tagged_image="$registry/$LITELLM_REPO:$image_tag"

  aws_cli ecr get-login-password --region "$AWS_REGION" |
    docker login --username AWS --password-stdin "$registry"
  docker buildx inspect "$BUILDX_BUILDER" >/dev/null 2>&1 ||
    docker buildx create --name "$BUILDX_BUILDER" >/dev/null
  docker buildx build \
    --builder "$BUILDX_BUILDER" \
    --platform linux/amd64 \
    --build-arg "LITELLM_BASE_IMAGE=$LITELLM_BASE_IMAGE" \
    --tag "$tagged_image" \
    --file "$REPO_ROOT/deployment/litellm/Dockerfile" \
    --push \
    "$REPO_ROOT/deployment/litellm"

  digest="$(aws_cli ecr describe-images \
    --repository-name "$LITELLM_REPO" \
    --image-ids "imageTag=$image_tag" \
    --region "$AWS_REGION" \
    --query 'imageDetails[0].imageDigest' \
    --output text)"
  LITELLM_IMAGE="$registry/$LITELLM_REPO@$digest"
  umask 077
  printf 'AWS_ACCOUNT_ID=%q\nLITELLM_IMAGE=%q\nIMAGE_TAG=%q\n' \
    "$AWS_ACCOUNT_ID" "$LITELLM_IMAGE" "$image_tag" >"$STATE_FILE"
  log "Built $LITELLM_IMAGE"
}

gateway_parameters() {
  GATEWAY_PARAMETERS=(
    "NetworkingStackName=$NETWORKING_STACK"
    "EnableOtel=false"
    "DBUsername=litellm"
    "AwsRegion=$BEDROCK_REGION"
    "MantleProjectId=${MANTLE_PROJECT_ID:-default}"
    "LiteLLMImage=$LITELLM_IMAGE"
    "AllowedCidr=$ALLOWED_CIDR"
    "AssignPublicIp=$ASSIGN_PUBLIC_IP"
    "DesiredCount=$DESIRED_COUNT"
    "MinTaskCount=$MIN_TASK_COUNT"
    "MaxTaskCount=$MAX_TASK_COUNT"
    "EnableWaf=$ENABLE_WAF"
    "EnableTls=$ENABLE_TLS"
    "DBMultiAz=$DB_MULTI_AZ"
  )
  if [[ -n "${GATEWAY_DOMAIN_NAME:-}" ]]; then
    GATEWAY_PARAMETERS+=("AlbDomainName=$GATEWAY_DOMAIN_NAME")
  fi
  if [[ -n "${ALB_CERTIFICATE_ARN:-}" ]]; then
    GATEWAY_PARAMETERS+=("AlbCertificateArn=$ALB_CERTIFICATE_ARN")
  fi
  if [[ -n "${ROUTE53_HOSTED_ZONE_ID:-}" ]]; then
    GATEWAY_PARAMETERS+=("Route53HostedZoneId=$ROUTE53_HOSTED_ZONE_ID")
  fi
}

networking_parameters() {
  NETWORKING_PARAMETERS=("VpcCidr=${VPC_CIDR:-10.0.0.0/16}")
  if [[ -n "${EXISTING_VPC_ID:-}" ||
        -n "${EXISTING_PUBLIC_SUBNET_1:-}" ||
        -n "${EXISTING_PUBLIC_SUBNET_2:-}" ]]; then
    require EXISTING_VPC_ID
    require EXISTING_PUBLIC_SUBNET_1
    require EXISTING_PUBLIC_SUBNET_2
    [[ "$EXISTING_PUBLIC_SUBNET_1" != "$EXISTING_PUBLIC_SUBNET_2" ]] ||
      die "EXISTING_PUBLIC_SUBNET_1 and EXISTING_PUBLIC_SUBNET_2 must differ."

    local subnet_json
    subnet_json="$(aws_cli ec2 describe-subnets \
      --subnet-ids "$EXISTING_PUBLIC_SUBNET_1" "$EXISTING_PUBLIC_SUBNET_2" \
      --region "$AWS_REGION" \
      --output json)"
    SUBNET_JSON="$subnet_json" python3 - "$EXISTING_VPC_ID" \
      "$EXISTING_PUBLIC_SUBNET_1" "$EXISTING_PUBLIC_SUBNET_2" \
      <<'PY' || die "Existing VPC/subnet validation failed."
import json
import os
import sys

vpc_id, *expected_subnets = sys.argv[1:]
subnets = json.loads(os.environ["SUBNET_JSON"]).get("Subnets", [])
actual_ids = {subnet["SubnetId"] for subnet in subnets}
if actual_ids != set(expected_subnets):
    raise SystemExit("AWS did not return both configured subnets")
if any(subnet["VpcId"] != vpc_id for subnet in subnets):
    raise SystemExit("configured subnets do not belong to EXISTING_VPC_ID")
if len({subnet["AvailabilityZone"] for subnet in subnets}) != 2:
    raise SystemExit("configured subnets must be in different availability zones")
if any(subnet["State"] != "available" for subnet in subnets):
    raise SystemExit("configured subnets must be available")
PY
    NETWORKING_PARAMETERS+=(
      "ExistingVpcId=$EXISTING_VPC_ID"
      "ExistingPublicSubnet1=$EXISTING_PUBLIC_SUBNET_1"
      "ExistingPublicSubnet2=$EXISTING_PUBLIC_SUBNET_2"
    )
    log "Reusing VPC $EXISTING_VPC_ID with two validated subnets."
  fi
}

check_deploy_inputs() {
  require LITELLM_BASE_IMAGE
  require LITELLM_IMAGE
  require ALLOWED_CIDR
  case "$ENABLE_TLS" in
    true)
      require GATEWAY_DOMAIN_NAME
      if [[ -z "${ALB_CERTIFICATE_ARN:-}" && -z "${ROUTE53_HOSTED_ZONE_ID:-}" ]]; then
        die "Set ROUTE53_HOSTED_ZONE_ID for a managed certificate or ALB_CERTIFICATE_ARN for an existing certificate."
      fi
      ;;
    false)
      if [[ -n "${GATEWAY_DOMAIN_NAME:-}" ||
            -n "${ALB_CERTIFICATE_ARN:-}" ||
            -n "${ROUTE53_HOSTED_ZONE_ID:-}" ]]; then
        die "GATEWAY_DOMAIN_NAME, ALB_CERTIFICATE_ARN, and ROUTE53_HOSTED_ZONE_ID must be blank when ENABLE_TLS=false."
      fi
      log "WARNING: TLS is disabled for a short-lived, CIDR-restricted walkthrough."
      ;;
    *) die "ENABLE_TLS must be true or false." ;;
  esac
  check_identity
  AWS_CLI="$AWS_CLI" python3 "$SCRIPT_DIR/preflight-litellm.py" \
    --stage deploy --check-ecr-image
  gateway_parameters
  networking_parameters
}

run_plan() {
  check_deploy_inputs
  if ! aws_cli cloudformation describe-stacks \
    --stack-name "$NETWORKING_STACK" --region "$AWS_REGION" >/dev/null 2>&1; then
    log "Networking does not exist. Creating a non-executed networking change set."
    aws_cli cloudformation deploy \
      --stack-name "$NETWORKING_STACK" \
      --template-file "$REPO_ROOT/deployment/infrastructure/networking.yaml" \
      --region "$AWS_REGION" \
      --parameter-overrides "${NETWORKING_PARAMETERS[@]}" \
      --no-execute-changeset
    log "Execute the networking change set before planning the gateway."
    return
  fi
  aws_cli cloudformation deploy \
    --stack-name "$GATEWAY_STACK" \
    --template-file "$REPO_ROOT/deployment/litellm/ecs/litellm-ecs.yaml" \
    --capabilities CAPABILITY_NAMED_IAM \
    --region "$AWS_REGION" \
    --parameter-overrides "${GATEWAY_PARAMETERS[@]}" \
    --no-execute-changeset
  log "Gateway change set created but not executed."
}

run_deploy() {
  confirm_write
  check_deploy_inputs
  aws_cli cloudformation deploy \
    --stack-name "$NETWORKING_STACK" \
    --template-file "$REPO_ROOT/deployment/infrastructure/networking.yaml" \
    --region "$AWS_REGION" \
    --parameter-overrides "${NETWORKING_PARAMETERS[@]}" \
    --no-fail-on-empty-changeset
  aws_cli cloudformation deploy \
    --stack-name "$GATEWAY_STACK" \
    --template-file "$REPO_ROOT/deployment/litellm/ecs/litellm-ecs.yaml" \
    --capabilities CAPABILITY_NAMED_IAM \
    --region "$AWS_REGION" \
    --parameter-overrides "${GATEWAY_PARAMETERS[@]}" \
    --no-fail-on-empty-changeset
  run_status
}

stack_output() {
  local key="$1"
  aws_cli cloudformation describe-stacks \
    --stack-name "$GATEWAY_STACK" \
    --region "$AWS_REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$key'].OutputValue" \
    --output text
}

run_status() {
  check_identity
  aws_cli cloudformation describe-stacks \
    --stack-name "$GATEWAY_STACK" \
    --region "$AWS_REGION" \
    --query 'Stacks[0].{Status:StackStatus,Outputs:Outputs}' \
    --output json
}

run_provision_key() {
  confirm_write
  check_identity
  local admin_endpoint secret_id kms_key_arn
  local -a key_parameters
  admin_endpoint="$(stack_output GatewayAdminEndpoint)"
  secret_id="${CODEX_API_SECRET_ID:-$GATEWAY_STACK/codex-walkthrough-key}"
  kms_key_arn="$(stack_output LiteLLMKmsKeyArn)"
  key_parameters=(
    --key-alias "${CODEX_KEY_ALIAS:-codex-walkthrough}"
    --models "${CODEX_KEY_MODELS:-gpt-5.5}"
  )
  [[ -z "${CODEX_KEY_USER_ID:-}" ]] ||
    key_parameters+=(--user-id "$CODEX_KEY_USER_ID")
  [[ -z "${CODEX_KEY_TEAM_ID:-}" ]] ||
    key_parameters+=(--team-id "$CODEX_KEY_TEAM_ID")
  [[ -z "${CODEX_KEY_MAX_BUDGET:-}" ]] ||
    key_parameters+=(--max-budget "$CODEX_KEY_MAX_BUDGET")
  [[ -z "${CODEX_KEY_BUDGET_DURATION:-}" ]] ||
    key_parameters+=(--budget-duration "$CODEX_KEY_BUDGET_DURATION")
  [[ -z "${CODEX_KEY_TPM_LIMIT:-}" ]] ||
    key_parameters+=(--tpm-limit "$CODEX_KEY_TPM_LIMIT")
  [[ -z "${CODEX_KEY_RPM_LIMIT:-}" ]] ||
    key_parameters+=(--rpm-limit "$CODEX_KEY_RPM_LIMIT")

  python3 "$SECRET_AUTH_HELPER" \
    --aws-cli "$AWS_CLI" \
    --region "$AWS_REGION" \
    --secret-id "$GATEWAY_STACK/litellm-secrets" \
    --field LITELLM_MASTER_KEY \
    --profile "${AWS_PROFILE:-default}" \
    exec-env --env LITELLM_MASTER_KEY -- \
    python3 "$SCRIPT_DIR/provision-litellm-key.py" \
      --admin-url "$admin_endpoint" \
      --secret-id "$secret_id" \
      --kms-key-id "$kms_key_arn" \
      --aws-cli "$AWS_CLI" \
      --region "$AWS_REGION" \
      "${key_parameters[@]}"

  umask 077
  printf 'CODEX_API_SECRET_ID=%q\n' "$secret_id" >>"$STATE_FILE"
  log "Scoped Codex key is available through Secrets Manager reference $secret_id."
}

run_codex_config() {
  check_identity
  local endpoint python_cli
  require CODEX_API_SECRET_ID
  endpoint="$(stack_output GatewayEndpoint)"
  python_cli="$(command -v python3)"
  python3 - \
    "$endpoint" \
    "$python_cli" \
    "$SECRET_AUTH_HELPER" \
    "$AWS_CLI" \
    "$CODEX_API_SECRET_ID" \
    "$AWS_REGION" \
    "${AWS_PROFILE:-default}" <<'PY'
import json
import sys

endpoint, python_cli, helper, aws_cli, secret_id, aws_region, aws_profile = sys.argv[1:]
print('model = "gpt-5.5"')
print('model_provider = "litellm-gateway"')
print('web_search = "disabled"')
print()
print("[model_providers.litellm-gateway]")
print('name = "LiteLLM Gateway"')
print(f"base_url = {json.dumps(endpoint)}")
print('wire_api = "responses"')
print()
print("[model_providers.litellm-gateway.auth]")
print(f"command = {json.dumps(python_cli)}")
print(
    "args = ["
    + ", ".join(
        json.dumps(value)
        for value in [
            helper,
            "--aws-cli",
            aws_cli,
            "--region",
            aws_region,
            "--secret-id",
            secret_id,
            "--field",
            "LITELLM_API_KEY",
            "--profile",
            aws_profile,
            "print-token",
        ]
    )
    + "]"
)
print("timeout_ms = 30000")
print("refresh_interval_ms = 300000")
PY
}

run_validate() {
  check_identity
  local endpoint
  endpoint="${GATEWAY_BASE_URL:-$(stack_output GatewayEndpoint)}"
  if [[ -n "${GATEWAY_API_KEY:-}" ]]; then
    GATEWAY_BASE_URL="$endpoint" \
      GATEWAY_MODEL="${GATEWAY_MODEL:-gpt-5.5}" \
      python3 "$SCRIPT_DIR/validate-responses-contract.py" --include-tool-call
    return
  fi

  local secret_id secret_field
  if [[ -n "${CODEX_API_SECRET_ID:-}" ]]; then
    secret_id="$CODEX_API_SECRET_ID"
    secret_field="LITELLM_API_KEY"
  else
    log "No scoped key is configured; using the admin key for this contract probe only."
    secret_id="$GATEWAY_STACK/litellm-secrets"
    secret_field="LITELLM_MASTER_KEY"
  fi
  GATEWAY_BASE_URL="$endpoint" \
    GATEWAY_MODEL="${GATEWAY_MODEL:-gpt-5.5}" \
    python3 "$SECRET_AUTH_HELPER" \
      --aws-cli "$AWS_CLI" \
      --region "$AWS_REGION" \
      --secret-id "$secret_id" \
      --field "$secret_field" \
      --profile "${AWS_PROFILE:-default}" \
      exec-env --env GATEWAY_API_KEY -- \
    python3 "$SCRIPT_DIR/validate-responses-contract.py" --include-tool-call
}

run_cleanup_plan() {
  check_identity
  local stack_status
  stack_status="$(aws_cli cloudformation describe-stacks \
    --stack-name "$GATEWAY_STACK" \
    --region "$AWS_REGION" \
    --query 'Stacks[0].StackStatus' \
    --output text 2>/dev/null)" || die \
      "Gateway stack $GATEWAY_STACK was not found in $AWS_REGION."

  printf '%s\n' \
    "Gateway stack: $GATEWAY_STACK ($stack_status)" \
    "Networking stack: $NETWORKING_STACK" \
    "Delete gateway: CONFIRM_STACK_DELETE=$GATEWAY_STACK make litellm-cleanup" \
    "Delete networking too: add DELETE_NETWORKING=1 and" \
    "  CONFIRM_NETWORKING_DELETE=$NETWORKING_STACK" \
    "" \
    "CloudFormation retention behavior:" \
    "  - RDS creates a final snapshot." \
    "  - KMS keys, Secrets Manager secrets, the log group, and the ALB log" \
    "    bucket are retained." \
    "  - ECR images and a provisioned scoped-key secret are outside stack" \
    "    deletion and remain." \
    "Review and remove retained resources under your organization's data" \
    "retention policy after the stack deletion completes."

  aws_cli cloudformation list-stack-resources \
    --stack-name "$GATEWAY_STACK" \
    --region "$AWS_REGION" \
    --query 'StackResourceSummaries[].{Type:ResourceType,LogicalId:LogicalResourceId,PhysicalId:PhysicalResourceId}' \
    --output table
}

run_cleanup() {
  [[ "${CONFIRM_STACK_DELETE:-}" == "$GATEWAY_STACK" ]] || die \
    "Set CONFIRM_STACK_DELETE=$GATEWAY_STACK to confirm gateway deletion."
  if [[ "${DELETE_NETWORKING:-0}" == "1" ]]; then
    [[ "${CONFIRM_NETWORKING_DELETE:-}" == "$NETWORKING_STACK" ]] || die \
      "Set CONFIRM_NETWORKING_DELETE=$NETWORKING_STACK to confirm networking deletion."
  fi
  check_identity

  local db_id deletion_protection
  db_id="$(aws_cli cloudformation describe-stack-resource \
    --stack-name "$GATEWAY_STACK" \
    --logical-resource-id RDSInstance \
    --region "$AWS_REGION" \
    --query 'StackResourceDetail.PhysicalResourceId' \
    --output text 2>/dev/null || true)"
  if [[ -n "$db_id" && "$db_id" != "None" ]]; then
    deletion_protection="$(aws_cli rds describe-db-instances \
      --db-instance-identifier "$db_id" \
      --region "$AWS_REGION" \
      --query 'DBInstances[0].DeletionProtection' \
      --output text)"
    [[ "$deletion_protection" != "True" ]] || die \
      "RDS deletion protection is enabled. Disable it through an approved stack update before cleanup."
  fi

  log "Deleting gateway stack $GATEWAY_STACK; retained resources are not deleted."
  aws_cli cloudformation delete-stack \
    --stack-name "$GATEWAY_STACK" \
    --region "$AWS_REGION"
  aws_cli cloudformation wait stack-delete-complete \
    --stack-name "$GATEWAY_STACK" \
    --region "$AWS_REGION"
  log "Gateway stack deletion completed."

  if [[ "${DELETE_NETWORKING:-0}" == "1" ]]; then
    log "Deleting networking stack $NETWORKING_STACK."
    aws_cli cloudformation delete-stack \
      --stack-name "$NETWORKING_STACK" \
      --region "$AWS_REGION"
    aws_cli cloudformation wait stack-delete-complete \
      --stack-name "$NETWORKING_STACK" \
      --region "$AWS_REGION"
    log "Networking stack deletion completed."
  else
    log "Networking stack $NETWORKING_STACK was preserved."
  fi

  log "Review retained snapshots, KMS keys, secrets, log groups, ALB log buckets, and ECR images."
}

case "${1:-help}" in
  check) run_check ;;
  build) run_build ;;
  plan) run_plan ;;
  deploy) run_deploy ;;
  status) run_status ;;
  provision-key) run_provision_key ;;
  codex-config) run_codex_config ;;
  validate) run_validate ;;
  cleanup-plan) run_cleanup_plan ;;
  cleanup) run_cleanup ;;
  help|-h|--help) usage ;;
  *) usage >&2; die "unknown command: $1" ;;
esac
