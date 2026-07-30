#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${IDC_ENV_FILE:-$ROOT_DIR/deployment/idc/.env.deploy}"
TEMPLATE="$ROOT_DIR/deployment/infrastructure/bedrock-auth-idc.yaml"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

AWS_PROFILE="${AWS_PROFILE:-default}"
AWS_REGION="${AWS_REGION:-us-east-1}"
IDC_REGION="${IDC_REGION:-us-east-1}"
STACK_NAME="${STACK_NAME:-codex-bedrock-idc-validation}"
POLICY_NAME="${POLICY_NAME:-CodexBedrockValidationInvokePolicy}"
PERMISSION_SET_NAME="${PERMISSION_SET_NAME:-CodexBedrockValidation}"
GROUP_NAME="${GROUP_NAME:-Codex-Bedrock-Validation}"
SESSION_DURATION="${SESSION_DURATION:-PT8H}"
ALLOWED_BEDROCK_REGIONS="${ALLOWED_BEDROCK_REGIONS:-$AWS_REGION}"
ALLOWED_MODEL_ID_PATTERN="${ALLOWED_MODEL_ID_PATTERN:-openai.gpt-*}"
IDC_CLIENT_PROFILE="${IDC_CLIENT_PROFILE:-codex-bedrock-validation}"
BEDROCK_TEST_MODEL="${BEDROCK_TEST_MODEL:-openai.gpt-oss-120b-1:0}"
CODEX_MODEL="${CODEX_MODEL:-openai.gpt-5.5}"
if [[ -n "${AWS_CLI:-}" ]]; then
  :
elif [[ -x /usr/local/bin/aws ]]; then
  AWS_CLI=/usr/local/bin/aws
elif [[ -x /opt/homebrew/bin/aws ]]; then
  AWS_CLI=/opt/homebrew/bin/aws
else
  AWS_CLI="$(command -v aws || true)"
fi
[[ -n "$AWS_CLI" ]] || {
  printf 'error: AWS CLI v2 is required\n' >&2
  exit 1
}
AWS=("$AWS_CLI" --profile "$AWS_PROFILE")

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

require_value() {
  local name="$1"
  [[ -n "${!name:-}" ]] || die "set $name in $ENV_FILE or the environment"
}

instance_arn() {
  "${AWS[@]}" sso-admin list-instances --region "$IDC_REGION" \
    --query 'Instances[0].InstanceArn' --output text
}

identity_store_id() {
  "${AWS[@]}" sso-admin list-instances --region "$IDC_REGION" \
    --query 'Instances[0].IdentityStoreId' --output text
}

account_id() {
  "${AWS[@]}" sts get-caller-identity --query Account --output text
}

stack_output() {
  "${AWS[@]}" cloudformation describe-stacks \
    --stack-name "$STACK_NAME" --region "$AWS_REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

find_permission_set() {
  local instance="$1"
  local arn name
  while read -r arn; do
    [[ -n "$arn" ]] || continue
    name="$("${AWS[@]}" sso-admin describe-permission-set \
      --instance-arn "$instance" --permission-set-arn "$arn" \
      --region "$IDC_REGION" --query 'PermissionSet.Name' --output text)"
    if [[ "$name" == "$PERMISSION_SET_NAME" ]]; then
      printf '%s\n' "$arn"
      return
    fi
  done < <("${AWS[@]}" sso-admin list-permission-sets \
    --instance-arn "$instance" --region "$IDC_REGION" \
    --query 'PermissionSets[]' --output text | tr '\t' '\n')
}

wait_for_assignment() {
  local instance="$1"
  local request_id="$2"
  local status reason
  for _ in {1..60}; do
    status="$("${AWS[@]}" sso-admin describe-account-assignment-creation-status \
      --instance-arn "$instance" \
      --account-assignment-creation-request-id "$request_id" \
      --region "$IDC_REGION" \
      --query 'AccountAssignmentCreationStatus.Status' --output text)"
    case "$status" in
      SUCCEEDED) return ;;
      FAILED)
        reason="$("${AWS[@]}" sso-admin describe-account-assignment-creation-status \
          --instance-arn "$instance" \
          --account-assignment-creation-request-id "$request_id" \
          --region "$IDC_REGION" \
          --query 'AccountAssignmentCreationStatus.FailureReason' --output text)"
        die "account assignment failed: $reason"
        ;;
    esac
    sleep 5
  done
  die "timed out waiting for the account assignment"
}

help_text() {
  cat <<'EOF'
IAM Identity Center validation helper

Usage: deployment/scripts/idc-stack.sh <command>

Commands:
  check         Verify AWS CLI v2, caller identity, IdC instance, and inputs.
  plan          Create a CloudFormation change set without executing it.
  deploy        Deploy the direct permission-set policy baseline.
  provision     Create/reuse an isolated group and permission set, then assign it.
  status        Show the stack, permission set, group, and assignment state.
  client-config Print the AWS CLI and Codex configuration for the test identity.
  validate      Validate an already logged-in IdC client profile and optional Codex task.
EOF
}

check() {
  local version
  version="$("$AWS_CLI" --version 2>&1)"
  [[ "$version" == aws-cli/2.* ]] || die "AWS CLI v2 is required; found $version"
  "${AWS[@]}" sts get-caller-identity --output json >/dev/null
  [[ "$(instance_arn)" != "None" ]] || die "no Identity Center instance in $IDC_REGION"
  [[ "$SESSION_DURATION" =~ ^PT([1-9]|1[0-2])H$ ]] ||
    die "SESSION_DURATION must be PT1H through PT12H"
  printf 'Identity Center preflight passed for account %s.\n' "$(account_id)"
}

deploy_stack() {
  check
  "${AWS[@]}" cloudformation deploy \
    --stack-name "$STACK_NAME" \
    --template-file "$TEMPLATE" \
    --capabilities CAPABILITY_NAMED_IAM \
    --region "$AWS_REGION" \
    --parameter-overrides \
      CreateChainedRole=false \
      PolicyName="$POLICY_NAME" \
      AllowedBedrockRegions="$ALLOWED_BEDROCK_REGIONS" \
      AllowedModelIdPattern="$ALLOWED_MODEL_ID_PATTERN"
  printf 'Deployed %s in %s.\n' "$STACK_NAME" "$AWS_REGION"
}

plan_stack() {
  check
  "${AWS[@]}" cloudformation deploy \
    --stack-name "$STACK_NAME" \
    --template-file "$TEMPLATE" \
    --capabilities CAPABILITY_NAMED_IAM \
    --region "$AWS_REGION" \
    --no-execute-changeset \
    --parameter-overrides \
      CreateChainedRole=false \
      PolicyName="$POLICY_NAME" \
      AllowedBedrockRegions="$ALLOWED_BEDROCK_REGIONS" \
      AllowedModelIdPattern="$ALLOWED_MODEL_ID_PATTERN"
}

provision() {
  check
  require_value IDC_USER_NAME

  local instance store account user_id group_id member_count ps_arn policy_attached
  local assignment_count request_id
  instance="$(instance_arn)"
  store="$(identity_store_id)"
  account="$(account_id)"
  user_id="$("${AWS[@]}" identitystore list-users \
    --identity-store-id "$store" --region "$IDC_REGION" \
    --filters "AttributePath=UserName,AttributeValue=$IDC_USER_NAME" \
    --query 'Users[0].UserId' --output text)"
  [[ "$user_id" != "None" ]] || die "Identity Center user not found: $IDC_USER_NAME"

  group_id="$("${AWS[@]}" identitystore list-groups \
    --identity-store-id "$store" --region "$IDC_REGION" \
    --filters "AttributePath=DisplayName,AttributeValue=$GROUP_NAME" \
    --query 'Groups[0].GroupId' --output text)"
  if [[ "$group_id" == "None" ]]; then
    group_id="$("${AWS[@]}" identitystore create-group \
      --identity-store-id "$store" --display-name "$GROUP_NAME" \
      --description "Isolated Codex on Bedrock validation group" \
      --region "$IDC_REGION" --query GroupId --output text)"
  fi

  member_count="$("${AWS[@]}" identitystore is-member-in-groups \
    --identity-store-id "$store" --member-id "UserId=$user_id" \
    --group-ids "$group_id" --region "$IDC_REGION" \
    --query 'length(Results)' --output text)"
  if [[ "$member_count" == "0" ]]; then
    "${AWS[@]}" identitystore create-group-membership \
      --identity-store-id "$store" --group-id "$group_id" \
      --member-id "UserId=$user_id" --region "$IDC_REGION" >/dev/null
  fi

  ps_arn="$(find_permission_set "$instance")"
  if [[ -z "$ps_arn" ]]; then
    ps_arn="$("${AWS[@]}" sso-admin create-permission-set \
      --instance-arn "$instance" --name "$PERMISSION_SET_NAME" \
      --description "Validated least-privilege Codex access to Amazon Bedrock" \
      --session-duration "$SESSION_DURATION" --region "$IDC_REGION" \
      --query 'PermissionSet.PermissionSetArn' --output text)"
  fi

  policy_attached="$("${AWS[@]}" sso-admin \
    list-customer-managed-policy-references-in-permission-set \
    --instance-arn "$instance" --permission-set-arn "$ps_arn" \
    --region "$IDC_REGION" \
    --query "length(CustomerManagedPolicyReferences[?Name=='$POLICY_NAME' && Path=='/'])" \
    --output text)"
  if [[ "$policy_attached" == "0" ]]; then
    "${AWS[@]}" sso-admin \
      attach-customer-managed-policy-reference-to-permission-set \
      --instance-arn "$instance" --permission-set-arn "$ps_arn" \
      --customer-managed-policy-reference "Name=$POLICY_NAME,Path=/" \
      --region "$IDC_REGION"
  fi

  assignment_count="$("${AWS[@]}" sso-admin list-account-assignments \
    --instance-arn "$instance" --permission-set-arn "$ps_arn" \
    --account-id "$account" --region "$IDC_REGION" \
    --query "length(AccountAssignments[?PrincipalType=='GROUP' && PrincipalId=='$group_id'])" \
    --output text)"
  if [[ "$assignment_count" == "0" ]]; then
    request_id="$("${AWS[@]}" sso-admin create-account-assignment \
      --instance-arn "$instance" --permission-set-arn "$ps_arn" \
      --principal-type GROUP --principal-id "$group_id" \
      --target-type AWS_ACCOUNT --target-id "$account" \
      --region "$IDC_REGION" \
      --query 'AccountAssignmentCreationStatus.RequestId' --output text)"
    wait_for_assignment "$instance" "$request_id"
  else
    "${AWS[@]}" sso-admin provision-permission-set \
      --instance-arn "$instance" --permission-set-arn "$ps_arn" \
      --target-type AWS_ACCOUNT --target-id "$account" \
      --region "$IDC_REGION" >/dev/null
  fi

  printf 'Provisioned %s for group %s in account %s.\n' \
    "$PERMISSION_SET_NAME" "$GROUP_NAME" "$account"
}

status() {
  local instance store account ps_arn group_id
  instance="$(instance_arn)"
  store="$(identity_store_id)"
  account="$(account_id)"
  ps_arn="$(find_permission_set "$instance")"
  group_id="$("${AWS[@]}" identitystore list-groups \
    --identity-store-id "$store" --region "$IDC_REGION" \
    --filters "AttributePath=DisplayName,AttributeValue=$GROUP_NAME" \
    --query 'Groups[0].GroupId' --output text)"
  "${AWS[@]}" cloudformation describe-stacks --stack-name "$STACK_NAME" \
    --region "$AWS_REGION" --query 'Stacks[0].[StackName,StackStatus]' --output table
  printf 'Permission set: %s\nGroup: %s (%s)\n' \
    "${ps_arn:-not found}" "$GROUP_NAME" "$group_id"
  if [[ -n "$ps_arn" && "$group_id" != "None" ]]; then
    "${AWS[@]}" sso-admin list-account-assignments \
      --instance-arn "$instance" --permission-set-arn "$ps_arn" \
      --account-id "$account" --region "$IDC_REGION" \
      --query 'AccountAssignments[].[PrincipalType,PrincipalId]' --output table
  fi
}

client_config() {
  require_value IDC_START_URL
  local account
  account="$(account_id)"
  cat <<EOF
[sso-session codex-bedrock-validation]
sso_start_url = $IDC_START_URL
sso_region = $IDC_REGION
sso_registration_scopes = sso:account:access

[profile $IDC_CLIENT_PROFILE]
sso_session = codex-bedrock-validation
sso_account_id = $account
sso_role_name = $PERMISSION_SET_NAME
region = $AWS_REGION

# ~/.codex/config.toml
model_provider = "amazon-bedrock"
model = "$CODEX_MODEL"

[model_providers.amazon-bedrock.aws]
profile = "$IDC_CLIENT_PROFILE"
region = "$AWS_REGION"
EOF
}

validate_client() {
  "$AWS_CLI" sts get-caller-identity --profile "$IDC_CLIENT_PROFILE" --output json
  "$AWS_CLI" bedrock-runtime converse \
    --profile "$IDC_CLIENT_PROFILE" --region "$AWS_REGION" \
    --model-id "$BEDROCK_TEST_MODEL" \
    --messages '[{"role":"user","content":[{"text":"Reply with VALIDATED only."}]}]' \
    --query 'output.message.content[0].text' --output text

  if [[ "${RUN_CODEX_VALIDATION:-false}" == "true" ]]; then
    command -v codex >/dev/null || die "codex is required for RUN_CODEX_VALIDATION=true"
    local temp_home
    temp_home="$(mktemp -d)"
    trap 'rm -rf "$temp_home"' EXIT
    cat >"$temp_home/config.toml" <<EOF
model_provider = "amazon-bedrock"
model = "$CODEX_MODEL"

[model_providers.amazon-bedrock.aws]
profile = "$IDC_CLIENT_PROFILE"
region = "$AWS_REGION"
EOF
    CODEX_HOME="$temp_home" codex exec --skip-git-repo-check \
      "Reply with IDENTITY_CENTER_VALIDATED only."
  fi
}

case "${1:-help}" in
  check) check ;;
  plan) plan_stack ;;
  deploy) deploy_stack ;;
  provision) provision ;;
  status) status ;;
  client-config) client_config ;;
  validate) validate_client ;;
  help|-h|--help) help_text ;;
  *) die "unknown command: $1" ;;
esac
