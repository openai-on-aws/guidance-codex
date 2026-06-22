# Operate — Troubleshooting

Known failure modes across the deploy paths. Not exhaustive — add entries as new failure modes surface.

Symptoms are grouped by path. The **All paths** section applies
regardless of how you deployed.

---

## All paths

### `bedrock:InvokeModel` returns `AccessDeniedException`
- **Likely cause:** the caller's role has the Bedrock customer-managed
  policy attached, but the session doesn't match the trust policy's
  `aws:PrincipalArn` condition (`AWSReservedSSO_CodexBedrockUser_*`).
- **Fix:** verify `aws sts get-caller-identity` returns an ARN matching the
  permission-set name baked into the stack. If you renamed the permission
  set, redeploy `bedrock-auth-idc.yaml` with the new
  `PermissionSetNamePattern` parameter.

### Model ID returns 404 / `ResourceNotFoundException`
- **Likely cause:** the model ID in `~/.codex/config.toml` (or gateway
  `litellm_config.yaml`) is not available in the target region, or it is
  only served through the mantle endpoint (GPT-5.4 / GPT-5.5) and you are calling
  standard Converse, or vice versa.
- **Fix:** verify the model ID against current AWS Bedrock docs and confirm it
  appears in `aws bedrock list-foundation-models --region <region>` as
  described in [reference-regions.md](reference-regions.md). `openai.gpt-oss-120b-1:0`
  and similar models use standard Converse; `gpt-5.4` / `gpt-5.5` use the mantle
  endpoint.

### CloudTrail `userIdentity.principalId` shows assumed-role ARN, not SSO username
- **Expected.** The SSO username is in `userIdentity.onBehalfOf` or is
  parsed from the assumed-role session name (`...:<sso-username>`). CUR
  2.0's `line_item_iam_principal` column contains the full assumed-role
  ARN; join on the session-name suffix for per-user attribution.

---

## IdC path

### `aws sso login` opens browser but Codex still fails with expired creds
- **Likely cause:** The AWS SDK cached credentials expired before Codex could
  use them, or the SSO session was established but credentials weren't refreshed.
- **Fix:** Re-run the Codex prompt. If it persists, verify that `~/.aws/sso/cache/`
  contains a fresh JSON blob with a future `expiresAt` timestamp. Run
  `aws sts get-caller-identity --profile codex` to verify the credential chain works.

### Using `aws login` (console-login) profiles instead of `aws sso login`
- **Requires:** Codex ≥ 0.130.0 (PR #21623 enabled the SDK's
  `credentials-login` feature so `login_session` profiles resolve for Bedrock
  SigV4 signing).
- **Fix for older Codex:** upgrade to ≥ 0.130.0, or use an `aws sso login`
  profile.

### Codex CLI works in terminal but fails in desktop app / VS Code
- **Likely cause:** GUI apps on macOS launch with a minimal PATH and may not
  inherit shell environment variables or AWS credential chain.
- **Fix:** Ensure the AWS CLI is in a standard location (`/usr/local/bin/aws`
  or Homebrew paths). For GUI apps, verify that `~/.aws/config` uses SSO
  profiles correctly. Run `aws sts get-caller-identity --profile codex` from
  the terminal first to confirm credentials work before launching the GUI.

---

## Gateway (LiteLLM) path

### `cloudformation delete-stack` on `codex-litellm-gateway` hangs on RDS
- **Cause:** `DeletionProtection: true` on the RDS instance — CloudFormation cannot
  delete it until protection is cleared.
- **Fix:**
  ```bash
  aws rds modify-db-instance \
    --db-instance-identifier <id> \
    --no-deletion-protection --apply-immediately
  ```
  Then retry the stack delete.

### Gateway `POST /v1/responses` from outside the VPC times out
- **Cause:** ALB `AllowedCidr` defaults to `10.0.0.0/8` — by design, the
  gateway is internal-only.
- **Fix:** either deploy a bastion or connect through the corporate VPN, or
  temporarily add a `/32` ingress rule to the ALB security group, then remove it afterward.

### LiteLLM returns `400` with `"LLM Provider NOT provided"`
- **Cause:** the `model` parameter in the request does not match an alias
  in `litellm_config.yaml`.
- **Fix:** use one of the aliases defined in the embedded config (e.g.
  `gpt-oss-120b`), or rebuild the image after editing the config.

---

## Observability (OTel → CloudWatch)

> Native AWS Access uses a **local sidecar collector** exporting to the
> CloudWatch native OTLP endpoint. Metrics are stored as CloudWatch Metrics and
> queried with PromQL. Run `deployment/scripts/check-otel-pipeline.sh <region>`
> on the developer machine.

### No metrics in CloudWatch after a Codex session
- **Check the `[otel.metrics_exporter.otlp-http]` block first.** `~/.codex/config.toml`
  must point the **metrics** exporter at the sidecar with the full `/v1/metrics`
  path (`endpoint = "http://127.0.0.1:4318/v1/metrics"`, Codex does not append it).
  A missing block means metrics default to the `statsig` exporter, not your collector.
- **Check the `analytics.enabled` metrics gate.** Codex gates **metrics** export
  (only metrics — logs/traces are unaffected) behind `analytics.enabled`. Both
  `codex exec` and the TUI default it to `true`, so metrics normally flow; but if a
  user, managed, or org config sets `[analytics] enabled = false`, the sidecar looks
  healthy while no metrics arrive. Confirm it is not disabled.
- **Checklist:**
  1. `[otel.metrics_exporter.otlp-http]` block present with the full `/v1/metrics`
     endpoint (see above).
  2. `[analytics] enabled` is not set to `false` anywhere in the config layers.
  3. A clean `codex exec` turn flushes on exit. If a run may exit via an error path
     (which can skip the on-exit flush), set `OTEL_METRIC_EXPORT_INTERVAL=1000` (ms)
     so a batch also flushes on a periodic interval as a safety net.
  4. Is the sidecar running? `curl http://127.0.0.1:13133/` (health) should
     return 200. If not, start `otelcol-local-<platform> --config otel-local-config.yaml`.
  5. Verify Codex version ≥ 0.130 — older versions emit different metric names.
  6. Confirm the IAM role/permission set allows `cloudwatch:PutMetricData`;
     without it the sidecar's SigV4 export is rejected (check collector stderr
     for 4xx/AccessDenied).
  7. Confirm the sidecar's `sigv4auth` region matches the dashboard region.
  8. Run `deployment/scripts/check-otel-pipeline.sh <region>` — it checks the
     exporter block, the analytics gate, sidecar health, credentials, and the PromQL query.

### Metrics land but `user.id` dimension is empty or shows incorrect value
- **Cause:** `__USER_ID__` / `__USER_EMAIL__` was not substituted in the
  rendered `otel-local-config.yaml`, or contains a placeholder.
- **Fix:** Re-render the sidecar config with the developer's real SSO identity:
  `aws sts get-caller-identity --profile codex --query Arn --output text`
  (the SSO username follows the final `/` in the assumed-role ARN), then
  restart the sidecar.

### Sidecar logs show `4xx` / `AccessDenied` on export
- **Cause:** Most commonly missing `cloudwatch:PutMetricData` on the developer's
  credentials, or a region mismatch between `sigv4auth` and the ingest region.
  Codex also emits logs/traces alongside metrics; the sidecar config routes
  those to the `debug` exporter so clients do not retry on 4xx.
