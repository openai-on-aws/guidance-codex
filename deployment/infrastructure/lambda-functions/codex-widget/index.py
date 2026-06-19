"""
Single parameterized CloudWatch custom-widget Lambda for Codex telemetry.
Each dashboard widget passes its own PromQL query + render type via widgetContext.
Queries the CloudWatch native OTLP Prometheus-compatible API (SigV4).
"""
import json, os, urllib.parse, urllib.request, time
import botocore.session, botocore.auth, botocore.awsrequest

REGION = os.environ.get("METRICS_REGION", "us-east-1")

DOC = """
## Codex Telemetry Widget
Renders a PromQL query against the CloudWatch native OTLP endpoint.
Widget params (in dashboard JSON `params`):
- `query` (str, required): PromQL query
- `render` (str): number | bar | pie | table | timeseries (default number)
- `unit` (str): optional suffix shown after a number (e.g. "ms")
- `title` (str): optional
"""

def _prom(query, minutes=180):
    sess = botocore.session.get_session()
    creds = sess.get_credentials().get_frozen_credentials()
    end = int(time.time()); start = end - minutes * 60
    url = f"https://monitoring.{REGION}.amazonaws.com/api/v1/query_range"
    body = urllib.parse.urlencode({"query": query, "start": start, "end": end, "step": 300})
    req = botocore.awsrequest.AWSRequest(method="POST", url=url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    botocore.auth.SigV4Auth(creds, "monitoring", REGION).add_auth(req)
    r = urllib.request.urlopen(urllib.request.Request(url, data=body.encode(),
        headers=dict(req.headers), method="POST"), timeout=20)
    return json.loads(r.read().decode()).get("data", {}).get("result", [])

def _label(series):
    m = {k: v for k, v in series.get("metric", {}).items() if not k.startswith("__") and not k.startswith("@")}
    for key in ("user.id", "user.email", "token_type", "model", "session_source", "success"):
        if key in m: return str(m[key])
    return next(iter(m.values()), "value") if m else "value"

def _last(series):
    vals = series.get("values") or []
    return float(vals[-1][1]) if vals else 0.0

def _sum_over(series):
    vals = series.get("values") or []
    return sum(float(v[1]) for v in vals)

def _fmt(n):
    n = float(n)
    if n >= 1_000_000: return f"{n/1_000_000:.2f}M"
    if n >= 1_000: return f"{n/1_000:.1f}K"
    if n == int(n): return str(int(n))
    return f"{n:.4g}"

# Distinct, color-blind-friendly palette (Tableau-10 style); readable on light & dark.
PALETTE = ["#4e79a7", "#f28e2b", "#59a14f", "#e15759", "#b07aa1",
           "#76b7b2", "#edc948", "#ff9da7", "#9c755f", "#bab0ac"]

def lambda_handler(event, context):
    if event.get("describe"):
        return DOC
    ctx = event.get("widgetContext", {})
    p = ctx.get("params", {}) or event.get("params", {})
    # CloudWatch passes the dashboard theme ("light"/"dark"); pick text colors that
    # stay legible on either. Default to a mid-tone that works on both if unknown.
    theme = (ctx.get("theme") or "light").lower()
    fg = "#e5e7eb" if theme == "dark" else "#0f172a"      # primary text
    muted = "#9ca3af" if theme == "dark" else "#64748b"   # secondary text
    query = p.get("query")
    render = p.get("render", "number")
    unit = p.get("unit", "")
    if not query:
        return "<p>No <code>query</code> param provided.</p>"
    try:
        res = _prom(query)
    except Exception as e:
        return f"<p style='color:#e15759'>Query error: {str(e)[:200]}</p>"
    if not res:
        return f"<p style='color:{muted}'>No data</p>"

    if render == "number":
        total = sum(_last(s) for s in res)
        return (f"<div style='text-align:center;padding:8px'>"
                f"<div style='font-size:42px;font-weight:700;color:{fg}'>{_fmt(total)}"
                f"<span style='font-size:18px;color:{muted}'> {unit}</span></div></div>")

    # rows for bar/pie/table: aggregate each series
    rows = sorted(((_label(s), _sum_over(s)) for s in res), key=lambda x: -x[1])

    if render == "bar":
        mx = max((v for _, v in rows), default=1) or 1
        bars = "".join(
            f"<tr><td style='padding:3px 8px;white-space:nowrap;color:{fg}'>{lbl}</td>"
            f"<td style='width:100%'><div style='background:{PALETTE[i % len(PALETTE)]};height:16px;border-radius:3px;width:{max(2, v/mx*100):.0f}%'></div></td>"
            f"<td style='padding:3px 8px;text-align:right;color:{fg};font-variant-numeric:tabular-nums'>{_fmt(v)}</td></tr>"
            for i, (lbl, v) in enumerate(rows))
        return f"<table style='width:100%;border-collapse:collapse;font:13px sans-serif'>{bars}</table>"

    if render == "pie":
        import math
        total = sum(v for _, v in rows) or 1
        cx = cy = 90; r = 80; a0 = -math.pi / 2
        segs = []; legend = []
        for i, (lbl, v) in enumerate(rows):
            frac = v / total
            a1 = a0 + frac * 2 * math.pi
            x0, y0 = cx + r * math.cos(a0), cy + r * math.sin(a0)
            x1, y1 = cx + r * math.cos(a1), cy + r * math.sin(a1)
            large = 1 if frac > 0.5 else 0
            color = PALETTE[i % len(PALETTE)]
            # full circle guard for a single 100% slice
            if frac >= 0.999:
                segs.append(f"<circle cx='{cx}' cy='{cy}' r='{r}' fill='{color}'/>")
            else:
                segs.append(f"<path d='M{cx},{cy} L{x0:.2f},{y0:.2f} A{r},{r} 0 {large} 1 {x1:.2f},{y1:.2f} Z' fill='{color}'/>")
            legend.append(f"<div style='display:flex;align-items:center;gap:6px;margin:2px 0;color:{fg}'>"
                          f"<span style='width:11px;height:11px;background:{color};border-radius:2px;display:inline-block'></span>"
                          f"<span style='font:12px sans-serif'>{lbl} — {_fmt(v)} ({frac*100:.0f}%)</span></div>")
            a0 = a1
        return (f"<div style='display:flex;align-items:center;gap:16px'>"
                f"<svg width='180' height='180' viewBox='0 0 180 180'>{''.join(segs)}</svg>"
                f"<div>{''.join(legend)}</div></div>")

    # table / default — ranked leaderboard
    trs = "".join(
        f"<tr><td style='padding:4px 8px;color:{muted};text-align:right'>{i+1}</td>"
        f"<td style='padding:4px 8px;color:{fg};white-space:nowrap'>{lbl}</td>"
        f"<td style='padding:4px 8px;text-align:right;color:{fg};font-variant-numeric:tabular-nums'>{_fmt(v)}</td></tr>"
        for i, (lbl, v) in enumerate(rows))
    return (f"<table style='width:100%;border-collapse:collapse;font:13px sans-serif'>"
            f"<thead><tr><th style='text-align:right;padding:4px 8px;color:{muted}'>#</th>"
            f"<th style='text-align:left;padding:4px 8px;color:{muted}'>{p.get('dim','Series')}</th>"
            f"<th style='text-align:right;padding:4px 8px;color:{muted}'>{p.get('unit') or 'Value'}</th></tr></thead>{trs}</table>")
