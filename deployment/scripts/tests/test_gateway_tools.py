import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SCRIPTS_DIR.parents[1]


def load_script(module_name, filename):
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


contract = load_script("responses_contract", "validate-responses-contract.py")
preflight = load_script("litellm_preflight", "preflight-litellm.py")
provision_key = load_script("provision_litellm_key", "provision-litellm-key.py")
secret_auth = load_script("aws_secret_auth", "aws-secret-auth.py")
doc_links = load_script("doc_links", "validate-doc-links.py")


class TestResponsesContract(unittest.TestCase):
    def test_header_env_reads_secret_without_cli_value(self):
        headers = contract.parse_header_env(
            ["x-portkey-api-key=PORTKEY_API_KEY"],
            {"PORTKEY_API_KEY": "secret"},
        )
        self.assertEqual(headers, {"x-portkey-api-key": "secret"})

    def test_header_env_rejects_missing_variable(self):
        with self.assertRaisesRegex(ValueError, "is not set"):
            contract.parse_header_env(["x-key=MISSING"], {})

    def test_build_headers_requires_authorization(self):
        with self.assertRaisesRegex(ValueError, "Authorization"):
            contract.build_headers(None, {})

    def test_validate_continuation_requires_remembered_value(self):
        response = {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": "CODEX_GATEWAY_7F3A"}
                    ],
                }
            ]
        }
        contract.validate_continuation(response, "CODEX_GATEWAY_7F3A")
        with self.assertRaisesRegex(RuntimeError, "previous_response_id"):
            contract.validate_continuation(response, "different")

    def test_validate_stream_accepts_completed_responses_stream(self):
        body = (
            b"event: response.created\n"
            b'data: {"type":"response.created","response":{"id":"resp_1"}}\n\n'
            b"event: response.completed\n"
            b'data: {"type":"response.completed","response":'
            b'{"id":"resp_1","object":"response","status":"completed","output":[]}}\n\n'
        )
        contract.validate_stream("text/event-stream; charset=utf-8", body)

    def test_validate_stream_rejects_missing_completion(self):
        body = (
            b'data: {"type":"response.created"}\n\n'
            b'data: {"type":"response.output_text.delta","delta":"ok"}\n\n'
        )
        with self.assertRaisesRegex(RuntimeError, "response.completed"):
            contract.validate_stream("text/event-stream", body)


class TestLiteLLMPreflight(unittest.TestCase):
    def test_find_aws_cli_prefers_a_v2_candidate(self):
        def fake_run(command):
            version = "aws-cli/2.33.11" if command[0] == "/usr/local/bin/aws" else "aws-cli/1.44.28"
            return type(
                "Result",
                (),
                {"returncode": 0, "stdout": version, "stderr": ""},
            )()

        with (
            patch.object(preflight.shutil, "which", return_value="/opt/homebrew/bin/aws"),
            patch.object(preflight.os.path, "isfile", return_value=True),
            patch.object(preflight.os, "access", return_value=True),
            patch.object(preflight, "run", side_effect=fake_run),
        ):
            path, version = preflight.find_aws_cli({})
        self.assertEqual(path, "/usr/local/bin/aws")
        self.assertEqual(version, "aws-cli/2.33.11")

    def test_digest_and_ecr_parsing(self):
        digest = "a" * 64
        image = (
            "123456789012.dkr.ecr.us-east-1.amazonaws.com/"
            f"codex-litellm@sha256:{digest}"
        )
        self.assertTrue(preflight.validate_digest_reference(image))
        self.assertEqual(preflight.parse_ecr_reference(image)["region"], "us-east-1")

    def test_cidr_rejects_public_and_ipv6_networks(self):
        self.assertTrue(preflight.validate_cidr("203.0.113.4/32"))
        self.assertFalse(preflight.validate_cidr("0.0.0.0/0"))
        self.assertFalse(preflight.validate_cidr("2001:db8::/64"))

    def test_hostname_must_be_inside_hosted_zone(self):
        self.assertTrue(
            preflight.hostname_in_zone(
                "codex-litellm.example.com",
                "example.com.",
            )
        )
        self.assertFalse(
            preflight.hostname_in_zone(
                "codex-litellm.example.net",
                "example.com.",
            )
        )

    def test_environment_rejects_mutable_images(self):
        environ = {
            "AWS_REGION": "us-east-1",
            "BEDROCK_REGION": "us-east-1",
            "ALB_CERTIFICATE_ARN": (
                "arn:aws:acm:us-east-1:123456789012:certificate/example"
            ),
            "ALLOWED_CIDR": "203.0.113.4/32",
            "GATEWAY_DOMAIN_NAME": "gateway.example.com",
            "LITELLM_BASE_IMAGE": "ghcr.io/berriai/litellm:latest",
            "LITELLM_IMAGE": (
                "123456789012.dkr.ecr.us-east-1.amazonaws.com/codex-litellm:v1"
            ),
        }
        errors, _ = preflight.check_environment(environ, "deploy")
        self.assertEqual(len(errors), 2)

    def test_environment_accepts_deployment_values(self):
        digest = "b" * 64
        environ = {
            "AWS_REGION": "us-east-1",
            "BEDROCK_REGION": "us-east-1",
            "ALB_CERTIFICATE_ARN": (
                "arn:aws:acm:us-east-1:123456789012:certificate/example"
            ),
            "ALLOWED_CIDR": "203.0.113.4/32",
            "LITELLM_BASE_IMAGE": f"ghcr.io/berriai/litellm@sha256:{digest}",
            "LITELLM_IMAGE": (
                "123456789012.dkr.ecr.us-east-1.amazonaws.com/"
                f"codex-litellm@sha256:{digest}"
            ),
            "GATEWAY_DOMAIN_NAME": "gateway.example.com",
        }
        errors, warnings = preflight.check_environment(environ, "deploy")
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_environment_accepts_restricted_additional_cidrs(self):
        digest = "9" * 64
        environ = {
            "AWS_REGION": "us-east-1",
            "BEDROCK_REGION": "us-east-1",
            "ENABLE_TLS": "false",
            "ALLOWED_CIDR": "203.0.113.4/32",
            "ADDITIONAL_ALLOWED_CIDR_1": "198.51.100.8/32",
            "ADDITIONAL_ALLOWED_CIDR_2": "192.0.2.16/32",
            "LITELLM_BASE_IMAGE": f"ghcr.io/berriai/litellm@sha256:{digest}",
            "LITELLM_IMAGE": (
                "123456789012.dkr.ecr.us-east-1.amazonaws.com/"
                f"codex-litellm@sha256:{digest}"
            ),
        }
        errors, _ = preflight.check_environment(environ, "deploy")
        self.assertEqual(errors, [])

    def test_environment_rejects_public_additional_cidr(self):
        digest = "8" * 64
        environ = {
            "AWS_REGION": "us-east-1",
            "BEDROCK_REGION": "us-east-1",
            "ENABLE_TLS": "false",
            "ALLOWED_CIDR": "203.0.113.4/32",
            "ADDITIONAL_ALLOWED_CIDR_1": "0.0.0.0/0",
            "LITELLM_BASE_IMAGE": f"ghcr.io/berriai/litellm@sha256:{digest}",
            "LITELLM_IMAGE": (
                "123456789012.dkr.ecr.us-east-1.amazonaws.com/"
                f"codex-litellm@sha256:{digest}"
            ),
        }
        errors, _ = preflight.check_environment(environ, "deploy")
        self.assertIn(
            "ADDITIONAL_ALLOWED_CIDR_1 must be a restricted IPv4 network",
            errors,
        )

    def test_environment_accepts_managed_certificate_values(self):
        digest = "d" * 64
        environ = {
            "AWS_REGION": "us-east-1",
            "BEDROCK_REGION": "us-east-1",
            "ALB_CERTIFICATE_ARN": (
                "arn:aws:acm:us-east-1:123456789012:certificate/example"
            ),
            "ALLOWED_CIDR": "203.0.113.4/32",
            "LITELLM_BASE_IMAGE": f"ghcr.io/berriai/litellm@sha256:{digest}",
            "LITELLM_IMAGE": (
                "123456789012.dkr.ecr.us-east-1.amazonaws.com/"
                f"codex-litellm@sha256:{digest}"
            ),
            "GATEWAY_DOMAIN_NAME": "gateway.example.com",
            "ROUTE53_HOSTED_ZONE_ID": "Z0123456789EXAMPLE",
        }
        errors, warnings = preflight.check_environment(environ, "deploy")
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_environment_requires_a_tls_provisioning_path(self):
        digest = "e" * 64
        environ = {
            "AWS_REGION": "us-east-1",
            "BEDROCK_REGION": "us-east-1",
            "ALLOWED_CIDR": "203.0.113.4/32",
            "LITELLM_BASE_IMAGE": f"ghcr.io/berriai/litellm@sha256:{digest}",
            "LITELLM_IMAGE": (
                "123456789012.dkr.ecr.us-east-1.amazonaws.com/"
                f"codex-litellm@sha256:{digest}"
            ),
            "GATEWAY_DOMAIN_NAME": "gateway.example.com",
        }
        errors, _ = preflight.check_environment(environ, "deploy")
        self.assertIn(
            "Set ROUTE53_HOSTED_ZONE_ID for a managed certificate or "
            "ALB_CERTIFICATE_ARN for an existing certificate",
            errors,
        )

    def test_environment_accepts_cidr_restricted_http_walkthrough(self):
        digest = "f" * 64
        errors, warnings = preflight.check_environment(
            {
                "AWS_REGION": "us-east-1",
                "BEDROCK_REGION": "us-east-1",
                "ENABLE_TLS": "false",
                "ALLOWED_CIDR": "203.0.113.4/32",
                "LITELLM_BASE_IMAGE": (
                    f"ghcr.io/berriai/litellm@sha256:{digest}"
                ),
                "LITELLM_IMAGE": (
                    "123456789012.dkr.ecr.us-east-1.amazonaws.com/"
                    f"codex-litellm@sha256:{digest}"
                ),
            },
            "deploy",
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)

    def test_http_walkthrough_rejects_dns_and_certificate_values(self):
        digest = "1" * 64
        errors, _ = preflight.check_environment(
            {
                "AWS_REGION": "us-east-1",
                "BEDROCK_REGION": "us-east-1",
                "ENABLE_TLS": "false",
                "ALLOWED_CIDR": "203.0.113.4/32",
                "GATEWAY_DOMAIN_NAME": "gateway.example.com",
                "ROUTE53_HOSTED_ZONE_ID": "Z0123456789EXAMPLE",
                "LITELLM_BASE_IMAGE": (
                    f"ghcr.io/berriai/litellm@sha256:{digest}"
                ),
                "LITELLM_IMAGE": (
                    "123456789012.dkr.ecr.us-east-1.amazonaws.com/"
                    f"codex-litellm@sha256:{digest}"
                ),
            },
            "deploy",
        )
        self.assertIn(
            "GATEWAY_DOMAIN_NAME, ROUTE53_HOSTED_ZONE_ID, and "
            "ALB_CERTIFICATE_ARN must be blank when ENABLE_TLS=false",
            errors,
        )

    def test_build_stage_does_not_require_deployment_values(self):
        digest = "c" * 64
        errors, warnings = preflight.check_environment(
            {
                "AWS_REGION": "us-east-1",
                "LITELLM_BASE_IMAGE": f"ghcr.io/berriai/litellm@sha256:{digest}",
            },
            "build",
        )
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])


class TestLiteLLMCloudFormation(unittest.TestCase):
    def test_waf_allows_large_responses_payloads_only_on_codex_endpoint(self):
        template = (
            REPO_ROOT / "deployment/litellm/ecs/litellm-ecs.yaml"
        ).read_text(encoding="utf-8")

        self.assertIn("Name: BlockOversizeBodiesExceptResponses", template)
        self.assertIn("SearchString: /v1/responses", template)
        self.assertIn("SearchString: POST", template)
        self.assertIn("Name: SizeRestrictions_BODY", template)
        self.assertIn("ActionToUse:\n                    Count: {}", template)


class TestLiteLLMKeyProvisioning(unittest.TestCase):
    @staticmethod
    def args():
        return SimpleNamespace(
            admin_url="https://gateway.example.com",
            secret_id="gateway/codex-key",
            kms_key_id="arn:aws:kms:us-east-1:123456789012:key/example",
            aws_cli="/usr/local/bin/aws",
            region="us-east-1",
            key_alias="codex-walkthrough",
            models="gpt-5.5",
            user_id=None,
            team_id=None,
            max_budget=None,
            budget_duration=None,
            tpm_limit=None,
            rpm_limit=None,
        )

    def test_secret_is_passed_to_aws_over_stdin(self):
        result = type("Result", (), {"returncode": 0, "stdout": "{}", "stderr": ""})()
        with patch.object(provision_key, "run_aws", return_value=result) as run_aws:
            provision_key.store_key(
                aws_cli="/usr/local/bin/aws",
                region="us-east-1",
                secret_id="gateway/codex-key",
                kms_key_id="arn:aws:kms:us-east-1:123456789012:key/example",
                key="sk-secret-value",
            )
        arguments = run_aws.call_args.args[1]
        self.assertNotIn("sk-secret-value", arguments)
        self.assertIn("file:///dev/stdin", arguments)
        self.assertEqual(
            run_aws.call_args.kwargs["secret_input"],
            '{"LITELLM_API_KEY": "sk-secret-value"}',
        )

    def test_existing_secret_skips_key_generation(self):
        result = type("Result", (), {"returncode": 0, "stdout": "{}", "stderr": ""})()
        with patch.object(provision_key, "run_aws", return_value=result):
            self.assertTrue(
                provision_key.secret_exists(
                    aws_cli="/usr/local/bin/aws",
                    region="us-east-1",
                    secret_id="gateway/codex-key",
                )
            )

    def test_status_does_not_print_existing_secret_identifier(self):
        with (
            patch.object(
                provision_key.argparse.ArgumentParser,
                "parse_args",
                return_value=self.args(),
            ),
            patch.object(provision_key, "secret_exists", return_value=True),
            patch.object(provision_key, "print", create=True) as output,
            patch.dict(
                provision_key.os.environ,
                {"LITELLM_MASTER_KEY": "master-key"},
            ),
        ):
            self.assertEqual(provision_key.main(), 0)
        output.assert_called_once_with("Scoped LiteLLM key secret already exists.")

    def test_status_does_not_print_stored_secret_identifier(self):
        with (
            patch.object(
                provision_key.argparse.ArgumentParser,
                "parse_args",
                return_value=self.args(),
            ),
            patch.object(provision_key, "secret_exists", return_value=False),
            patch.object(provision_key, "generate_key", return_value="scoped-key"),
            patch.object(provision_key, "store_key"),
            patch.object(provision_key, "print", create=True) as output,
            patch.dict(
                provision_key.os.environ,
                {"LITELLM_MASTER_KEY": "master-key"},
            ),
        ):
            self.assertEqual(provision_key.main(), 0)
        output.assert_called_once_with("Stored scoped LiteLLM key in Secrets Manager.")


class TestAwsSecretAuth(unittest.TestCase):
    def test_resolves_json_field_without_putting_value_in_arguments(self):
        result = type(
            "Result",
            (),
            {
                "returncode": 0,
                "stdout": '{"LITELLM_API_KEY":"sk-secret-value"}\n',
                "stderr": "",
            },
        )()
        with patch.object(secret_auth.subprocess, "run", return_value=result) as run:
            value = secret_auth.resolve_secret_field(
                aws_cli="/usr/local/bin/aws",
                region="us-east-1",
                secret_id="gateway/codex-key",
                field="LITELLM_API_KEY",
                profile="codex-developer",
            )
        self.assertEqual(value, "sk-secret-value")
        self.assertNotIn("sk-secret-value", run.call_args.args[0])
        self.assertIn("codex-developer", run.call_args.args[0])

    def test_rejects_missing_or_non_string_field(self):
        result = type(
            "Result",
            (),
            {
                "returncode": 0,
                "stdout": '{"OTHER_KEY":"value"}\n',
                "stderr": "",
            },
        )()
        with patch.object(secret_auth.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "does not contain"):
                secret_auth.resolve_secret_field(
                    aws_cli="/usr/local/bin/aws",
                    region="us-east-1",
                    secret_id="gateway/codex-key",
                    field="LITELLM_API_KEY",
                )

    def test_print_token_writes_only_credential_protocol_response(self):
        args = SimpleNamespace(
            aws_cli="/usr/local/bin/aws",
            region="us-east-1",
            secret_id="gateway/codex-key",
            field="LITELLM_API_KEY",
            profile="codex-developer",
            action="print-token",
        )
        with (
            patch.object(secret_auth, "parse_args", return_value=args),
            patch.object(
                secret_auth,
                "resolve_secret_field",
                return_value="credential-value",
            ),
            patch.object(secret_auth.sys.stdout, "write") as output,
        ):
            self.assertEqual(secret_auth.main(), 0)
        output.assert_called_once_with("credential-value\n")


class TestDocumentationLinks(unittest.TestCase):
    def test_external_and_anchor_targets_are_ignored(self):
        self.assertIsNone(doc_links.local_target("https://example.com/path"))
        self.assertIsNone(doc_links.local_target("#section"))

    def test_local_target_removes_anchor_and_title(self):
        self.assertEqual(
            doc_links.local_target("guide.md#section \"Guide\""),
            "guide.md",
        )

    def test_absolute_target_within_repo_exists(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image = root / "docs" / "assets" / "image.jpg"
            image.parent.mkdir(parents=True)
            image.write_bytes(b"image")
            (root / "docs" / "draft.md").write_text(
                f"![Local image](<{image}>)\n",
                encoding="utf-8",
            )
            self.assertEqual(doc_links.missing_links(root), [])

    def test_ignored_browser_artifacts_are_not_scanned(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            artifact = root / "output" / "playwright" / "trace.md"
            artifact.parent.mkdir(parents=True)
            artifact.write_text("[missing](not-present.md)\n", encoding="utf-8")
            self.assertEqual(doc_links.missing_links(root), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
