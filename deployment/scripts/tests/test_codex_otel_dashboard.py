import json
from pathlib import Path
import re
import unittest


REPO_ROOT = Path(__file__).resolve().parents[3]
DASHBOARD_TEMPLATE = (
    REPO_ROOT / "deployment" / "infrastructure" / "codex-otel-dashboard.yaml"
)
PIPELINE_CHECK = REPO_ROOT / "deployment" / "scripts" / "check-otel-pipeline.sh"


def load_dashboard_body():
    template = DASHBOARD_TEMPLATE.read_text(encoding="utf-8")
    marker = "      DashboardBody: !Sub |\n"
    _, body_and_outputs = template.split(marker, maxsplit=1)

    body_lines = []
    for line in body_and_outputs.splitlines():
        if line and not line.startswith("        "):
            break
        body_lines.append(line[8:] if line else line)

    body = "\n".join(body_lines).replace("${MetricsRegion}", "us-east-1")
    return json.loads(body)


def chart_queries(dashboard):
    queries = {}
    for widget in dashboard["widgets"]:
        properties = widget.get("properties", {})
        title = properties.get("title")
        if widget.get("type") != "chart" or not title:
            continue
        queries[title] = properties["data"]["queries"][0]["query"]
    return queries


class TestCodexOtelDashboard(unittest.TestCase):
    def test_total_and_attribution_queries_use_total_token_series(self):
        queries = chart_queries(load_dashboard_body())

        token_queries = {
            title: query
            for title, query in queries.items()
            if "codex.turn.token_usage" in query and title != "Tokens by type"
        }
        self.assertGreater(len(token_queries), 1)
        for title, query in token_queries.items():
            with self.subTest(title=title):
                self.assertIn('token_type="total"', query)
                self.assertNotIn('token_type!="total"', query)

    def test_by_type_chart_does_not_stack_overlapping_series(self):
        dashboard = load_dashboard_body()
        widget = next(
            widget
            for widget in dashboard["widgets"]
            if widget.get("properties", {}).get("title") == "Tokens by type"
        )

        query = widget["properties"]["data"]["queries"][0]["query"]
        line_options = widget["properties"]["plotOptions"]["style"]["lineOptions"]
        self.assertIn('token_type!="total"', query)
        self.assertIs(line_options["filled"], False)
        self.assertIs(line_options["stacked"], False)

    def test_pipeline_check_uses_total_token_series(self):
        script = PIPELINE_CHECK.read_text(encoding="utf-8")
        match = re.search(r"^PROM_QUERY='([^']+)'$", script, flags=re.MULTILINE)

        self.assertIsNotNone(match)
        self.assertIn('token_type="total"', match.group(1))


if __name__ == "__main__":
    unittest.main()
