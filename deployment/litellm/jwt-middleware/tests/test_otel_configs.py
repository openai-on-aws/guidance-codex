"""
Tests for org/identity attribute coverage in both OTEL collector configs.

Path 1 — native sidecar:   deployment/templates/otel-local-config.yaml
Path 2 — LiteLLM gateway:  deployment/litellm/otel-collector-config.yaml

The two paths model attribution differently on purpose:

  * Sidecar — identity/org are baked in at render time as RESOURCE attributes
    (static ``insert`` of ``__PLACEHOLDER__`` values), then a
    ``transform/identity_to_datapoint`` processor copies each onto the datapoint
    so the metric carries both forms. Emitting them as resource attributes is
    what lets a single ``@resource.``-prefixed dashboard work for this path AND
    the no-collector bearer-token path, where Codex sets the same keys via
    ``OTEL_RESOURCE_ATTRIBUTES`` (also resource attributes).

  * Gateway — LiteLLM stamps identity onto metric datapoints from a fixed
    allowlist, namespaced ``metadata.user_api_key_*`` (model on
    ``gen_ai.request.model``). The collector lifts those real keys to friendly
    names via ``from_attribute``/``upsert``. The richer org fields ride only on
    spans/logs, so they are intentionally NOT metric attributes here.
"""
import pathlib
import unittest
import yaml

REPO_ROOT = pathlib.Path(__file__).parents[4]

# Required identity, present on both paths.
IDENTITY_ATTRIBUTES = {"user.email", "user.id"}

# ---------------------------------------------------------------------------
# Path 1 — native sidecar: RESOURCE attributes, static insert placeholders.
# ---------------------------------------------------------------------------
SIDECAR_PLACEHOLDERS = {
    "user.email":   "__USER_EMAIL__",
    "user.id":      "__USER_ID__",
    "user.name":    "__USER_NAME__",
    "department":   "__DEPARTMENT__",
    "team.id":      "__TEAM_ID__",
    "cost_center":  "__COST_CENTER__",
    "organization": "__ORGANIZATION__",
    "location":     "__LOCATION__",
    "role":         "__ROLE__",
    "manager":      "__MANAGER__",
}

# Keys the transform processor must copy resource -> datapoint (everything the
# sidecar attributes by; service-level keys like source/collector.type are not
# copied because nothing queries them per-datapoint).
SIDECAR_DATAPOINT_KEYS = set(SIDECAR_PLACEHOLDERS)

# ---------------------------------------------------------------------------
# Path 2 — LiteLLM gateway: lift the real (namespaced) metric datapoint keys.
# ---------------------------------------------------------------------------
GATEWAY_FROM_ATTRIBUTE = {
    "user.email":   "metadata.user_api_key_user_email",
    "user.id":      "metadata.user_api_key_user_id",
    "model":        "gen_ai.request.model",
    "team.id":      "metadata.user_api_key_team_id",
    "organization": "metadata.user_api_key_org_id",
}

# Org fields that are span/log-only on the gateway path — must NOT appear as
# metric attributes (they are joined downstream via CUR / Athena instead).
GATEWAY_SPAN_ONLY_ABSENT = {
    "department", "cost_center", "location", "role", "manager", "user.name",
}

# awsemf single dimensions that bound CloudWatch cardinality.
GATEWAY_EXPECTED_DIMENSIONS = {
    "organization", "team.id", "model", "user.email", "user.id",
}


def _load_yaml(rel_path: str) -> dict:
    path = REPO_ROOT / rel_path
    with open(path) as f:
        return yaml.safe_load(f)


class TestSidecarConfig(unittest.TestCase):
    """Path 1: deployment/templates/otel-local-config.yaml (resource attrs)."""

    def setUp(self):
        self.cfg = _load_yaml("deployment/templates/otel-local-config.yaml")
        # Identity/org live under the RESOURCE processor on this path.
        attrs = self.cfg["processors"]["resource"]["attributes"]
        self.by_key = {a["key"]: a for a in attrs}

    def test_identity_attributes_present(self):
        for attr in IDENTITY_ATTRIBUTES:
            with self.subTest(attr=attr):
                self.assertIn(attr, self.by_key, f"Missing identity attribute: {attr}")

    def test_org_attribute_keys_present(self):
        for attr in SIDECAR_PLACEHOLDERS:
            with self.subTest(attr=attr):
                self.assertIn(attr, self.by_key, f"Missing attribute: {attr}")

    def test_team_id_present(self):
        self.assertIn("team.id", self.by_key)

    def test_placeholder_values(self):
        for key, placeholder in SIDECAR_PLACEHOLDERS.items():
            with self.subTest(key=key):
                entry = self.by_key.get(key)
                self.assertIsNotNone(entry, f"No entry for key '{key}'")
                self.assertEqual(
                    entry.get("value"), placeholder,
                    f"Key '{key}' should use placeholder '{placeholder}', got '{entry.get('value')}'"
                )

    def test_all_actions_are_insert(self):
        """Sidecar uses static insert (not upsert / from_attribute)."""
        for key in SIDECAR_PLACEHOLDERS:
            with self.subTest(key=key):
                self.assertEqual(self.by_key[key]["action"], "insert")

    def test_service_name_not_set(self):
        """Codex stamps service.name itself; the collector must not override it."""
        self.assertNotIn("service.name", self.by_key)

    def test_transform_copies_each_attribute_to_datapoint(self):
        """A transform processor must copy every identity/org resource attr onto
        the datapoint, each guarded on the resource attribute existing."""
        transform = self.cfg["processors"].get("transform/identity_to_datapoint")
        self.assertIsNotNone(transform, "transform/identity_to_datapoint processor missing")

        statements = []
        for block in transform["metric_statements"]:
            self.assertEqual(block["context"], "datapoint")
            statements.extend(block["statements"])
        joined = "\n".join(statements)

        for key in SIDECAR_DATAPOINT_KEYS:
            with self.subTest(key=key):
                expected_set = f'set(attributes["{key}"], resource.attributes["{key}"])'
                expected_guard = f'where resource.attributes["{key}"] != nil'
                self.assertIn(expected_set, joined, f"transform must copy '{key}' to datapoint")
                self.assertIn(expected_guard, joined, f"transform copy of '{key}' must be guarded on existence")

    def test_metrics_pipeline_order(self):
        """resource must run before the transform that reads its output."""
        processors = self.cfg["service"]["pipelines"]["metrics"]["processors"]
        self.assertEqual(processors, ["resource", "transform/identity_to_datapoint", "batch"])


class TestLiteLLMGatewayConfig(unittest.TestCase):
    """Path 2: deployment/litellm/otel-collector-config.yaml (lift datapoint attrs)."""

    def setUp(self):
        self.cfg = _load_yaml("deployment/litellm/otel-collector-config.yaml")
        actions = self.cfg["processors"]["attributes"]["actions"]
        self.by_key = {a["key"]: a for a in actions}

    def test_identity_attributes_present(self):
        for attr in IDENTITY_ATTRIBUTES:
            with self.subTest(attr=attr):
                self.assertIn(attr, self.by_key, f"Missing identity attribute: {attr}")

    def test_team_id_and_organization_present(self):
        self.assertIn("team.id", self.by_key)
        self.assertIn("organization", self.by_key)

    def test_model_present(self):
        self.assertIn("model", self.by_key)

    def test_from_attribute_sources_are_namespaced(self):
        """LiteLLM emits metric attrs under metadata.user_api_key_* /
        gen_ai.request.model — the flat names are silent no-ops."""
        for key, src in GATEWAY_FROM_ATTRIBUTE.items():
            with self.subTest(key=key):
                entry = self.by_key.get(key)
                self.assertIsNotNone(entry, f"No entry for key '{key}'")
                self.assertEqual(
                    entry.get("from_attribute"), src,
                    f"Key '{key}' should lift from '{src}', got '{entry.get('from_attribute')}'"
                )

    def test_all_actions_are_upsert(self):
        for key in GATEWAY_FROM_ATTRIBUTE:
            with self.subTest(key=key):
                self.assertEqual(self.by_key[key]["action"], "upsert")

    def test_span_only_fields_absent_from_metric_attributes(self):
        """Richer org fields are not on LiteLLM metric datapoints; they must not
        be declared as metric attributes here (joined downstream instead)."""
        for key in GATEWAY_SPAN_ONLY_ABSENT:
            with self.subTest(key=key):
                self.assertNotIn(key, self.by_key, f"'{key}' is span-only and must not be a gateway metric attribute")

    def test_awsemf_metric_declarations_bound_dimensions(self):
        """Each declared dimension set is a single attribute (cardinality bound)
        and the expected dimensions are covered."""
        decls = self.cfg["exporters"]["awsemf"]["metric_declarations"]
        declared = set()
        for d in decls:
            for dim_set in d["dimensions"]:
                # Every declaration pairs the attribute with OTelLib (or OTelLib alone).
                self.assertIn("OTelLib", dim_set, f"dimension set {dim_set} should include OTelLib")
                for dim in dim_set:
                    if dim != "OTelLib":
                        declared.add(dim)
        for dim in GATEWAY_EXPECTED_DIMENSIONS:
            with self.subTest(dimension=dim):
                self.assertIn(dim, declared, f"awsemf should declare dimension '{dim}'")

    def test_gateway_pipeline_includes_attributes_processor(self):
        metrics_pipeline = self.cfg["service"]["pipelines"]["metrics"]
        self.assertIn("attributes", metrics_pipeline["processors"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
