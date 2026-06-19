#!/bin/bash
# OTEL pipeline diagnostics — NATIVE AWS ACCESS path (local sidecar collector).
#
# This path has no ECS/ALB. Telemetry flows:
#   Codex CLI --OTLP--> local sidecar (127.0.0.1:4318) --OTLP+SigV4--> CloudWatch
#   native OTLP endpoint (monitoring.<region>.amazonaws.com). Metrics are stored
#   as CloudWatch Metrics and queried with PromQL.
#
# Run this ON THE DEVELOPER MACHINE that has the sidecar running.
#
# Usage: check-otel-pipeline.sh [region]
set -euo pipefail

REGION="${1:-us-west-2}"
COLLECTOR_HEALTH="http://127.0.0.1:13133/"
COLLECTOR_OTLP="http://127.0.0.1:4318"

echo "========================================="
echo "OTEL Pipeline Diagnostics (sidecar model)"
echo "Region: ${REGION}"
echo "========================================="
echo ""

echo "STEP 1: Local sidecar collector health"
echo "---------------------------------------"
if curl -fsS --max-time 5 "${COLLECTOR_HEALTH}" >/dev/null 2>&1; then
  echo "✓ Collector health check OK (${COLLECTOR_HEALTH})"
else
  echo "✗ Collector not responding on ${COLLECTOR_HEALTH}"
  echo "  Is the sidecar running? Start it with the rendered otel-local-config.yaml:"
  echo "    otelcol-local-<platform> --config otel-local-config.yaml"
fi
echo ""

echo "STEP 2: Local OTLP receiver reachable"
echo "--------------------------------------"
# OTLP/HTTP receiver returns 405/415 to a bare GET — any HTTP response means it's listening.
if curl -s --max-time 5 -o /dev/null -w '%{http_code}' "${COLLECTOR_OTLP}/v1/metrics" 2>/dev/null | grep -qE '^[0-9]{3}$'; then
  echo "✓ OTLP receiver is listening on ${COLLECTOR_OTLP}"
else
  echo "✗ Nothing listening on ${COLLECTOR_OTLP} — Codex cannot export metrics."
fi
echo ""

echo "STEP 3: AWS credentials + region"
echo "---------------------------------"
if aws sts get-caller-identity --region "${REGION}" >/dev/null 2>&1; then
  caller=$(aws sts get-caller-identity --region "${REGION}" --query Arn --output text)
  echo "✓ Credentials valid: ${caller}"
  echo "  (the sidecar uses these same credentials to SigV4-sign OTLP exports)"
else
  echo "✗ No valid AWS credentials for ${REGION}. Run: aws sso login"
fi
echo ""

echo "STEP 4: Verify metrics reached CloudWatch (PromQL query API)"
echo "------------------------------------------------------------"
# CloudWatch exposes a Prometheus-compatible query API at
#   https://monitoring.<region>.amazonaws.com/api/v1/query
# It requires SigV4-signed requests. `awscurl` signs automatically; if it is not
# installed we print the manual instruction rather than guessing an unsigned call.
PROM_QUERY='sum(histogram_sum({"codex.turn.token_usage", "@instrumentation.@name"="codex"}))'
PROM_URL="https://monitoring.${REGION}.amazonaws.com/api/v1/query"
if command -v awscurl >/dev/null 2>&1; then
  echo "Querying: ${PROM_QUERY}"
  awscurl --region "${REGION}" --service monitoring \
    "${PROM_URL}" --data-urlencode "query=${PROM_QUERY}" -X POST 2>/dev/null \
    | (command -v jq >/dev/null 2>&1 && jq '.data.result | {series: length}' || cat) \
    || echo "  (query returned no data yet — emit a Codex turn and retry in ~60s)"
else
  echo "awscurl not installed — skipping the signed query."
  echo "  Install (pip install awscurl) then run:"
  echo "    awscurl --region ${REGION} --service monitoring \\"
  echo "      '${PROM_URL}' --data-urlencode 'query=${PROM_QUERY}' -X POST"
  echo "  Or open the CloudWatch console → Metrics → Query with PromQL."
fi
echo ""

echo "========================================="
echo "Diagnostics complete"
echo "========================================="
echo ""
echo "If steps 1-3 pass but no metrics appear in step 4:"
echo "  - Confirm ~/.codex/config.toml [otel] endpoint = http://127.0.0.1:4318"
echo "  - Confirm the sidecar's sigv4auth region matches ${REGION}"
echo "  - Confirm the IAM role/permission set allows cloudwatch:PutMetricData"
echo "  - Allow ~60s after a Codex turn for metrics to surface"
