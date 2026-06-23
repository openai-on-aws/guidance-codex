# Operate — Monitoring & Cost Attribution

How to see who is using Codex, what they are spending, and when to alert.

Both deploy paths (IdC and Gateway) end in a user-scoped IAM session invoking
Bedrock. That single identity fans out into two authoritative signals —
**CloudTrail → CUR** for cost, **OTel → CloudWatch** for usage — plus an
optional cold-storage pipeline for deep historical queries.

## The identity chain

```
Corporate IdP (EntraID / Okta / …)
  └── SAML → IAM Identity Center
       └── aws sso login → temporary IAM credentials (assumed role)
            ├── bedrock:InvokeModel call
            │    └── CloudTrail userIdentity (SSO username)
            │         └── CUR 2.0 (line_item_iam_principal + cost-alloc tags)
            │              └── Per-user / per-team spend ← source of truth
            └── Codex OTel exporter (user.id/user.email, install-time baked)
                 └── Local OTel sidecar collector (per developer)
                      └── CloudWatch native OTLP endpoint (SigV4)
                           └── CloudWatch Metrics (user.id dimension, PromQL)
                                └── Per-user / per-team usage dashboard + quota alerts (soft; no cutoff)
```

Single identity, two dashboards. The gateway paths split this into two
planes (JWT for attribution, task role for Bedrock) — see the LLM Gateway
and AgentCore Gateway sections at the end.

This diagram is the canonical version. Other docs link here; do not copy.

---

## Three layers

| Layer | Purpose | Latency | Storage | Authority |
|---|---|---|---|---|
| **Live dashboards + quota alerts** | "What is happening now. Who is over budget." | ~60s | CloudWatch Metrics (native OTLP) | Usage only |
| **Cost attribution** | "What each user or team cost this month." | ~24h | CUR 2.0 on S3 | Billing-grade |
| **Deep historical analytics** (optional) | Arbitrary SQL over months of token-level data. | ~5min | S3 Parquet + Athena | Usage only |

Select the layers you need. Most organizations run **live + cost**; add the
analytics pipeline only when CUR cannot answer the question (per-turn token
counts, TPM/RPM spikes, session-duration studies).

---

## Layer 1 — Live dashboards + quota alerts (CloudWatch)

### What's deployed

The Native AWS Access path uses a **local sidecar collector** — no ECS, ALB,
or VPC. `deploy-otel-stack.sh` deploys only the dashboard:

- **Local OTel sidecar** (per developer, `otel-local-config.yaml` +
  `build-local-collector.sh`). Receives OTLP from Codex on `127.0.0.1:4318`,
  stamps the install-time-baked `user.id`/`user.email` resource attributes, and
  exports to the CloudWatch **native OTLP endpoint**
  (`monitoring.<region>.amazonaws.com`) with SigV4 auth from the developer's
  `aws sso login` credentials. Metrics land in CloudWatch Metrics, queryable
  via PromQL.
- **CloudWatch dashboard** `CodexOnBedrock` (`codex-otel-dashboard.yaml`) —
  scorecards, stacked line charts, ranked per-user leaderboards, and a
  session-source pie covering tokens, per-user attribution, API requests, and
  activity. Each widget is a native CloudWatch PromQL chart widget that queries
  the CloudWatch OTLP Prometheus-compatible API. The four Overview scorecards
  (Total Tokens, Active Users, Total Turns, API Requests) show rolling 24-hour
  totals: each wraps its metric in `sum_over_time(...[1d])` so the value is the
  sum across the last 24 hours. The line, bar, and pie widgets below plot the
  metric across the dashboard's selected time range.

OTLP metric ingestion is a one-time per-account enablement (`aws cloudwatch
start-otel-enrichment` + `aws observabilityadmin start-telemetry-enrichment`);
until both report `Running`, the sidecar's exports are accepted but not stored.

End users emit metrics once their generated `~/.codex/config.toml` ships with an
`[otel]` block pointing at the local sidecar
(`endpoint = "http://127.0.0.1:4318/v1/metrics"`) — see `deploy-identity-center.md`
§5. Note: Codex gates **metrics** export (not logs/traces) behind
`analytics.enabled`, which `codex exec` and the TUI default to `true` — metrics
flow by default and are dropped only if a config sets `[analytics] enabled = false`.
The developer's IAM role/permission set needs `cloudwatch:PutMetricData`.

### Metrics Codex actually emits

Metrics emitted by Codex ≥ 0.130:

- `codex.api_request`, `codex.api_request.duration_ms` — HTTP call count + latency; dimension `status`.
- `codex.turn.e2e_duration_ms` — wall-clock per turn.
- `codex.turn.token_usage` — dimension `token_type` ∈ {input, output, cached_input, reasoning_output, total}.
- `codex.turn.tool.call`, `codex.turn.memory`, `codex.turn.network_proxy`.
- `codex.thread.started`, `codex.thread.skills.*`.
- `codex.shell_snapshot(.duration_ms)`, `codex.startup_prewarm.*`, `codex.plugins.startup_sync(.final)`, `codex.conversation.turn.count`.

Codex automatically stamps resource attributes: `service.name` (e.g.
`codex_exec`), `service.version`,
`app.version`, `model`, `originator`, `session_source`, `os`. These become
additional CloudWatch dimensions.

### Spend

The live dashboard shows **token volume** (per user, per type, per model), not a
dollar figure — token-count × list-price estimates drift from real billing. The
ground truth for spend is **CUR (Layer 2)**, which carries per-user cost via
`line_item_iam_principal`. The dashboard shows token volume; CUR (Layer 2) is the ground truth for spend.

### Quota alerts

Under IdC there is **no credential issuer to gate**: IdC issues session
credentials directly, and you cannot revoke them mid-session on token overage.

Recommended posture under IdC:

- **Soft alerts via CloudWatch alarms** on `codex.turn.token_usage` summed by
  `user.id` over a rolling window. Route to SNS for email or Slack.
- **Hard enforcement, if required, via the Gateway path.** LiteLLM's
  per-user and per-key budget controls sit in the request path and can actively
  deny requests.

---

## Layer 2 — Cost attribution (CloudTrail → CUR 2.0)

**Cleanest under IdC.** The Gateway path sees only the gateway's task role
in CloudTrail (see gateway caveat below).

### Per-user: built-in, no IdP changes

Every `bedrock:InvokeModel` call carries the SSO username in
`userIdentity.principalId`, which shows up in CUR 2.0 under
`line_item_iam_principal` as:

```
arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_CodexBedrockUser_…/alice@example.com
```

Enable it:

1. Billing → **Data Exports** → create or edit a Standard CUR 2.0 export.
2. Under **Additional export content**, enable **"Include caller identity
   (IAM principal) allocation data"**.
3. Query via Athena:

```sql
SELECT line_item_iam_principal, SUM(line_item_unblended_cost) AS usd
FROM cur2
WHERE line_item_product_code = 'AmazonBedrock'
  AND year = '2026' AND month = '5'
GROUP BY 1 ORDER BY usd DESC;
```

Cost Explorer does **not** expose `line_item_iam_principal` as a
filter or grouping dimension — you need Athena or QuickSight. If you want per-user
visibility *in Cost Explorer*, add session tags (next section).

### Per-team: IAM principal tags

All IdC users share the same role, so role-level tags provide team or department
rollups only — not per-user.

1. Tag the `CodexBedrockUser` role (or its permission-set-backed role) with
   `department`, `cost-center`, etc.
2. Billing → **Cost Allocation Tags** → filter by **IAM principal type**,
   select, **Activate**.
3. Tags take up to 24 hours to appear after the first tagged call, then up to 24 hours
   more to activate.

### Session tags for per-user Cost Explorer visibility

Optional. Needed only if you want per-user grouping in Cost Explorer (not just
Athena). Embed an `https://aws.amazon.com/tags` claim in the IdP's ID token —
formats vary by IdP (Auth0 uses a nested object; Okta and Entra use flattened per-key).
See the [AWS STS session-tags guide](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_session-tags.html)
for claim formats per IdP — this is an IdP-side customization, not
Codex-specific.

The role's trust policy must allow `sts:TagSession` or
`AssumeRoleWithWebIdentity` fails outright.

---

## Layer 3 — Deep historical analytics (optional)

When CUR and live dashboards do not answer the question — typically token-level
studies, TPM/RPM spike analysis, or multi-month session correlation — a
historical analytics pipeline gives you arbitrary SQL over months of data:

- The local sidecar can dual-export — alongside the native OTLP metrics path, add
  an `awsemf` exporter that writes Embedded Metric Format to a CloudWatch Logs
  group dedicated to analytics.
- **Kinesis Firehose** streams that log group to S3 as Parquet, partitioned by
  `year/month/day/hour`.
- **Athena** with partition projection (no Glue crawler) queries the lake.
- **S3 lifecycle** transitions to Glacier after 90 days.

Cost: Firehose, S3, and Athena scans. For most organizations, CUR and live dashboards
suffice; enable this pipeline only when you have a specific query that requires it.
A CloudFormation template for this pipeline is not shipped in this repository —
build it when the need is concrete.

---

## LLM Gateway path caveat

If you route through an LLM Gateway, the identity chain splits:

- **CloudTrail** sees only the gateway's task role on every `InvokeModel`.
  `line_item_iam_principal` can no longer attribute cost to end users.
- **Cost attribution** moves into the gateway's own spend logs / metrics
  (e.g. LiteLLM's Postgres tables keyed by the JWT subject; Portkey's
  analytics; Kong's metrics plugin). Export to your BI layer for durable
  reporting.
- **Usage telemetry** is the gateway's responsibility on this path —
  configure the gateway's native OTel/Prometheus/spend callbacks, not the
  Codex-side OTel collector described in Layer 1.

This is the principal operational cost of choosing the gateway path. See
[QUICKSTART_LLM_GATEWAY.md](QUICKSTART_LLM_GATEWAY.md) for the LiteLLM
reference setup and its telemetry configuration.

---

## AgentCore Gateway path

The managed gateway has two telemetry sources, with different setup.

### Server-side metrics (no collector)

AWS records these; there is no sidecar to run and no SigV4 signing to arrange:

| Namespace | Emitted by | Carries | Setup |
|---|---|---|---|
| `AWS/BedrockMantle` | Bedrock **Mantle** (the GPT-5.x inference layer) | Token usage: `Inferences`, `InputTokens`, `OutputTokens`, … keyed by `Model`/`Project` | None — live as soon as traffic flows |
| `AWS/Bedrock-AgentCore` | AgentCore **Gateway** observability | Invocation/latency/error metrics: `Invocations`, `Throttles`, `SystemErrors`, `UserErrors`, `Latency`, `Duration` (dimensions `Operation`, `Protocol`, `Method`, `Resource`) | One-time **CloudWatch Transaction Search** enablement per account |

These namespaces do not include Codex's client OTEL: there is no per-user
`user.id` dimension and no per-turn / per-tool breakdown. All gateway traffic
shares one IAM role, so neither namespace attributes usage to an end user. See
the AgentCore quickstart's
[Verify telemetry](QUICKSTART_AGENTCORE_GATEWAY.md#verify-telemetry) section for
a live `get-metric-statistics` example.

### Client OTEL (the richer signals)

To also get the Layer 1 client metrics — `codex.turn.token_usage`,
`codex.turn.tool.call`, per-user `user.id` — enable Codex's `[otel]` block and run
the **per-developer local sidecar** exactly as on the
[Native path](#layer-1--live-dashboards--quota-alerts-cloudwatch). This is
independent of the gateway: Codex emits client OTEL the same way regardless of how
it reaches Bedrock. (Codex cannot SigV4-sign, so the sidecar signs — it needs the
developer's AWS credentials; a developer holding only an OIDC bearer has no
credentials for the sidecar to sign with.)

---

## Verification checklist

After deploying the OTel stack and generating developer configs:

1. **Metric lands in CloudWatch.** Run `deployment/scripts/check-otel-pipeline.sh
   <region>` on the developer machine; it checks the metrics-exporter config and
   sidecar health, then queries the CloudWatch PromQL API for
   `codex.turn.token_usage`. Metrics surface within ~60s of a Codex call.
2. **Dashboard renders per-user.** Open the `CodexOnBedrock` dashboard;
   "Estimated token spend (USD) per user" should show at least one
   user after a real session.
3. **CloudTrail logs the SSO username.** `aws cloudtrail lookup-events
   --lookup-attributes AttributeKey=EventName,AttributeValue=InvokeModel
   --region us-west-2` — `userIdentity.principalId` should contain the
   SSO email after the `/`.
4. **CUR principal column populates.** Up to 24h after the first invoke,
   `line_item_iam_principal` in your CUR 2.0 export should contain the
   same SSO email.

Steps 1–3 take seconds to minutes. Step 4 is the CUR latency — expect up to a day
before per-user spend appears in Athena.
