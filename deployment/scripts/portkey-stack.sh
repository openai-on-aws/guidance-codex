#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${PORTKEY_ENV_FILE:-$ROOT_DIR/deployment/portkey/.env.deploy}"
CONTRACT_PROBE="$ROOT_DIR/deployment/scripts/validate-responses-contract.py"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

PORTKEY_BASE_URL="${PORTKEY_BASE_URL:-https://api.portkey.ai/v1}"
PORTKEY_MODEL="${PORTKEY_MODEL:-}"

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

require_value() {
  local name="$1"
  [[ -n "${!name:-}" ]] || die "set $name in $ENV_FILE or the environment"
}

help_text() {
  cat <<'EOF'
Portkey evaluation helper

Usage: deployment/scripts/portkey-stack.sh <command>

Commands:
  check         Check local configuration without sending model traffic.
  codex-config  Print the user-level Codex provider configuration.
  validate      Run the strict Responses, continuation, streaming, and tool probe.

The managed path requires a Portkey workspace API key and a Model Catalog
provider. A hybrid deployment additionally requires Portkey-issued image
credentials, client auth, and an organization ID.
EOF
}

check() {
  command -v python3 >/dev/null || die "python3 is required"
  require_value PORTKEY_MODEL
  [[ "$PORTKEY_BASE_URL" == */v1 ]] || die "PORTKEY_BASE_URL must end in /v1"
  [[ "$PORTKEY_MODEL" == @*/* ]] ||
    die "PORTKEY_MODEL must use @<provider-slug>/<model-id>"
  if [[ -z "${PORTKEY_API_KEY:-}" ]]; then
    printf 'Portkey configuration is structurally valid; PORTKEY_API_KEY is not set.\n'
    return
  fi
  printf 'Portkey configuration is ready for a live contract probe.\n'
}

codex_config() {
  require_value PORTKEY_MODEL
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
  require_value PORTKEY_API_KEY
  GATEWAY_BASE_URL="$PORTKEY_BASE_URL" \
    GATEWAY_MODEL="$PORTKEY_MODEL" \
    python3 "$CONTRACT_PROBE" \
      --api-key-env PORTKEY_API_KEY \
      --include-tool-call
}

case "${1:-help}" in
  check) check ;;
  codex-config) codex_config ;;
  validate) validate ;;
  help|-h|--help) help_text ;;
  *) die "unknown command: $1" ;;
esac
