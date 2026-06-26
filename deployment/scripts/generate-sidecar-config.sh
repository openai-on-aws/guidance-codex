#!/usr/bin/env bash
# Generate a developer-specific otel-local-config.yaml from the template.
#
# Identity fields (__USER_EMAIL__, __USER_ID__) are derived automatically from
# the active AWS SSO session. Org attributes can be supplied three ways:
#
#   1. --auto-lookup  Pull Department, Organization, CostCenter, Title, Locale
#                     from the IAM Identity Center identity store (requires
#                     sso-admin:ListInstances + identitystore:DescribeUser on
#                     the calling role). Ideal for MDM / fleet scripts run by
#                     an admin role that has IdC read access.
#
#   2. Explicit flags  --department, --team, --cost-center, etc.
#                      Explicit flags win over --auto-lookup for any field
#                      they supply.
#
#   3. Omit            The OTEL collector inserts an empty-string label for
#                      missing fields, which CloudWatch silently ignores.
#
# MDM / fleet usage (Jamf, Intune, shell-script policy):
#   Run this script once per device at enrollment or login using an admin IAM
#   role that has sso-admin:ListInstances + identitystore:DescribeUser access.
#   The generated config is written to --output; the MDM policy can then place
#   it at a known path (e.g. /etc/codex/otel-local-config.yaml) and start the
#   sidecar service. See docs/deploy-identity-center.md §"MDM distribution".

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$SCRIPT_DIR/../templates/otel-local-config.yaml"

err()  { printf '\033[1;31m[ERROR]\033[0m %s\n' "$*" >&2; }
log()  { printf '\033[1;34m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
ok()   { printf '\033[1;32m[OK]\033[0m %s\n' "$*"; }

usage() {
  cat <<'EOF'
Usage: generate-sidecar-config.sh [options]

Generate a rendered otel-local-config.yaml for a specific developer. Identity
fields are auto-derived from the active AWS SSO session. Org fields can be
auto-populated from the IAM Identity Center identity store (--auto-lookup) or
supplied manually.

Required:
  --region REGION          AWS region (e.g. us-west-2)

Identity (auto-derived from SSO session; override if needed):
  --profile PROFILE        AWS named profile (default: codex-bedrock)
  --user-email EMAIL       Override derived email
  --user-id ID             Override derived user ID

Org attributes — choose one source:
  --auto-lookup            Fetch org attrs from the IdC identity store using
                           the SSO username. Requires the calling role to have
                           sso-admin:ListInstances and identitystore:DescribeUser.
                           Explicit flags below override any auto-looked-up value.
  --department DEPT        e.g. Engineering
  --team TEAM              e.g. platform
  --cost-center CC         e.g. CC-9001
  --organization ORG       e.g. ACME
  --location LOC           e.g. Seattle
  --role ROLE              e.g. developer
  --manager MGR            e.g. manager@example.com

Output:
  --output PATH            Write rendered config to PATH
                           (default: deployment/templates/otel-local-config-<user>.yaml)
  -h, --help               Show this help

Examples:
  # Minimal — identity only, no org attributes
  generate-sidecar-config.sh --region us-west-2

  # Auto-populate from IdC (recommended for MDM / fleet scripts)
  generate-sidecar-config.sh --region us-west-2 --auto-lookup

  # Auto-lookup with one field overridden
  generate-sidecar-config.sh --region us-west-2 --auto-lookup --team infra

  # Fully manual
  generate-sidecar-config.sh --region us-west-2 \
    --department Engineering --team platform --cost-center CC-9001

  # MDM fleet: run per-device under an admin profile, write to shared path
  generate-sidecar-config.sh --region us-west-2 --profile admin-readonly \
    --auto-lookup --output /etc/codex/otel-local-config.yaml
EOF
}

# Defaults
region=""
profile="codex-bedrock"
user_email=""
user_id=""
department=""
team_id=""
cost_center=""
organization=""
location=""
role=""
manager=""
output=""
auto_lookup=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region)       region="${2:?--region requires a value}"; shift 2;;
    --profile)      profile="${2:?--profile requires a value}"; shift 2;;
    --user-email)   user_email="${2:?--user-email requires a value}"; shift 2;;
    --user-id)      user_id="${2:?--user-id requires a value}"; shift 2;;
    --auto-lookup)  auto_lookup=true; shift;;
    --department)   department="${2:?--department requires a value}"; shift 2;;
    --team)         team_id="${2:?--team requires a value}"; shift 2;;
    --cost-center)  cost_center="${2:?--cost-center requires a value}"; shift 2;;
    --organization) organization="${2:?--organization requires a value}"; shift 2;;
    --location)     location="${2:?--location requires a value}"; shift 2;;
    --role)         role="${2:?--role requires a value}"; shift 2;;
    --manager)      manager="${2:?--manager requires a value}"; shift 2;;
    --output)       output="${2:?--output requires a value}"; shift 2;;
    -h|--help) usage; exit 0;;
    *) err "Unknown flag: $1"; echo "Run with --help for usage." >&2; exit 2;;
  esac
done

if [[ -z "$region" ]]; then
  err "--region is required."
  echo "Run with --help for usage." >&2
  exit 2
fi

if ! [[ "$region" =~ ^[a-z]{2}-[a-z]+-[0-9]+$ ]]; then
  err "Invalid --region value: '$region' (expected format like 'us-west-2')."
  exit 1
fi

if [[ ! -f "$TEMPLATE" ]]; then
  err "Template not found: $TEMPLATE"
  exit 1
fi

if [[ -z "$user_email" ]] || [[ -z "$user_id" ]] || [[ "$auto_lookup" == "true" ]]; then
  if ! command -v aws >/dev/null 2>&1; then
    err "AWS CLI v2 is required but was not found in PATH."
    exit 1
  fi
fi

if [[ "$auto_lookup" == "true" ]]; then
  if ! command -v python3 >/dev/null 2>&1; then
    err "python3 is required for --auto-lookup JSON parsing but was not found in PATH."
    exit 1
  fi
fi

# ---------------------------------------------------------------------------
# Derive identity from the SSO session (skipped when both are already set)
# ---------------------------------------------------------------------------
if [[ -z "$user_email" ]] || [[ -z "$user_id" ]] || [[ "$auto_lookup" == "true" ]]; then
  log "Deriving identity from AWS profile '$profile'..."

  if ! caller_arn=$(aws sts get-caller-identity --profile "$profile" \
        --query Arn --output text 2>/dev/null); then
    err "Could not get caller identity for profile '$profile'."
    err "Run: aws sso login --profile $profile"
    exit 1
  fi

  # SSO ARN format:
  #   arn:aws:sts::<account>:assumed-role/AWSReservedSSO_<permset>_<hex>/<username>
  # The session name (after the final '/') is the SSO username, typically an email.
  sso_username="${caller_arn##*/}"

  [[ -z "$user_email" ]] && user_email="$sso_username" && log "Derived user.email: $user_email"

  if [[ -z "$user_id" ]]; then
    # Stable unique ID: role/username portion of the ARN, without account number.
    role_path=$(echo "$caller_arn" | sed 's|.*assumed-role/||')
    user_id="$role_path"
    log "Derived user.id: $user_id"
  fi
fi

# ---------------------------------------------------------------------------
# Auto-lookup org attributes from the IdC identity store
# ---------------------------------------------------------------------------
if [[ "$auto_lookup" == "true" ]]; then
  log "Looking up org attributes for '$sso_username' in IdC identity store..."

  # Discover IdC instance (control-plane is always us-east-1)
  idc_json=$(aws sso-admin list-instances --region us-east-1 \
    --profile "$profile" --output json 2>/dev/null || true)

  if [[ -z "$idc_json" ]] || [[ "$idc_json" == "null" ]]; then
    err "Could not discover IdC instance. Check that the profile has sso-admin:ListInstances."
    err "Continuing without org attributes."
    auto_lookup=false
  else
    identity_store_id=$(echo "$idc_json" | \
      python3 -c "import sys,json; print(json.load(sys.stdin)['Instances'][0]['IdentityStoreId'])" 2>/dev/null || true)

    if [[ -z "$identity_store_id" ]]; then
      err "No IdC instance found. Continuing without org attributes."
      auto_lookup=false
    else
      log "Identity store: $identity_store_id"

      # Resolve the IdC UserId from the SSO username (UserName field)
      idc_user_json=$(aws identitystore list-users \
        --identity-store-id "$identity_store_id" \
        --filters "AttributePath=UserName,AttributeValue=${sso_username}" \
        --region us-east-1 --profile "$profile" --output json 2>/dev/null || true)

      idc_user_id=$(echo "$idc_user_json" | \
        python3 -c "import sys,json; users=json.load(sys.stdin).get('Users',[]); print(users[0]['UserId'] if users else '')" 2>/dev/null || true)

      if [[ -z "$idc_user_id" ]]; then
        log "User '$sso_username' not found in identity store. Continuing without org attributes."
      else
        log "IdC UserId: $idc_user_id"

        # Fetch full user record including enterprise extension
        user_json=$(aws identitystore describe-user \
          --identity-store-id "$identity_store_id" \
          --user-id "$idc_user_id" \
          --extensions aws:identitystore:enterprise \
          --region us-east-1 --profile "$profile" --output json 2>/dev/null || true)

        if [[ -n "$user_json" ]]; then
          # Parse standard fields and enterprise extension with python3
          # Enterprise extension carries Department, Organization, CostCenter,
          # Manager, Division — synced from IdP via SCIM.
          parsed=$(echo "$user_json" | python3 - <<'PYEOF'
import sys, json

u = json.load(sys.stdin)

def first(lst, key):
    return (lst or [{}])[0].get(key, "")

# Standard SCIM fields
email   = first(u.get("Emails", []), "Value")
dept    = ""
org     = ""
cc      = ""
loc     = u.get("Locale", "") or first(u.get("Addresses", []), "Locality")
role_   = u.get("Title", "")

# Enterprise extension (aws:identitystore:enterprise), synced via SCIM
ext = (u.get("Extensions") or {}).get("aws:identitystore:enterprise") or {}
dept = ext.get("Department", "") or dept
org  = ext.get("Organization", "") or org
cc   = ext.get("CostCenter", "") or cc
# Manager comes as an object with value/displayName
mgr_obj = ext.get("Manager") or {}
if isinstance(mgr_obj, dict):
    mgr = mgr_obj.get("value", "") or mgr_obj.get("displayName", "")
else:
    mgr = str(mgr_obj)

print(f"LOOKUP_EMAIL={email}")
print(f"LOOKUP_DEPARTMENT={dept}")
print(f"LOOKUP_ORGANIZATION={org}")
print(f"LOOKUP_COST_CENTER={cc}")
print(f"LOOKUP_LOCATION={loc}")
print(f"LOOKUP_ROLE={role_}")
print(f"LOOKUP_MANAGER={mgr}")
PYEOF
          )

          # Source the parsed values; explicit CLI flags take priority
          eval "$parsed"
          [[ -z "$user_email"    && -n "${LOOKUP_EMAIL:-}"        ]] && user_email="$LOOKUP_EMAIL"
          [[ -z "$department"    && -n "${LOOKUP_DEPARTMENT:-}"   ]] && department="$LOOKUP_DEPARTMENT"
          [[ -z "$organization"  && -n "${LOOKUP_ORGANIZATION:-}" ]] && organization="$LOOKUP_ORGANIZATION"
          [[ -z "$cost_center"   && -n "${LOOKUP_COST_CENTER:-}"  ]] && cost_center="$LOOKUP_COST_CENTER"
          [[ -z "$location"      && -n "${LOOKUP_LOCATION:-}"     ]] && location="$LOOKUP_LOCATION"
          [[ -z "$role"          && -n "${LOOKUP_ROLE:-}"         ]] && role="$LOOKUP_ROLE"
          [[ -z "$manager"       && -n "${LOOKUP_MANAGER:-}"      ]] && manager="$LOOKUP_MANAGER"

          log "Org attributes populated from IdC identity store."
        fi
      fi
    fi
  fi
fi

# ---------------------------------------------------------------------------
# Default output path
# ---------------------------------------------------------------------------
if [[ -z "$output" ]]; then
  safe_name="${user_email//@/-}"
  safe_name="${safe_name//./-}"
  output="$SCRIPT_DIR/../templates/otel-local-config-${safe_name}.yaml"
fi

# ---------------------------------------------------------------------------
# Render template — substitute all placeholders
# ---------------------------------------------------------------------------
log "Rendering template → $output"

sed \
  -e "s|__AWS_REGION__|${region}|g" \
  -e "s|__USER_EMAIL__|${user_email}|g" \
  -e "s|__USER_ID__|${user_id}|g" \
  -e "s|__DEPARTMENT__|${department}|g" \
  -e "s|__TEAM_ID__|${team_id}|g" \
  -e "s|__COST_CENTER__|${cost_center}|g" \
  -e "s|__ORGANIZATION__|${organization}|g" \
  -e "s|__LOCATION__|${location}|g" \
  -e "s|__ROLE__|${role}|g" \
  -e "s|__MANAGER__|${manager}|g" \
  "$TEMPLATE" > "$output"

ok "Generated: $output"

echo ""
echo "  user.email:   ${user_email}"
echo "  user.id:      ${user_id}"
echo "  department:   ${department:-<empty>}"
echo "  team.id:      ${team_id:-<empty>}"
echo "  cost_center:  ${cost_center:-<empty>}"
echo "  organization: ${organization:-<empty>}"
echo "  location:     ${location:-<empty>}"
echo "  role:         ${role:-<empty>}"
echo "  manager:      ${manager:-<empty>}"
echo ""
echo "Start the sidecar with:"
echo "  otelcol-local-<platform> --config \"$output\""
