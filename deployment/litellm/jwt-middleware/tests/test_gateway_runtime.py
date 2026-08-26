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

    wrapper_path = (
        pathlib.Path(__file__).parents[2]
        / "run_litellm_with_bedrock_token_refresh.py"
    )
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


class TestBedrockTokenRefresh(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = _load_wrapper()

    def test_refresh_updates_runtime_tokens(self):
        with (
            patch.dict(os.environ, {"AWS_REGION": "us-east-2"}, clear=True),
            patch.object(
                self.runtime,
                "provide_token",
                side_effect=["first-token", "second-token"],
            ),
        ):
            self.runtime._refresh_bedrock_token()
            self.assertEqual(os.environ["OPENAI_API_KEY"], "first-token")
            self.assertEqual(
                os.environ["AWS_BEARER_TOKEN_BEDROCK"],
                "first-token",
            )
            self.assertEqual(os.environ["BEDROCK_RUNTIME_REGION"], "us-east-2")

            self.runtime._refresh_bedrock_token()
            self.assertEqual(os.environ["OPENAI_API_KEY"], "second-token")
            self.assertEqual(
                os.environ["AWS_BEARER_TOKEN_BEDROCK"],
                "second-token",
            )


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
        cls.config = (
            repo_root / "deployment/litellm/litellm_config.yaml"
        ).read_text(encoding="utf-8")

    def test_task_definition_does_not_render_database_password(self):
        self.assertNotIn("${DBPassword}", self.template)
        self.assertNotIn("Name: DATABASE_URL", self.template)
        self.assertIn("RDSInstance.MasterUserSecret.SecretArn", self.template)

    def test_gateway_image_requires_digest(self):
        self.assertIn("AllowedPattern: \".+@sha256:[a-f0-9]{64}\"", self.template)
        self.assertNotIn("main-latest", self.dockerfile)

    def test_gateway_master_key_is_generated_by_default(self):
        self.assertIn("HasProvidedLiteLLMMasterKey", self.template)
        self.assertIn("GenerateStringKey: LITELLM_MASTER_KEY", self.template)
        self.assertIn("LiteLLMSecretArn:", self.template)

    def test_gateway_can_manage_tls_and_dns(self):
        self.assertIn("ManagedCertificate:", self.template)
        self.assertIn("GatewayAliasRecord:", self.template)
        self.assertIn("Route53HostedZoneId:", self.template)

    def test_gateway_has_explicit_ecs_rollout_safeguards(self):
        self.assertIn("PlatformVersion: LATEST", self.template)
        self.assertIn("mode: blocking", self.template)
        self.assertIn("deregistration_delay.timeout_seconds", self.template)

    def test_runtime_token_is_resolved_per_request(self):
        self.assertNotIn(
            "api_key: os.environ/AWS_BEARER_TOKEN_BEDROCK",
            self.config,
        )
        wrapper = (
            pathlib.Path(__file__).parents[2]
            / "run_litellm_with_bedrock_token_refresh.py"
        ).read_text(encoding="utf-8")
        self.assertIn('os.environ["OPENAI_API_KEY"] = token', wrapper)

    def test_gateway_config_is_runtime_only(self):
        self.assertNotIn("bedrock_mantle/", self.config)
        self.assertNotIn("model_name: gpt-5.4", self.config)
        self.assertNotIn("model_name: gpt-5.5", self.config)

    def test_runtime_iam_is_scoped_to_gpt_5_6_and_source_region(self):
        self.assertIn(
            "foundation-model/openai.gpt-5.6-*",
            self.template,
        )
        self.assertIn(
            "inference-profile/global.openai.gpt-5.6-*",
            self.template,
        )
        self.assertIn(
            "inference-profile/us.openai.gpt-5.6-*",
            self.template,
        )
        self.assertNotIn("application-inference-profile/*", self.template)
        invoke_statement = self.template.split(
            "- bedrock:InvokeModelWithResponseStream",
            maxsplit=1,
        )[1].split("- Effect: Allow", maxsplit=1)[0]
        self.assertIn("aws:RequestedRegion: !Ref AwsRegion", invoke_statement)


if __name__ == "__main__":
    unittest.main(verbosity=2)
