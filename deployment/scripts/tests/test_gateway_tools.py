import importlib.util
from pathlib import Path
import unittest


SCRIPTS_DIR = Path(__file__).resolve().parents[1]


def load_script(module_name, filename):
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


contract = load_script("responses_contract", "validate-responses-contract.py")
preflight = load_script("litellm_preflight", "preflight-litellm.py")
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
    def test_digest_and_ecr_parsing(self):
        digest = "a" * 64
        image = (
            "123456789012.dkr.ecr.us-east-1.amazonaws.com/"
            f"codex-litellm@sha256:{digest}"
        )
        self.assertTrue(preflight.validate_digest_reference(image))
        self.assertEqual(preflight.parse_ecr_reference(image)["region"], "us-east-1")

    def test_environment_rejects_mutable_images(self):
        environ = {
            "AWS_REGION": "us-east-1",
            "BEDROCK_REGION": "us-east-1",
            "ALB_CERTIFICATE_ARN": (
                "arn:aws:acm:us-east-1:123456789012:certificate/example"
            ),
            "ALLOWED_CIDR": "203.0.113.4/32",
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


class TestDocumentationLinks(unittest.TestCase):
    def test_external_and_anchor_targets_are_ignored(self):
        self.assertIsNone(doc_links.local_target("https://example.com/path"))
        self.assertIsNone(doc_links.local_target("#section"))

    def test_local_target_removes_anchor_and_title(self):
        self.assertEqual(
            doc_links.local_target("guide.md#section \"Guide\""),
            "guide.md",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
