"""
Tests for org attribute coverage in both OTEL collector configs.

Path 1 — native sidecar:   deployment/templates/otel-local-config.yaml
Path 2 — LiteLLM gateway:  deployment/litellm/otel-collector-config.yaml
"""
import pathlib
import unittest
import yaml

REPO_ROOT = pathlib.Path(__file__).parents[4]

ORG_ATTRIBUTES = {
    "department",
    "cost_center",
    "organization",
    "location",
    "role",
    "manager",
}

IDENTITY_ATTRIBUTES = {
    "user.email",
    "user.id",
}

# Path 1 — native sidecar uses static insert placeholders
SIDECAR_PLACEHOLDERS = {
    "department":   "__DEPARTMENT__",
    "team.id":      "__TEAM_ID__",
    "cost_center":  "__COST_CENTER__",
    "organization": "__ORGANIZATION__",
    "location":     "__LOCATION__",
    "role":         "__ROLE__",
    "manager":      "__MANAGER__",
}


def _load_yaml(rel_path: str) -> dict:
    path = REPO_ROOT / rel_path
    with open(path) as f:
        return yaml.safe_load(f)


class TestSidecarConfig(unittest.TestCase):
    """Path 1: deployment/templates/otel-local-config.yaml"""

    def setUp(self):
        cfg = _load_yaml("deployment/templates/otel-local-config.yaml")
        actions = cfg["processors"]["attributes"]["actions"]
        self.by_key = {a["key"]: a for a in actions}

    def test_identity_attributes_present(self):
        for attr in IDENTITY_ATTRIBUTES:
            with self.subTest(attr=attr):
                self.assertIn(attr, self.by_key, f"Missing identity attribute: {attr}")

    def test_org_attribute_keys_present(self):
        for attr in ORG_ATTRIBUTES:
            with self.subTest(attr=attr):
                self.assertIn(attr, self.by_key, f"Missing org attribute: {attr}")

    def test_team_id_present(self):
        self.assertIn("team.id", self.by_key)

    def test_org_placeholder_values(self):
        for key, placeholder in SIDECAR_PLACEHOLDERS.items():
            with self.subTest(key=key):
                entry = self.by_key.get(key)
                self.assertIsNotNone(entry, f"No entry for key '{key}'")
                self.assertEqual(
                    entry.get("value"), placeholder,
                    f"Key '{key}' should use placeholder '{placeholder}', got '{entry.get('value')}'"
                )

    def test_all_actions_are_insert(self):
        """Sidecar uses static insert (not upsert from span attrs)."""
        for key in SIDECAR_PLACEHOLDERS:
            with self.subTest(key=key):
                entry = self.by_key[key]
                self.assertEqual(entry["action"], "insert")


class TestLiteLLMGatewayConfig(unittest.TestCase):
    """Path 2: deployment/litellm/otel-collector-config.yaml"""

    def setUp(self):
        cfg = _load_yaml("deployment/litellm/otel-collector-config.yaml")
        actions = cfg["processors"]["attributes"]["actions"]
        self.by_key = {a["key"]: a for a in actions}

    def test_identity_attributes_present(self):
        for attr in IDENTITY_ATTRIBUTES:
            with self.subTest(attr=attr):
                self.assertIn(attr, self.by_key, f"Missing identity attribute: {attr}")

    def test_org_attribute_keys_present(self):
        for attr in ORG_ATTRIBUTES:
            with self.subTest(attr=attr):
                self.assertIn(attr, self.by_key, f"Missing org attribute: {attr}")

    def test_manager_attribute_present(self):
        self.assertIn("manager", self.by_key)

    def test_team_id_present(self):
        self.assertIn("team.id", self.by_key)

    def test_org_attributes_use_from_attribute(self):
        """Gateway path lifts span attributes — each entry must use from_attribute."""
        expected_sources = {
            "department":   "department",
            "cost_center":  "cost_center",
            "organization": "organization",
            "location":     "location",
            "role":         "role",
            "manager":      "manager",
        }
        for key, src in expected_sources.items():
            with self.subTest(key=key):
                entry = self.by_key.get(key)
                self.assertIsNotNone(entry, f"No entry for key '{key}'")
                self.assertEqual(
                    entry.get("from_attribute"), src,
                    f"Key '{key}' should lift from span attribute '{src}'"
                )

    def test_all_org_actions_are_upsert(self):
        for key in ("department", "cost_center", "organization", "location", "role", "manager"):
            with self.subTest(key=key):
                self.assertEqual(self.by_key[key]["action"], "upsert")

    def test_gateway_pipeline_includes_attributes_processor(self):
        cfg = _load_yaml("deployment/litellm/otel-collector-config.yaml")
        metrics_pipeline = cfg["service"]["pipelines"]["metrics"]
        self.assertIn("attributes", metrics_pipeline["processors"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
