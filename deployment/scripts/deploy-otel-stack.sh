#!/usr/bin/env bash
# Deploy the Codex-on-Bedrock OTel telemetry for the NATIVE AWS ACCESS path.
#
# Architecture: local sidecar collector (no ECS, no ALB, no VPC).
#   - Each developer runs a small OTel Collector binary on their own machine.
#   - The sidecar receives OTLP from Codex on 127.0.0.1:4318 and exports to the
#     CloudWatch *native OTLP* endpoint (monitoring.<region>.amazonaws.com) using
#     SigV4 auth from the developer's `aws sso login` credentials.
#   - Metrics land in CloudWatch Metrics, queryable via PromQL.
#
# This script only deploys the account-level CloudWatch dashboard. The collector
# itself is per-developer: build it with build-local-collector.sh and ship the
# rendered otel-local-config.yaml + the [otel] config.toml block (see
# docs/QUICKSTART_NATIVE_AWS_ACCESS.md).
#
# Prereqs:
#   - AWS CLI v2 configured against the target account/region (env vars,
#     ~/.aws/credentials, or `aws sso login`)
#   - Developers' IAM role/permission set must allow cloudwatch:PutMetricData
#     (that single action is all the sidecar needs to publish via OTLP).

set -euo pipefail

usage() {
  cat <<'EOF'
Usage: deploy-otel-stack.sh [options]

Deploys the CloudWatch dashboard for Codex-on-Bedrock usage (native AWS access,
local sidecar collector model). No ECS/ALB/VPC is created — telemetry flows from
each developer's local OTel collector straight to the CloudWatch native OTLP
endpoint.

Common options:
  --region REGION              AWS region metrics are ingested in (default: us-west-2).
                               Must match the region baked into each developer's
                               sidecar config (sigv4auth region).
  --aws-profile PROFILE        AWS named profile to use (optional;
                               otherwise uses the default credential chain)
  --stack-prefix PREFIX        Prefix for the dashboard stack (default: codex-otel)
  --dashboard-name NAME        CloudWatch dashboard name (default: CodexOnBedrock)
  -h, --help                   Show this help

Example:
  deploy-otel-stack.sh --region us-west-2 --dashboard-name CodexOnBedrock

After deploy completes, build and distribute the sidecar collector:
  ./build-local-collector.sh --all
Then give each developer the rendered otel-local-config.yaml (substitute
__AWS_REGION__, __USER_EMAIL__, __USER_ID__, and optional org fields
__DEPARTMENT__, __TEAM_ID__, __COST_CENTER__, __ORGANIZATION__, __LOCATION__,
__ROLE__) and this Codex config.toml block.
Codex selects metric, log, and trace exporters separately; the dashboard needs
the metrics exporter pointed at the local sidecar:

  [otel]
  environment = "production"

  [otel.metrics_exporter]
  otlp-http = { endpoint = "http://127.0.0.1:4318/v1/metrics", protocol = "binary" }

The sidecar forwards to https://monitoring.<region>.amazonaws.com via SigV4.
Account-level prerequisite: OTLP metric ingestion must be enabled once per
account (aws cloudwatch start-otel-enrichment + aws observabilityadmin
start-telemetry-enrichment).
EOF
}

region="us-west-2"
aws_profile=""
prefix="codex-otel"
dashboard_name="CodexOnBedrock"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region) region="${2:?--region requires a value}"; shift 2;;
    --aws-profile) aws_profile="${2:?--aws-profile requires a value}"; shift 2;;
    --stack-prefix) prefix="${2:?--stack-prefix requires a value}"; shift 2;;
    --dashboard-name) dashboard_name="${2:?--dashboard-name requires a value}"; shift 2;;
    -h|--help) usage; exit 0;;
    *) echo "Error: unknown flag: $1" >&2; echo "Run with --help for usage." >&2; exit 2;;
  esac
done

# ----------------------------------------------------------------------------
# Pre-deployment validation
# ----------------------------------------------------------------------------
err()  { printf '\033[1;31m[ERROR]\033[0m %s\n' "$*" >&2; }
log()  { printf '\033[1;34m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
ok()   { printf '\033[1;32m[OK]\033[0m %s\n' "$*"; }

if [[ -n "$aws_profile" ]]; then
  export AWS_PROFILE="$aws_profile"
fi

if ! command -v aws >/dev/null 2>&1; then
  err "AWS CLI v2 is required but was not found in PATH."
  err "Install: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
  exit 1
fi

if ! [[ "$region" =~ ^[a-z]{2}-[a-z]+-[0-9]+$ ]]; then
  err "Invalid --region value: '$region' (expected format like 'us-west-2')."
  exit 1
fi

if ! [[ "$prefix" =~ ^[a-zA-Z][-a-zA-Z0-9]*$ ]]; then
  err "Invalid --stack-prefix '$prefix' (must match [a-zA-Z][-a-zA-Z0-9]*)."
  exit 1
fi

if ! aws sts get-caller-identity --region "$region" >/dev/null 2>&1; then
  err "AWS credentials are not configured or do not have access in region '$region'."
  err "Try one of:"
  err "  - export AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY"
  err "  - aws sso login --profile <your-profile>  (and pass --aws-profile <your-profile>)"
  err "  - aws configure"
  exit 1
fi

infra_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../infrastructure" && pwd)"
if [[ ! -f "$infra_dir/codex-otel-dashboard.yaml" ]]; then
  err "CloudFormation template not found: $infra_dir/codex-otel-dashboard.yaml"
  exit 1
fi

# ----------------------------------------------------------------------------
# Deploy the dashboard stack (one AWS::CloudWatch::Dashboard resource)
# ----------------------------------------------------------------------------
dash_stack="${prefix}-dashboard"

log "Deploying dashboard stack: $dash_stack (region $region)"
aws cloudformation deploy \
  --region "$region" \
  --stack-name "$dash_stack" \
  --template-file "$infra_dir/codex-otel-dashboard.yaml" \
  --parameter-overrides \
      DashboardName="$dashboard_name" \
      MetricsRegion="$region" \
  --no-fail-on-empty-changeset
ok "dashboard ready"

dashboard_url=$(aws cloudformation describe-stacks --region "$region" --stack-name "$dash_stack" \
  --query "Stacks[0].Outputs[?OutputKey=='DashboardURL'].OutputValue" --output text)

cat <<EOF

==========================================================================
Codex OTel dashboard deployed (native AWS access / local sidecar model).

Dashboard:  $dashboard_url

No ECS/ALB/VPC was created. Telemetry flows from each developer's local
collector directly to the CloudWatch native OTLP endpoint.

Next steps — set up the per-developer sidecar:

  1. Build the collector binaries:
       ./build-local-collector.sh --all

  2. Render deployment/templates/otel-local-config.yaml for each developer,
     substituting __AWS_REGION__ (=$region), __USER_EMAIL__, __USER_ID__,
     and optional org fields __DEPARTMENT__, __TEAM_ID__, __COST_CENTER__,
     __ORGANIZATION__, __LOCATION__, __ROLE__ (leave empty if not available).

  3. Add this block to each developer's ~/.codex/config.toml:

       [otel]
       environment = "production"

       [otel.metrics_exporter]
       otlp-http = { endpoint = "http://127.0.0.1:4318/v1/metrics", protocol = "binary" }

  4. Ensure their IAM role/permission set allows cloudwatch:PutMetricData.
  5. One-time per account: enable OTLP metric ingestion with
       aws cloudwatch start-otel-enrichment
       aws observabilityadmin start-telemetry-enrichment

See docs/QUICKSTART_NATIVE_AWS_ACCESS.md for the full walkthrough.

Teardown:
  aws cloudformation delete-stack --region $region --stack-name $dash_stack
==========================================================================
EOF
