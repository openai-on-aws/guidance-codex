import importlib.util
import os
import pathlib
import sys
import unittest
from unittest.mock import MagicMock, patch


def _load_wrapper():
    token_generator = MagicMock()
    token_generator.provide_token.return_value = "test-token"
    sys.modules.setdefault("aws_bedrock_token_generator", token_generator)

    wrapper_path = pathlib.Path(__file__).parents[2] / "run_litellm_with_bedrock_mantle_refresh.py"
    spec = importlib.util.spec_from_file_location("gateway_runtime", wrapper_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestDatabaseConfiguration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = _load_wrapper()

    def test_builds_url_from_secret_fields_and_escapes_credentials(self):
        env = {
            "DB_HOST": "database.internal",
            "DB_PORT": "5432",
            "DB_NAME": "litellm",
            "DB_USERNAME": "service@tenant",
            "DB_PASSWORD": "p@ss/word",
        }
        with patch.dict(os.environ, env, clear=True):
            self.runtime._configure_database_url()
            self.assertEqual(
                os.environ["DATABASE_URL"],
                "postgresql://service%40tenant:p%40ss%2Fword@database.internal:5432/litellm",
            )
            self.assertNotIn("DB_USERNAME", os.environ)
            self.assertNotIn("DB_PASSWORD", os.environ)

    def test_preserves_explicit_database_url(self):
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://managed"}, clear=True):
            self.runtime._configure_database_url()
            self.assertEqual(os.environ["DATABASE_URL"], "postgresql://managed")

    def test_fails_when_secret_fields_are_missing(self):
        with patch.dict(os.environ, {"DB_HOST": "database.internal"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "DB_USERNAME, DB_PASSWORD"):
                self.runtime._configure_database_url()


class TestInfrastructureContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        repo_root = pathlib.Path(__file__).parents[4]
        cls.template = (
            repo_root / "deployment/litellm/ecs/litellm-ecs.yaml"
        ).read_text(encoding="utf-8")
        cls.dockerfile = (
            repo_root / "deployment/litellm/Dockerfile"
        ).read_text(encoding="utf-8")

    def test_task_definition_does_not_render_database_password(self):
        self.assertNotIn("${DBPassword}", self.template)
        self.assertNotIn("Name: DATABASE_URL", self.template)
        self.assertIn("RDSInstance.MasterUserSecret.SecretArn", self.template)

    def test_gateway_image_requires_digest(self):
        self.assertIn("AllowedPattern: \".+@sha256:[a-f0-9]{64}\"", self.template)
        self.assertNotIn("main-latest", self.dockerfile)


if __name__ == "__main__":
    unittest.main(verbosity=2)
