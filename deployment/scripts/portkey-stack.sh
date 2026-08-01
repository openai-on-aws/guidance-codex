#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${PORTKEY_ENV_FILE:-$ROOT_DIR/deployment/portkey/.env.deploy}"
TEMPLATE="$ROOT_DIR/deployment/portkey/bedrock-mantle-role.yaml"
CONTRACT_PROBE="$ROOT_DIR/deployment/scripts/validate-responses-contract.py"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

AWS_REGION="${AWS_REGION:-us-east-1}"
export AWS_REGION
PORTKEY_STACK_NAME="${PORTKEY_STACK_NAME:-codex-portkey-bedrock-mantle}"
PORTKEY_BASE_URL="${PORTKEY_BASE_URL:-https://api.portkey.ai/v1}"
PORTKEY_PROVIDER_SLUG="${PORTKEY_PROVIDER_SLUG:-}"
PORTKEY_MODEL="${PORTKEY_MODEL:-}"
MANTLE_MODEL_ID="openai.gpt-5.5"
BEDROCK_MANTLE_PROJECT_ID="${BEDROCK_MANTLE_PROJECT_ID:-*}"

AWS_GLOBAL_ARGS=(--region "$AWS_REGION")
if [[ -n "${AWS_PROFILE:-}" ]]; then
  AWS_GLOBAL_ARGS=(--profile "$AWS_PROFILE" "${AWS_GLOBAL_ARGS[@]}")
fi

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null || die "$1 is required"
}

require_value() {
  local name="$1"
  [[ -n "${!name:-}" ]] || die "set $name in $ENV_FILE or the environment"
}

aws_cli() {
  aws "${AWS_GLOBAL_ARGS[@]}" "$@"
}

help_text() {
  cat <<'EOF'
Portkey hosted Bedrock Mantle evaluation helper

Usage: deployment/scripts/portkey-stack.sh <command>

AWS role lifecycle:
  aws-check      Validate AWS role inputs and caller identity.
  plan           Validate the CloudFormation template without changing AWS.
  deploy         Create or update the Portkey-assumable IAM role.
  status         Print stack status and non-secret provider setup outputs.
  cleanup-plan   List the resources that cleanup will delete.
  cleanup        Delete the IAM role stack and wait for completion.

Portkey validation:
  check          Validate the hosted Portkey and Bedrock Mantle configuration.
  codex-config   Print the user-level Codex provider configuration.
  validate       Require the exact catalog model, then run Responses, reasoning,
                 continuation, stream,
                 and function-tool contract probe.
  codex-validate Run an isolated codex exec file-read/tool/write task.
  auth-negative  Confirm an intentionally invalid Portkey key is rejected.

The hosted path requires a Portkey workspace key and a Model Catalog provider
of type Bedrock Mantle. Hybrid deployment is intentionally out of scope.
EOF
}

validate_external_id() {
  require_value PORTKEY_EXTERNAL_ID
  ((${#PORTKEY_EXTERNAL_ID} >= 16)) ||
    die "PORTKEY_EXTERNAL_ID must be at least 16 characters"
  [[ "$PORTKEY_EXTERNAL_ID" =~ ^[A-Za-z0-9+=,.@:/_-]+$ ]] ||
    die "PORTKEY_EXTERNAL_ID contains unsupported characters"
}

validate_aws_inputs() {
  require_command aws
  require_command python3
  [[ "$AWS_REGION" == "us-east-1" ]] ||
    die "AWS_REGION must be us-east-1 for this validation path"
  [[ "$PORTKEY_STACK_NAME" =~ ^[A-Za-z][-A-Za-z0-9]{0,127}$ ]] ||
    die "PORTKEY_STACK_NAME is not a valid CloudFormation stack name"
  require_value PORTKEY_AWS_PRINCIPAL_ARN
  [[ "$PORTKEY_AWS_PRINCIPAL_ARN" =~ ^arn:(aws|aws-us-gov):iam::[0-9]{12}:(role|root)(/.*)?$ ]] ||
    die "PORTKEY_AWS_PRINCIPAL_ARN must be an IAM role or root ARN"
  validate_external_id
  [[ "$BEDROCK_MANTLE_PROJECT_ID" == "*" ||
    "$BEDROCK_MANTLE_PROJECT_ID" =~ ^proj_[A-Za-z0-9_-]+$ ]] ||
    die "BEDROCK_MANTLE_PROJECT_ID must be * or a proj_... identifier"
}

aws_check() {
  validate_aws_inputs
  aws_cli sts get-caller-identity >/dev/null ||
    die "AWS credentials are not authenticated for $AWS_REGION"
  printf 'AWS role inputs and caller identity are valid.\n'
}

plan() {
  aws_check
  aws_cli cloudformation validate-template \
    --template-body "file://$TEMPLATE" >/dev/null
  printf 'CloudFormation template is valid.\n'
  printf 'Planned stack: %s\n' "$PORTKEY_STACK_NAME"
  printf 'Region: %s\nModel: %s\nProject scope: %s\n' \
    "$AWS_REGION" "$MANTLE_MODEL_ID" "$BEDROCK_MANTLE_PROJECT_ID"
  printf 'Run make portkey-aws-deploy to create or update the role.\n'
}

write_parameters() {
  local destination="$1"
  PORTKEY_PARAMETERS_FILE="$destination" python3 - <<'PY'
import json
import os

parameters = [
    {"ParameterKey": "PortkeyPrincipalArn", "ParameterValue": os.environ["PORTKEY_AWS_PRINCIPAL_ARN"]},
    {"ParameterKey": "ExternalId", "ParameterValue": os.environ["PORTKEY_EXTERNAL_ID"]},
    {"ParameterKey": "AwsRegion", "ParameterValue": os.environ["AWS_REGION"]},
    {"ParameterKey": "MantleModelId", "ParameterValue": "openai.gpt-5.5"},
    {"ParameterKey": "MantleProjectId", "ParameterValue": os.environ.get("BEDROCK_MANTLE_PROJECT_ID", "*")},
]
path = os.environ["PORTKEY_PARAMETERS_FILE"]
with open(path, "w", encoding="utf-8") as handle:
    json.dump(parameters, handle)
os.chmod(path, 0o600)
PY
}

stack_exists() {
  aws_cli cloudformation describe-stacks \
    --stack-name "$PORTKEY_STACK_NAME" >/dev/null 2>&1
}

print_redacted_error() {
  local source="$1"
  PORTKEY_ERROR_FILE="$source" python3 - <<'PY'
import os
from pathlib import Path

text = Path(os.environ["PORTKEY_ERROR_FILE"]).read_text(encoding="utf-8", errors="replace")
for name in ("PORTKEY_EXTERNAL_ID", "PORTKEY_API_KEY"):
    value = os.environ.get(name)
    if value:
        text = text.replace(value, "[redacted]")
print(text.rstrip())
PY
}

deploy() {
  aws_check
  local parameters_file error_file
  parameters_file="$(mktemp)"
  error_file="$(mktemp)"
  trap "rm -f '$parameters_file' '$error_file'" EXIT
  write_parameters "$parameters_file"

  if stack_exists; then
    if ! aws_cli cloudformation update-stack \
      --stack-name "$PORTKEY_STACK_NAME" \
      --template-body "file://$TEMPLATE" \
      --parameters "file://$parameters_file" \
      --capabilities CAPABILITY_IAM \
      --tags Key=Application,Value=guidance-codex-portkey \
      >/dev/null 2>"$error_file"; then
      if grep -q 'No updates are to be performed' "$error_file"; then
        printf 'Stack is already up to date.\n'
        status
        return
      fi
      print_redacted_error "$error_file" >&2
      die "CloudFormation update failed"
    fi
    aws_cli cloudformation wait stack-update-complete \
      --stack-name "$PORTKEY_STACK_NAME"
  else
    if ! aws_cli cloudformation create-stack \
      --stack-name "$PORTKEY_STACK_NAME" \
      --template-body "file://$TEMPLATE" \
      --parameters "file://$parameters_file" \
      --capabilities CAPABILITY_IAM \
      --on-failure DELETE \
      --tags Key=Application,Value=guidance-codex-portkey \
      >/dev/null 2>"$error_file"; then
      print_redacted_error "$error_file" >&2
      die "CloudFormation create failed"
    fi
    aws_cli cloudformation wait stack-create-complete \
      --stack-name "$PORTKEY_STACK_NAME"
  fi
  status
}

status() {
  require_command aws
  stack_exists || die "stack $PORTKEY_STACK_NAME does not exist in $AWS_REGION"
  aws_cli cloudformation describe-stacks \
    --stack-name "$PORTKEY_STACK_NAME" \
    --query 'Stacks[0].{Status:StackStatus,RoleArn:Outputs[?OutputKey==`PortkeyRoleArn`]|[0].OutputValue,Region:Outputs[?OutputKey==`AwsRegion`]|[0].OutputValue,Model:Outputs[?OutputKey==`MantleModelId`]|[0].OutputValue,ProjectScope:Outputs[?OutputKey==`MantleProjectScope`]|[0].OutputValue}' \
    --output table
}

cleanup_plan() {
  require_command aws
  stack_exists || die "stack $PORTKEY_STACK_NAME does not exist in $AWS_REGION"
  printf 'Cleanup will delete stack %s and these resources:\n' "$PORTKEY_STACK_NAME"
  aws_cli cloudformation list-stack-resources \
    --stack-name "$PORTKEY_STACK_NAME" \
    --query 'StackResourceSummaries[].{Type:ResourceType,LogicalId:LogicalResourceId,Status:ResourceStatus}' \
    --output table
}

cleanup() {
  cleanup_plan
  aws_cli cloudformation delete-stack --stack-name "$PORTKEY_STACK_NAME"
  aws_cli cloudformation wait stack-delete-complete \
    --stack-name "$PORTKEY_STACK_NAME"
  printf 'Deleted stack %s.\n' "$PORTKEY_STACK_NAME"
}

validate_portkey_target() {
  [[ "$PORTKEY_BASE_URL" == "https://api.portkey.ai/v1" ]] ||
    die "PORTKEY_BASE_URL must be https://api.portkey.ai/v1 for the hosted path"
  require_value PORTKEY_PROVIDER_SLUG
  [[ "$PORTKEY_PROVIDER_SLUG" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]] ||
    die "PORTKEY_PROVIDER_SLUG must omit @ and contain only letters, numbers, _ or -"
  require_value PORTKEY_MODEL
  [[ "$PORTKEY_MODEL" == "@$PORTKEY_PROVIDER_SLUG/$MANTLE_MODEL_ID" ]] ||
    die "PORTKEY_MODEL must be @$PORTKEY_PROVIDER_SLUG/$MANTLE_MODEL_ID"
}

check() {
  require_command python3
  validate_portkey_target
  require_value PORTKEY_API_KEY
  [[ "$PORTKEY_API_KEY" != *$'\n'* && "$PORTKEY_API_KEY" != *$'\r'* ]] ||
    die "PORTKEY_API_KEY must not contain a newline"
  printf 'Hosted Portkey Bedrock Mantle configuration is ready.\n'
}

codex_config() {
  check
  cat <<EOF
model_provider = "portkey"
model = "$PORTKEY_MODEL"

[model_providers.portkey]
name = "Portkey"
base_url = "$PORTKEY_BASE_URL"
env_key = "PORTKEY_API_KEY"
wire_api = "responses"
EOF
}

validate() {
  check
  GATEWAY_BASE_URL="$PORTKEY_BASE_URL" \
    GATEWAY_MODEL="$PORTKEY_MODEL" \
    python3 "$CONTRACT_PROBE" \
      --api-key-env PORTKEY_API_KEY \
      --header-env x-portkey-api-key=PORTKEY_API_KEY \
      --expected-model "$MANTLE_MODEL_ID" \
      --require-model-listed \
      --require-reasoning \
      --include-tool-call
}

codex_validate() {
  check
  require_command codex
  local fixture output
  fixture="$(mktemp -d)"
  output="$fixture/final-message.txt"
  trap "rm -rf '$fixture'" EXIT
  printf 'PORTKEY_E2E_INPUT\n' >"$fixture/input.txt"

  codex exec \
    --ignore-user-config \
    --ephemeral \
    --skip-git-repo-check \
    --cd "$fixture" \
    --sandbox workspace-write \
    --model "$PORTKEY_MODEL" \
    --config 'model_provider="portkey"' \
    --config 'model_providers.portkey.name="Portkey"' \
    --config "model_providers.portkey.base_url=\"$PORTKEY_BASE_URL\"" \
    --config 'model_providers.portkey.env_key="PORTKEY_API_KEY"' \
    --config 'model_providers.portkey.wire_api="responses"' \
    --output-last-message "$output" \
    - <<'EOF'
Read input.txt with a local tool. Create sentinel.txt containing exactly
PORTKEY_CODEX_E2E_OK followed by a newline, then reply with exactly
PORTKEY_CODEX_E2E_OK.
EOF

  [[ "$(cat "$fixture/sentinel.txt" 2>/dev/null)" == "PORTKEY_CODEX_E2E_OK" ]] ||
    die "codex exec did not create the expected sentinel file"
  [[ "$(tr -d '\r\n' <"$output")" == "PORTKEY_CODEX_E2E_OK" ]] ||
    die "codex exec did not return the expected final response"
  printf 'Isolated codex exec validation passed.\n'
}

auth_negative() {
  require_command python3
  validate_portkey_target
  PORTKEY_NEGATIVE_URL="$PORTKEY_BASE_URL/responses" \
    PORTKEY_NEGATIVE_MODEL="$PORTKEY_MODEL" \
    python3 - <<'PY'
import json
import os
import urllib.error
import urllib.request

request = urllib.request.Request(
    os.environ["PORTKEY_NEGATIVE_URL"],
    data=json.dumps({"model": os.environ["PORTKEY_NEGATIVE_MODEL"], "input": "auth check"}).encode(),
    headers={"Authorization": "Bearer intentionally-invalid", "Content-Type": "application/json"},
    method="POST",
)
try:
    urllib.request.urlopen(request, timeout=30)
except urllib.error.HTTPError as error:
    if error.code in (401, 403):
        print(f"Invalid Portkey key rejected with HTTP {error.code}.")
    else:
        raise SystemExit(f"unexpected HTTP status for invalid key: {error.code}")
else:
    raise SystemExit("invalid Portkey key was accepted")
PY
}

case "${1:-help}" in
  aws-check) aws_check ;;
  plan) plan ;;
  deploy) deploy ;;
  status) status ;;
  cleanup-plan) cleanup_plan ;;
  cleanup) cleanup ;;
  check) check ;;
  codex-config) codex_config ;;
  validate) validate ;;
  codex-validate) codex_validate ;;
  auth-negative) auth_negative ;;
  help|-h|--help) help_text ;;
  *) die "unknown command: $1" ;;
esac
