import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
import urllib.error
from unittest.mock import MagicMock, patch


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

    def test_expected_model_requires_exact_upstream_id(self):
        contract.validate_expected_model(
            "@bedrock-mantle-validation/openai.gpt-5.5",
            "openai.gpt-5.5",
        )
        with self.assertRaisesRegex(RuntimeError, "must resolve"):
            contract.validate_expected_model(
                "@bedrock-mantle-validation/openai.gpt-5.4",
                "openai.gpt-5.5",
            )

    def test_validate_reasoning_requires_reasoning_item(self):
        contract.validate_reasoning({"output": [{"type": "reasoning"}]})
        with self.assertRaisesRegex(RuntimeError, "reasoning output item"):
            contract.validate_reasoning({"output": [{"type": "message"}]})

    def test_require_listed_model_requires_exact_provider_model(self):
        response = MagicMock()
        response.read.return_value = b'{"data":[{"id":"@mantle/openai.gpt-5.5"}]}'
        response.__enter__.return_value = response
        with patch.object(contract.urllib.request, "urlopen", return_value=response):
            contract.require_listed_model(
                "https://api.portkey.ai/v1",
                {"x-portkey-api-key": "secret"},
                "@mantle/openai.gpt-5.5",
                10,
            )

    def test_require_listed_model_rejects_missing_model(self):
        response = MagicMock()
        response.read.return_value = b'{"data":[{"id":"@mantle/openai.gpt-5.4"}]}'
        response.__enter__.return_value = response
        with (
            patch.object(contract.urllib.request, "urlopen", return_value=response),
            self.assertRaisesRegex(RuntimeError, "does not expose"),
        ):
            contract.require_listed_model(
                "https://api.portkey.ai/v1",
                {"x-portkey-api-key": "secret"},
                "@mantle/openai.gpt-5.5",
                10,
            )

    def test_require_listed_model_retries_catalog_sync_without_fallback(self):
        missing = MagicMock()
        missing.read.return_value = b'{"data":[{"id":"@mantle/openai.gpt-5.4"}]}'
        missing.__enter__.return_value = missing
        present = MagicMock()
        present.read.return_value = b'{"data":[{"id":"@mantle/openai.gpt-5.5"}]}'
        present.__enter__.return_value = present
        with (
            patch.object(
                contract.urllib.request,
                "urlopen",
                side_effect=[missing, missing, present],
            ) as urlopen,
            patch.object(contract.time, "sleep") as sleep,
        ):
            contract.require_listed_model(
                "https://api.portkey.ai/v1",
                {"x-portkey-api-key": "secret"},
                "@mantle/openai.gpt-5.5",
                10,
                attempts=3,
                delay_seconds=10,
            )

        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

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

    def test_validate_stream_rejects_malformed_event_json(self):
        with self.assertRaisesRegex(RuntimeError, "invalid JSON"):
            contract.validate_stream("text/event-stream", b"data: {broken}\n\n")

    def test_validate_tool_call_rejects_missing_forced_tool(self):
        with self.assertRaisesRegex(RuntimeError, "get_contract_value"):
            contract.validate_tool_call({"output": []}, "get_contract_value")

    def test_send_request_reports_authorization_failure(self):
        error = urllib.error.HTTPError(
            "https://gateway.example/v1/responses",
            401,
            "Unauthorized",
            {},
            io.BytesIO(b'{"error":"unauthorized"}'),
        )
        with (
            patch.object(contract.urllib.request, "urlopen", side_effect=error),
            self.assertRaisesRegex(RuntimeError, "HTTP 401"),
        ):
            contract.send_request(
                "https://gateway.example/v1",
                {"Authorization": "Bearer secret"},
                {"model": "test"},
                10,
            )


class TestPortkeyDeployment(unittest.TestCase):
    GATEWAY_IMAGE_DIGEST = "sha256:" + ("a" * 64)
    REDIS_IMAGE_DIGEST = "sha256:" + ("b" * 64)

    def portkey_environment(self):
        return {
            **os.environ,
            "PORTKEY_ENV_FILE": str(REPO_ROOT / "does-not-exist"),
            "AWS_REGION": "us-east-1",
            "BEDROCK_MANTLE_REGION": "us-east-1",
            "PORTKEY_BASE_URL": "https://portkey.internal.example/v1",
            "PORTKEY_GATEWAY_HOSTNAME": "portkey.internal.example",
            "PORTKEY_NLB_TLS_CERTIFICATE_ARN": (
                "arn:aws:acm:us-east-1:123456789012:certificate/"
                "11111111-2222-3333-4444-555555555555"
            ),
            "PORTKEY_NLB_ALLOWED_PREFIX_LIST_IDS": "pl-0123456789abcdef0",
            "PORTKEY_PROVIDER_SLUG": "bedrock-mantle-validation",
            "PORTKEY_ALLOWED_MODELS": "openai.gpt-5.5",
            "PORTKEY_MODEL": "@bedrock-mantle-validation/openai.gpt-5.5",
            "PORTKEY_API_KEY": "do-not-print-this-secret",
        }

    @staticmethod
    def certificate_arn(region="us-east-1", partition="aws"):
        return (
            f"arn:{partition}:acm:{region}:123456789012:certificate/"
            "11111111-2222-3333-4444-555555555555"
        )

    @staticmethod
    def gateway_role_arn():
        return "arn:aws:iam::123456789012:role/portkey-gateway-role"

    @staticmethod
    def gateway_policy_arn():
        return "arn:aws:iam::123456789012:policy/portkey"

    @classmethod
    def gateway_service_account_payload(cls, managed_by="eksctl"):
        return {
            "metadata": {
                "name": "gateway-sa",
                "namespace": "portkeyai",
                "labels": {"app.kubernetes.io/managed-by": managed_by},
                "annotations": {
                    "eks.amazonaws.com/role-arn": cls.gateway_role_arn()
                },
            }
        }

    @classmethod
    def gateway_iam_stack_payload(
        cls,
        *,
        application="guidance-codex-portkey",
        status="CREATE_COMPLETE",
        role_arn=None,
    ):
        tags = [
            {"Key": "alpha.eksctl.io/cluster-name", "Value": "codex-portkey"},
            {
                "Key": "alpha.eksctl.io/iamserviceaccount-name",
                "Value": "portkeyai/gateway-sa",
            },
        ]
        if application is not None:
            tags.append({"Key": "Application", "Value": application})
        return {
            "Stacks": [
                {
                    "StackName": (
                        "eksctl-codex-portkey-addon-iamserviceaccount-"
                        "portkeyai-gateway-sa"
                    ),
                    "StackStatus": status,
                    "Tags": tags,
                    "Outputs": [
                        {
                            "OutputKey": "Role1",
                            "OutputValue": role_arn or cls.gateway_role_arn(),
                        }
                    ],
                }
            ]
        }

    @classmethod
    def gateway_role_payload(cls, region="us-east-1"):
        issuer = f"oidc.eks.{region}.amazonaws.com/id/EXAMPLE"
        return {
            "Role": {
                "Arn": cls.gateway_role_arn(),
                "AssumeRolePolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": "sts:AssumeRoleWithWebIdentity",
                            "Principal": {
                                "Federated": (
                                    "arn:aws:iam::123456789012:oidc-provider/"
                                    f"{issuer}"
                                )
                            },
                            "Condition": {
                                "StringEquals": {
                                    f"{issuer}:aud": "sts.amazonaws.com",
                                    f"{issuer}:sub": (
                                        "system:serviceaccount:portkeyai:gateway-sa"
                                    ),
                                }
                            },
                        }
                    ],
                },
            }
        }

    @staticmethod
    def rendered_lbc_policy(
        region="us-east-1",
        partition="aws",
        account_id="123456789012",
        vpc_id="vpc-0123456789abcdef0",
        cluster_name="codex-portkey",
    ):
        policy_text = (
            REPO_ROOT / "deployment" / "portkey" / "lbc-iam-policy.json.tmpl"
        ).read_text(encoding="utf-8")
        replacements = {
            "__AWS_ACCOUNT_ID__": account_id,
            "__AWS_PARTITION__": partition,
            "__AWS_REGION__": region,
            "__VPC_ID__": vpc_id,
            "__CLUSTER_NAME__": cluster_name,
        }
        for placeholder, value in replacements.items():
            policy_text = policy_text.replace(placeholder, value)
        return json.loads(policy_text)

    @classmethod
    def legacy_lbc_policy(cls):
        legacy = json.loads(json.dumps(cls.rendered_lbc_policy()))
        statements = legacy["Statement"]
        by_sid = {statement["Sid"]: statement for statement in statements}
        by_sid["ReadNetworkLoadBalancerStateInDeploymentRegion"]["Action"].remove(
            "elasticloadbalancing:DescribeListenerCertificates"
        )
        create_listener = by_sid[
            "CreateTaggedTlsListenersOnControllerLoadBalancers"
        ]
        create_listener["Sid"] = (
            "CreateTaggedTcpListenersOnControllerLoadBalancers"
        )
        create_condition = create_listener["Condition"]
        create_condition.pop("ForAnyValue:StringEquals")
        create_condition["StringEquals"][
            "elasticloadbalancing:ListenerProtocol"
        ] = "TCP"
        statements.remove(
            by_sid["ModifyOnlyTlsListenersOnControllerLoadBalancers"]
        )
        lifecycle_actions = by_sid[
            "ManageControllerNetworkLoadBalancerListeners"
        ]["Action"]
        lifecycle_actions.remove(
            "elasticloadbalancing:AddListenerCertificates"
        )
        lifecycle_actions.remove(
            "elasticloadbalancing:RemoveListenerCertificates"
        )
        lifecycle_actions.append("elasticloadbalancing:ModifyListener")
        return legacy

    @staticmethod
    def lbc_stack_template(policy):
        issuer = "oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE"
        service_account = "kube-system/aws-load-balancer-controller"
        return {
            "TemplateBody": {
                "AWSTemplateFormatVersion": "2010-09-09",
                "Description": (
                    f'IAM role for serviceaccount "{service_account}" '
                    "[created and managed by eksctl]"
                ),
                "Resources": {
                    "Role1": {
                        "Type": "AWS::IAM::Role",
                        "Properties": {
                            "AssumeRolePolicyDocument": {
                                "Version": "2012-10-17",
                                "Statement": [
                                    {
                                        "Effect": "Allow",
                                        "Action": [
                                            "sts:AssumeRoleWithWebIdentity"
                                        ],
                                        "Principal": {
                                            "Federated": (
                                                "arn:aws:iam::123456789012:"
                                                f"oidc-provider/{issuer}"
                                            )
                                        },
                                        "Condition": {
                                            "StringEquals": {
                                                f"{issuer}:aud": (
                                                    "sts.amazonaws.com"
                                                ),
                                                f"{issuer}:sub": (
                                                    "system:serviceaccount:"
                                                    "kube-system:"
                                                    "aws-load-balancer-controller"
                                                ),
                                            }
                                        },
                                    }
                                ],
                            }
                        },
                    },
                    "Policy1": {
                        "Type": "AWS::IAM::Policy",
                        "Properties": {
                            "Roles": [{"Ref": "Role1"}],
                            "PolicyName": {
                                "Fn::Sub": "${AWS::StackName}-Policy1"
                            },
                            "PolicyDocument": policy,
                        },
                    },
                },
                "Outputs": {
                    "Role1": {"Value": {"Fn::GetAtt": "Role1.Arn"}}
                },
            }
        }

    def test_portkey_check_and_config_do_not_print_secret(self):
        script = SCRIPTS_DIR / "portkey-stack.sh"
        for command in ("check", "codex-config"):
            result = subprocess.run(
                ["bash", str(script), command],
                cwd=REPO_ROOT,
                env=self.portkey_environment(),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("do-not-print-this-secret", result.stdout + result.stderr)

    def test_portkey_codex_and_probes_use_the_same_api_key_headers(self):
        script_path = SCRIPTS_DIR / "portkey-stack.sh"
        result = subprocess.run(
            ["bash", str(script_path), "codex-config"],
            cwd=REPO_ROOT,
            env=self.portkey_environment(),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('env_key = "PORTKEY_API_KEY"', result.stdout)
        self.assertIn(
            'env_http_headers = { "x-portkey-api-key" = "PORTKEY_API_KEY" }',
            result.stdout,
        )

        script = script_path.read_text(encoding="utf-8")
        self.assertIn(
            '--header-env x-portkey-api-key=PORTKEY_API_KEY',
            script,
        )
        self.assertIn(
            'model_providers.portkey.env_http_headers=',
            script,
        )
        self.assertIn(
            "'x-portkey-api-key':'intentionally-invalid'",
            script,
        )
        self.assertIn('shell_environment_policy.inherit="core"', script)
        self.assertNotIn("set -a", script)

    def test_portkey_check_rejects_selected_model_outside_allowlist(self):
        environ = self.portkey_environment()
        environ["PORTKEY_MODEL"] = "@bedrock-mantle-validation/openai.gpt-5.4"
        result = subprocess.run(
            ["bash", str(SCRIPTS_DIR / "portkey-stack.sh"), "check"],
            cwd=REPO_ROOT,
            env=environ,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PORTKEY_ALLOWED_MODELS", result.stderr)
        self.assertNotIn("do-not-print-this-secret", result.stdout + result.stderr)

    def test_portkey_check_accepts_configurable_independent_regions_and_model(self):
        environ = self.portkey_environment()
        environ.update(
            {
                "AWS_REGION": "us-west-2",
                "BEDROCK_MANTLE_REGION": "us-east-2",
                "PORTKEY_NLB_TLS_CERTIFICATE_ARN": self.certificate_arn(
                    "us-west-2"
                ),
                "PORTKEY_ALLOWED_MODELS": (
                    "openai.gpt-5.5,openai.gpt-5.4"
                ),
                "PORTKEY_MODEL": (
                    "@bedrock-mantle-validation/openai.gpt-5.4"
                ),
            }
        )
        result = subprocess.run(
            ["bash", str(SCRIPTS_DIR / "portkey-stack.sh"), "check"],
            cwd=REPO_ROOT,
            env=environ,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("do-not-print-this-secret", result.stdout + result.stderr)

    def test_portkey_check_rejects_malformed_regions(self):
        for variable, value in (
            ("AWS_REGION", "us_east_1"),
            ("BEDROCK_MANTLE_REGION", "*"),
        ):
            with self.subTest(variable=variable):
                environ = self.portkey_environment()
                environ[variable] = value
                result = subprocess.run(
                    ["bash", str(SCRIPTS_DIR / "portkey-stack.sh"), "check"],
                    cwd=REPO_ROOT,
                    env=environ,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(variable, result.stderr)
                self.assertNotIn(
                    "do-not-print-this-secret",
                    result.stdout + result.stderr,
                )

    def test_portkey_check_rejects_regions_in_different_partitions(self):
        environ = self.portkey_environment()
        environ.update(
            {
                "AWS_REGION": "us-west-2",
                "BEDROCK_MANTLE_REGION": "us-gov-west-1",
            }
        )
        result = subprocess.run(
            ["bash", str(SCRIPTS_DIR / "portkey-stack.sh"), "check"],
            cwd=REPO_ROOT,
            env=environ,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("same AWS partition", result.stderr)
        self.assertNotIn("do-not-print-this-secret", result.stdout + result.stderr)

    def test_portkey_check_rejects_unsupported_mantle_region(self):
        environ = self.portkey_environment()
        environ["BEDROCK_MANTLE_REGION"] = "cn-north-1"
        result = subprocess.run(
            ["bash", str(SCRIPTS_DIR / "portkey-stack.sh"), "check"],
            cwd=REPO_ROOT,
            env=environ,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("AWS-documented Bedrock Mantle region", result.stderr)
        self.assertNotIn("do-not-print-this-secret", result.stdout + result.stderr)

    def test_portkey_check_rejects_explicitly_empty_model_allowlist(self):
        environ = self.portkey_environment()
        environ["PORTKEY_ALLOWED_MODELS"] = ""
        result = subprocess.run(
            ["bash", str(SCRIPTS_DIR / "portkey-stack.sh"), "check"],
            cwd=REPO_ROOT,
            env=environ,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PORTKEY_ALLOWED_MODELS", result.stderr)
        self.assertNotIn("do-not-print-this-secret", result.stdout + result.stderr)

    def test_portkey_check_rejects_model_allowlist_over_cloudformation_limit(self):
        models = [f"model{i:02d}.{'a' * 244}" for i in range(17)]
        oversized_allowlist = ",".join(models)
        self.assertGreater(len(oversized_allowlist), 4096)
        self.assertTrue(all(len(model) <= 256 for model in models))

        environ = self.portkey_environment()
        environ["PORTKEY_ALLOWED_MODELS"] = oversized_allowlist
        result = subprocess.run(
            ["bash", str(SCRIPTS_DIR / "portkey-stack.sh"), "check"],
            cwd=REPO_ROOT,
            env=environ,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("4096", result.stderr)
        self.assertNotIn("do-not-print-this-secret", result.stdout + result.stderr)

    def test_portkey_check_rejects_project_id_over_cloudformation_limit(self):
        oversized_project_id = "proj_" + ("a" * 252)
        self.assertGreater(len(oversized_project_id), 256)

        environ = self.portkey_environment()
        environ["BEDROCK_MANTLE_PROJECT_ID"] = oversized_project_id
        result = subprocess.run(
            ["bash", str(SCRIPTS_DIR / "portkey-stack.sh"), "check"],
            cwd=REPO_ROOT,
            env=environ,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("256", result.stderr)
        self.assertNotIn("do-not-print-this-secret", result.stdout + result.stderr)

    def test_portkey_check_rejects_unsafe_or_malformed_model_allowlists(self):
        rejected_allowlists = {
            "wildcard": "*",
            "duplicate": "openai.gpt-5.5,openai.gpt-5.5",
            "empty entry": "openai.gpt-5.5,",
            "provider-qualified entry": (
                "openai.gpt-5.5,@bedrock-mantle-validation/openai.gpt-5.4"
            ),
            "malformed entry": "openai.gpt-5.5,bad$model",
        }
        for description, allowed_models in rejected_allowlists.items():
            with self.subTest(description=description):
                environ = self.portkey_environment()
                environ["PORTKEY_ALLOWED_MODELS"] = allowed_models
                result = subprocess.run(
                    ["bash", str(SCRIPTS_DIR / "portkey-stack.sh"), "check"],
                    cwd=REPO_ROOT,
                    env=environ,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("PORTKEY_ALLOWED_MODELS", result.stderr)
                self.assertNotIn(
                    "do-not-print-this-secret",
                    result.stdout + result.stderr,
                )

    def test_portkey_cluster_plan_renders_configured_aws_region(self):
        script = SCRIPTS_DIR / "portkey-stack.sh"
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            rendered_cluster = temp / "rendered-cluster.yaml"

            eksctl = fake_bin / "eksctl"
            eksctl.write_text(
                """#!/usr/bin/env bash
if [[ "${1:-}" == version ]]; then printf '%s\n' "${PORTKEY_TEST_EKSCTL_VERSION:-0.229.0}"; exit 0; fi
previous=''
for argument in "$@"; do
  if [[ "$previous" == '--config-file' ]]; then
    cp "$argument" "$PORTKEY_TEST_RENDERED_CLUSTER"
  fi
  previous="$argument"
done
exit 0
""",
                encoding="utf-8",
            )
            eksctl.chmod(0o700)

            environ = self.portkey_environment()
            environ.update(
                {
                    "AWS_REGION": "us-west-2",
                    "BEDROCK_MANTLE_REGION": "us-east-2",
                    "PORTKEY_NLB_TLS_CERTIFICATE_ARN": self.certificate_arn(
                        "us-west-2"
                    ),
                    "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                    "PORTKEY_TEST_RENDERED_CLUSTER": str(rendered_cluster),
                }
            )
            result = subprocess.run(
                ["bash", str(script), "cluster-plan"],
                cwd=REPO_ROOT,
                env=environ,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            rendered = rendered_cluster.read_text(encoding="utf-8")
            self.assertIn("region: us-west-2", rendered)
            self.assertNotIn("region: us-east-1", rendered)

    def test_portkey_cluster_deploy_validates_version_and_tls_before_write(self):
        script = SCRIPTS_DIR / "portkey-stack.sh"
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            command_log = temp / "commands.log"

            eksctl = fake_bin / "eksctl"
            eksctl.write_text(
                """#!/usr/bin/env bash
if [[ "${1:-}" == version ]]; then printf '%s\n' "${PORTKEY_TEST_EKSCTL_VERSION:-0.229.0}"; exit 0; fi
printf 'eksctl %s\n' "$*" >>"$PORTKEY_TEST_COMMAND_LOG"
exit 0
""",
                encoding="utf-8",
            )
            eksctl.chmod(0o700)

            helm = fake_bin / "helm"
            helm.write_text(
                """#!/usr/bin/env bash
printf 'helm %s\n' "$*" >>"$PORTKEY_TEST_COMMAND_LOG"
if [[ "${1:-}" == version && "${2:-}" == --short ]]; then printf '%s\n' 'v3.21.4+gtest'; fi
exit 0
""",
                encoding="utf-8",
            )
            helm.chmod(0o700)

            aws = fake_bin / "aws"
            aws.write_text(
                """#!/usr/bin/env bash
printf 'aws %s\n' "$*" >>"$PORTKEY_TEST_COMMAND_LOG"
case "$*" in
  *"sts get-caller-identity"*"--query Account"*) printf '%s\n' '123456789012' ;;
  *"acm describe-certificate"*) printf '%s\n' '{"Status":"EXPIRED","DomainName":"portkey.internal.example","SubjectAlternativeNames":["portkey.internal.example"]}' ;;
esac
exit 0
""",
                encoding="utf-8",
            )
            aws.chmod(0o700)

            environment = {
                **self.portkey_environment(),
                "CONFIRM_AWS_WRITE": "1",
                "PORTKEY_NLB_TLS_AWS_VALIDATED": "true",
                "_PORTKEY_NLB_TLS_AWS_VALIDATED": "true",
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                "PORTKEY_TEST_COMMAND_LOG": str(command_log),
            }
            cases = (
                ("0.229.0", "certificate must be ISSUED"),
                ("0.230.0", "eksctl 0.229.0 exactly is required"),
                ("0.229.0-rc.1", "eksctl 0.229.0 exactly is required"),
            )
            for version, expected_error in cases:
                with self.subTest(eksctl_version=version):
                    command_log.write_text("", encoding="utf-8")
                    result = subprocess.run(
                        ["bash", str(script), "cluster-deploy"],
                        cwd=REPO_ROOT,
                        env={
                            **environment,
                            "PORTKEY_TEST_EKSCTL_VERSION": version,
                        },
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(expected_error, result.stderr)
                    commands = command_log.read_text(encoding="utf-8")
                    cluster_creates = [
                        line
                        for line in commands.splitlines()
                        if line.startswith("eksctl create cluster")
                    ]
                    self.assertEqual(len(cluster_creates), 1, commands)
                    self.assertIn("--dry-run", cluster_creates[0])
                    self.assertNotIn(
                        "eksctl utils associate-iam-oidc-provider", commands
                    )
                    self.assertNotIn("eksctl create iamserviceaccount", commands)
                    self.assertNotIn("eksctl update iamserviceaccount", commands)
                    self.assertNotIn("helm upgrade --install", commands)
                    self.assertNotIn("cloudformation deploy", commands)

    def test_portkey_deploy_propagates_regions_and_model_allowlist(self):
        script = SCRIPTS_DIR / "portkey-stack.sh"
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            command_log = temp / "commands.log"
            service_account_file = temp / "service-account.json"
            stack_file = temp / "gateway-iam-stack.json"
            role_file = temp / "gateway-role.json"
            service_account_file.write_text(
                json.dumps(self.gateway_service_account_payload()),
                encoding="utf-8",
            )
            stack_file.write_text(
                json.dumps(self.gateway_iam_stack_payload()),
                encoding="utf-8",
            )
            role_file.write_text(
                json.dumps(self.gateway_role_payload("us-west-2")),
                encoding="utf-8",
            )

            aws = fake_bin / "aws"
            aws.write_text(
                """#!/usr/bin/env bash
printf 'aws %s\n' "$*" >>"$PORTKEY_TEST_COMMAND_LOG"
case "$*" in
  *"sts get-caller-identity"*"--query Account"*) printf '%s\n' '123456789012' ;;
  *"sts get-caller-identity"*) printf '%s\n' '{}' ;;
  *"cluster.identity.oidc.issuer"*) printf '%s\n' 'https://oidc.eks.us-west-2.amazonaws.com/id/EXAMPLE' ;;
  *"cloudformation describe-stacks --stack-name eksctl-"*"StackName"*) printf '%s\n' 'eksctl-codex-portkey-addon-iamserviceaccount-portkeyai-gateway-sa' ;;
  *"cloudformation describe-stacks --stack-name eksctl-"*"--output json"*) cat "$PORTKEY_TEST_GATEWAY_IAM_STACK_JSON" ;;
  *"GatewayManagedPolicyArn"*) printf '%s\n' 'arn:aws:iam::123456789012:policy/portkey' ;;
  *"iam get-role"*) cat "$PORTKEY_TEST_GATEWAY_ROLE_JSON" ;;
  *"iam list-attached-role-policies"*) printf '%s\n' '["arn:aws:iam::123456789012:policy/portkey"]' ;;
  *"iam list-role-policies"*) printf '%s\n' '[]' ;;
esac
exit 0
""",
                encoding="utf-8",
            )
            aws.chmod(0o700)

            eksctl = fake_bin / "eksctl"
            eksctl.write_text(
                """#!/usr/bin/env bash
if [[ "${1:-}" == version ]]; then printf '%s\n' "${PORTKEY_TEST_EKSCTL_VERSION:-0.229.0}"; exit 0; fi
printf 'eksctl %s\n' "$*" >>"$PORTKEY_TEST_COMMAND_LOG"
exit 0
""",
                encoding="utf-8",
            )
            eksctl.chmod(0o700)

            kubectl = fake_bin / "kubectl"
            kubectl.write_text(
                """#!/usr/bin/env bash
case "$*" in
  *"get serviceaccount gateway-sa"*"--ignore-not-found -o name"*) printf '%s' 'serviceaccount/gateway-sa' ;;
  *"get serviceaccount gateway-sa"*"role-arn"*) printf '%s' 'arn:aws:iam::123456789012:role/portkey-gateway-role' ;;
  *"get serviceaccount gateway-sa -o json"*) cat "$PORTKEY_TEST_GATEWAY_SERVICE_ACCOUNT_JSON" ;;
esac
exit 0
""",
                encoding="utf-8",
            )
            kubectl.chmod(0o700)

            environ = self.portkey_environment()
            environ.update(
                {
                    "AWS_REGION": "us-west-2",
                    "BEDROCK_MANTLE_REGION": "us-east-2",
                    "PORTKEY_ALLOWED_MODELS": (
                        "openai.gpt-5.5,openai.gpt-5.4"
                    ),
                    "CONFIRM_AWS_WRITE": "1",
                    "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                    "PORTKEY_TEST_COMMAND_LOG": str(command_log),
                    "PORTKEY_TEST_GATEWAY_SERVICE_ACCOUNT_JSON": str(
                        service_account_file
                    ),
                    "PORTKEY_TEST_GATEWAY_IAM_STACK_JSON": str(stack_file),
                    "PORTKEY_TEST_GATEWAY_ROLE_JSON": str(role_file),
                }
            )
            result = subprocess.run(
                ["bash", str(script), "deploy"],
                cwd=REPO_ROOT,
                env=environ,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            commands = command_log.read_text(encoding="utf-8")
            self.assertIn("--region us-west-2 cloudformation deploy", commands)
            self.assertIn("BedrockMantleRegion=us-east-2", commands)
            self.assertIn(
                "MantleModelIds=openai.gpt-5.5,openai.gpt-5.4",
                commands,
            )
            self.assertIn(
                "eksctl utils associate-iam-oidc-provider "
                "--cluster codex-portkey --region us-west-2",
                commands,
            )

    def test_portkey_gateway_service_account_deploy_is_owned_and_fail_closed(self):
        script = SCRIPTS_DIR / "portkey-stack.sh"
        script_text = script.read_text(encoding="utf-8")
        self.assertNotIn("--override-existing-serviceaccounts", script_text)

        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            command_log = temp / "commands.log"
            created_marker = temp / "iam-service-account-created"
            service_account_file = temp / "service-account.json"
            stack_file = temp / "iam-stack.json"
            role_file = temp / "role.json"

            stack_file.write_text(
                json.dumps(self.gateway_iam_stack_payload()),
                encoding="utf-8",
            )
            role_file.write_text(
                json.dumps(self.gateway_role_payload()),
                encoding="utf-8",
            )

            aws = fake_bin / "aws"
            aws.write_text(
                r'''#!/usr/bin/env bash
printf 'aws %s\n' "$*" >>"$PORTKEY_TEST_COMMAND_LOG"
stack_present=false
case "${PORTKEY_TEST_GATEWAY_IAM_STATE:-fresh}" in
  managed|drift|stack-only) stack_present=true ;;
  fresh) [[ -f "$PORTKEY_TEST_CREATED_MARKER" ]] && stack_present=true ;;
esac
case "$*" in
  *"sts get-caller-identity"*"--query Account"*) printf '%s\n' '123456789012' ;;
  *"sts get-caller-identity"*) printf '%s\n' '{}' ;;
  *"cluster.identity.oidc.issuer"*) printf '%s\n' 'https://oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE' ;;
  *"cloudformation describe-stacks --stack-name eksctl-"*"StackName"*)
    if [[ "${PORTKEY_TEST_GATEWAY_IAM_STATE:-}" == unreadable ]]; then
      printf '%s\n' 'AccessDenied: test lookup denied' >&2
      exit 41
    fi
    if [[ "$stack_present" == true ]]; then
      printf '%s\n' 'eksctl-codex-portkey-addon-iamserviceaccount-portkeyai-gateway-sa'
    else
      printf '%s\n' 'An error occurred (ValidationError): Stack does not exist' >&2
      exit 255
    fi
    ;;
  *"cloudformation describe-stacks --stack-name eksctl-"*"--output json"*) cat "$PORTKEY_TEST_GATEWAY_IAM_STACK_JSON" ;;
  *"GatewayManagedPolicyArn"*) printf '%s\n' 'arn:aws:iam::123456789012:policy/portkey' ;;
  *"iam get-role"*) cat "$PORTKEY_TEST_GATEWAY_ROLE_JSON" ;;
  *"iam list-attached-role-policies"*) printf '%s\n' '["arn:aws:iam::123456789012:policy/portkey"]' ;;
  *"iam list-role-policies"*) printf '%s\n' '[]' ;;
  *"cloudformation validate-template"*) printf '%s\n' '{}' ;;
  *"cloudformation deploy"*) exit 0 ;;
esac
exit 0
''',
                encoding="utf-8",
            )
            aws.chmod(0o700)

            kubectl = fake_bin / "kubectl"
            kubectl.write_text(
                r'''#!/usr/bin/env bash
printf 'kubectl %s\n' "$*" >>"$PORTKEY_TEST_COMMAND_LOG"
service_account_present=false
case "${PORTKEY_TEST_GATEWAY_IAM_STATE:-fresh}" in
  managed|drift|sa-only) service_account_present=true ;;
  fresh) [[ -f "$PORTKEY_TEST_CREATED_MARKER" ]] && service_account_present=true ;;
  race) [[ -f "$PORTKEY_TEST_CREATED_MARKER" ]] && service_account_present=true ;;
esac
case "$*" in
  *"get serviceaccount gateway-sa"*"--ignore-not-found -o name"*)
    [[ "$service_account_present" == false ]] || printf '%s' 'serviceaccount/gateway-sa'
    ;;
  *"get serviceaccount gateway-sa"*"role-arn"*)
    [[ "$service_account_present" == true ]] || exit 1
    printf '%s' 'arn:aws:iam::123456789012:role/portkey-gateway-role'
    ;;
  *"get serviceaccount gateway-sa -o json"*)
    [[ "$service_account_present" == true ]] || exit 1
    cat "$PORTKEY_TEST_GATEWAY_SERVICE_ACCOUNT_JSON"
    ;;
esac
exit 0
''',
                encoding="utf-8",
            )
            kubectl.chmod(0o700)

            eksctl = fake_bin / "eksctl"
            eksctl.write_text(
                r'''#!/usr/bin/env bash
if [[ "${1:-}" == version ]]; then printf '%s\n' '0.229.0'; exit 0; fi
printf 'eksctl %s\n' "$*" >>"$PORTKEY_TEST_COMMAND_LOG"
if [[ "$*" == *"create iamserviceaccount"* ]]; then
  if [[ "${PORTKEY_TEST_GATEWAY_IAM_STATE:-}" == race ]]; then
    touch "$PORTKEY_TEST_CREATED_MARKER"
    printf '%s\n' 'eksctl silently excluded a service account that appeared after preflight'
    exit 0
  fi
  touch "$PORTKEY_TEST_CREATED_MARKER"
fi
exit 0
''',
                encoding="utf-8",
            )
            eksctl.chmod(0o700)

            base_environment = {
                **self.portkey_environment(),
                "CONFIRM_AWS_WRITE": "1",
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                "PORTKEY_TEST_COMMAND_LOG": str(command_log),
                "PORTKEY_TEST_CREATED_MARKER": str(created_marker),
                "PORTKEY_TEST_GATEWAY_SERVICE_ACCOUNT_JSON": str(
                    service_account_file
                ),
                "PORTKEY_TEST_GATEWAY_IAM_STACK_JSON": str(stack_file),
                "PORTKEY_TEST_GATEWAY_ROLE_JSON": str(role_file),
            }

            def run_case(state, service_account=None):
                command_log.write_text("", encoding="utf-8")
                created_marker.unlink(missing_ok=True)
                service_account_file.write_text(
                    json.dumps(
                        service_account or self.gateway_service_account_payload()
                    ),
                    encoding="utf-8",
                )
                result = subprocess.run(
                    ["bash", str(script), "deploy"],
                    cwd=REPO_ROOT,
                    env={
                        **base_environment,
                        "PORTKEY_TEST_GATEWAY_IAM_STATE": state,
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                )
                return result, command_log.read_text(encoding="utf-8")

            fresh, fresh_commands = run_case("fresh")
            self.assertEqual(fresh.returncode, 0, fresh.stderr)
            self.assertIn("cloudformation deploy", fresh_commands)
            self.assertIn("eksctl create iamserviceaccount", fresh_commands)
            self.assertIn(
                "--tags Application=guidance-codex-portkey",
                fresh_commands,
            )
            self.assertNotIn("--override-existing-serviceaccounts", fresh_commands)

            managed, managed_commands = run_case("managed")
            self.assertEqual(managed.returncode, 0, managed.stderr)
            self.assertIn("cloudformation deploy", managed_commands)
            self.assertNotIn("eksctl create iamserviceaccount", managed_commands)
            self.assertIn("Reusing the verified", managed.stdout)

            rejected_states = (
                (
                    "sa-only",
                    "already exists without its expected eksctl IAM stack",
                ),
                (
                    "stack-only",
                    "exists without its expected Kubernetes service account",
                ),
                (
                    "unreadable",
                    "could not determine whether the gateway IAM service-account stack exists",
                ),
            )
            for state, expected_error in rejected_states:
                with self.subTest(state=state):
                    rejected, commands = run_case(state)
                    self.assertNotEqual(rejected.returncode, 0)
                    self.assertIn(expected_error, rejected.stderr)
                    self.assertNotIn("cloudformation deploy", commands)
                    self.assertNotIn(
                        "eksctl utils associate-iam-oidc-provider", commands
                    )
                    self.assertNotIn("eksctl create iamserviceaccount", commands)

            drifted, drifted_commands = run_case(
                "drift",
                self.gateway_service_account_payload(managed_by="platform-team"),
            )
            self.assertNotEqual(drifted.returncode, 0)
            self.assertIn(
                "gateway IAM service-account ownership validation failed",
                drifted.stderr,
            )
            self.assertNotIn("cloudformation deploy", drifted_commands)
            self.assertNotIn("eksctl create iamserviceaccount", drifted_commands)

            raced, race_commands = run_case("race")
            self.assertNotEqual(raced.returncode, 0)
            self.assertIn(
                "exists without its expected eksctl IAM stack",
                raced.stderr,
            )
            self.assertIn("eksctl create iamserviceaccount", race_commands)
            self.assertNotIn("--override-existing-serviceaccounts", race_commands)

            for result in (fresh, managed, drifted, raced):
                self.assertNotIn(
                    "do-not-print-this-secret",
                    result.stdout + result.stderr,
                )

    def test_portkey_strict_validation_probes_every_allowed_model(self):
        script = SCRIPTS_DIR / "portkey-stack.sh"
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            probe_log = temp / "probes.log"
            real_python = shutil.which("python3") or sys.executable

            python = fake_bin / "python3"
            python.write_text(
                """#!/usr/bin/env bash
if [[ "${1:-}" == *validate-responses-contract.py ]]; then
  printf '%s|%s\n' "$GATEWAY_MODEL" "$*" >>"$PORTKEY_TEST_PROBE_LOG"
  exit 0
fi
exec "$PORTKEY_TEST_REAL_PYTHON" "$@"
""",
                encoding="utf-8",
            )
            python.chmod(0o700)

            environ = self.portkey_environment()
            environ.update(
                {
                    "AWS_REGION": "us-west-2",
                    "BEDROCK_MANTLE_REGION": "us-east-2",
                    "PORTKEY_ALLOWED_MODELS": (
                        "openai.gpt-5.5,openai.gpt-5.4"
                    ),
                    "PORTKEY_NLB_TLS_CERTIFICATE_ARN": self.certificate_arn(
                        "us-west-2"
                    ),
                    "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                    "PORTKEY_TEST_PROBE_LOG": str(probe_log),
                    "PORTKEY_TEST_REAL_PYTHON": real_python,
                }
            )
            result = subprocess.run(
                ["bash", str(script), "validate"],
                cwd=REPO_ROOT,
                env=environ,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            probes = probe_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(probes), 2, probes)
            expected = [
                (
                    "@bedrock-mantle-validation/openai.gpt-5.5",
                    "openai.gpt-5.5",
                ),
                (
                    "@bedrock-mantle-validation/openai.gpt-5.4",
                    "openai.gpt-5.4",
                ),
            ]
            for line, (qualified_model, upstream_model) in zip(probes, expected):
                model, arguments = line.split("|", 1)
                self.assertEqual(model, qualified_model)
                self.assertIn(f"--expected-model {upstream_model}", arguments)
                self.assertIn("--require-model-listed", arguments)
                self.assertIn("--require-reasoning", arguments)
                self.assertIn("--include-tool-call", arguments)

    def test_portkey_strict_validation_fails_when_second_model_probe_fails(self):
        script = SCRIPTS_DIR / "portkey-stack.sh"
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            probe_log = temp / "probes.log"
            real_python = shutil.which("python3") or sys.executable

            python = fake_bin / "python3"
            python.write_text(
                """#!/usr/bin/env bash
if [[ "${1:-}" == *validate-responses-contract.py ]]; then
  printf '%s\n' "$GATEWAY_MODEL" >>"$PORTKEY_TEST_PROBE_LOG"
  if [[ "$GATEWAY_MODEL" == *@*/openai.gpt-5.4 ]]; then
    printf '%s\n' 'forced second-model contract failure' >&2
    exit 23
  fi
  exit 0
fi
exec "$PORTKEY_TEST_REAL_PYTHON" "$@"
""",
                encoding="utf-8",
            )
            python.chmod(0o700)

            environ = self.portkey_environment()
            environ.update(
                {
                    "PORTKEY_ALLOWED_MODELS": (
                        "openai.gpt-5.5,openai.gpt-5.4"
                    ),
                    "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                    "PORTKEY_TEST_PROBE_LOG": str(probe_log),
                    "PORTKEY_TEST_REAL_PYTHON": real_python,
                }
            )
            result = subprocess.run(
                ["bash", str(script), "validate"],
                cwd=REPO_ROOT,
                env=environ,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 23)
            self.assertEqual(
                probe_log.read_text(encoding="utf-8").splitlines(),
                [
                    "@bedrock-mantle-validation/openai.gpt-5.5",
                    "@bedrock-mantle-validation/openai.gpt-5.4",
                ],
            )
            self.assertIn("forced second-model contract failure", result.stderr)
            self.assertNotRegex(
                result.stdout.lower(),
                r"(all allowed models|strict validation (passed|succeeded|complete))",
            )

    def test_portkey_irsa_role_scopes_service_account_and_mantle(self):
        template = (
            REPO_ROOT / "deployment" / "portkey" / "hybrid-infrastructure.yaml"
        ).read_text(encoding="utf-8")
        self.assertIn("AWS::IAM::ManagedPolicy", template)
        script = (SCRIPTS_DIR / "portkey-stack.sh").read_text(encoding="utf-8")
        self.assertIn("eksctl create iamserviceaccount", script)
        self.assertIn("BedrockMantleRegion:", template)
        self.assertIn("MantleModelIds:", template)
        self.assertRegex(
            template,
            r"(?ms)MantleProjectId:.*?MaxLength: 256",
        )
        self.assertRegex(
            template,
            r"(?ms)MantleModelIds:\s+Type: String",
        )
        self.assertIn(
            "bedrock-mantle:Model: !Split [',', !Ref MantleModelIds]",
            template,
        )
        self.assertIn("${BedrockMantleRegion}", template)
        self.assertNotIn("bedrock-mantle:${AWS::Region}", template)
        self.assertIn("bedrock-mantle:CreateInference", template)
        self.assertNotIn("bedrock:InvokeModel", template)
        self.assertNotIn("AllowedValues: [openai.gpt-5.5]", template)

    def test_portkey_supported_mantle_regions_match_cloudformation(self):
        script = (SCRIPTS_DIR / "portkey-stack.sh").read_text(encoding="utf-8")
        template = (
            REPO_ROOT / "deployment" / "portkey" / "hybrid-infrastructure.yaml"
        ).read_text(encoding="utf-8")
        script_match = re.search(
            r"(?ms)^SUPPORTED_BEDROCK_MANTLE_REGIONS=\(\n(.*?)^\)",
            script,
        )
        template_match = re.search(
            r"(?ms)^  BedrockMantleRegion:\n.*?^    AllowedValues:\n"
            r"(.*?)^    ConstraintDescription:",
            template,
        )
        self.assertIsNotNone(script_match)
        self.assertIsNotNone(template_match)
        script_regions = set(script_match.group(1).split())
        template_regions = set(
            re.findall(r"(?m)^      - ([a-z0-9-]+)$", template_match.group(1))
        )
        self.assertEqual(script_regions, template_regions)
        self.assertIn("us-east-1", script_regions)
        self.assertNotIn("cn-north-1", script_regions)

    def test_portkey_hybrid_values_do_not_contain_committed_secrets(self):
        values = (
            REPO_ROOT / "deployment" / "portkey" / "values.yaml.tmpl"
        ).read_text(encoding="utf-8")
        self.assertIn("__PORTKEY_CLIENT_AUTH__", values)
        self.assertIn("__PORTKEY_DOCKER_PASSWORD__", values)
        self.assertNotIn("api.portkey.ai/v1", values)

    def test_portkey_image_inputs_require_digest_pins_and_reject_aliases_safely(self):
        script = SCRIPTS_DIR / "portkey-stack.sh"
        shell_command = (
            'set -- help; source "$PORTKEY_TEST_SCRIPT" >/dev/null; '
            "validate_helm_secrets"
        )
        canary = "do-not-print-image-input-canary"
        environment = {
            **os.environ,
            "PORTKEY_ENV_FILE": str(REPO_ROOT / "does-not-exist"),
            "PORTKEY_TEST_SCRIPT": str(script),
            "PORTKEY_DOCKER_USERNAME": "registry-user",
            "PORTKEY_DOCKER_PASSWORD": "registry-password",
            "PORTKEY_CLIENT_AUTH": "client-auth",
            "PORTKEY_ORGANIZATION_ID": "organization-id",
            "PORTKEY_HELM_CHART_VERSION": "1.7.7",
            "PORTKEY_GATEWAY_IMAGE_TAG": "2026.08.03",
            "PORTKEY_GATEWAY_IMAGE_DIGEST": self.GATEWAY_IMAGE_DIGEST,
            "PORTKEY_REDIS_IMAGE_TAG": "7.2.10-alpine",
            "PORTKEY_REDIS_IMAGE_DIGEST": self.REDIS_IMAGE_DIGEST,
        }

        accepted = subprocess.run(
            ["bash", "-c", shell_command],
            cwd=REPO_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)

        for digest_name in (
            "PORTKEY_GATEWAY_IMAGE_DIGEST",
            "PORTKEY_REDIS_IMAGE_DIGEST",
        ):
            with self.subTest(missing=digest_name):
                missing_environment = dict(environment)
                missing_environment.pop(digest_name)
                rejected = subprocess.run(
                    ["bash", "-c", shell_command],
                    cwd=REPO_ROOT,
                    env=missing_environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(rejected.returncode, 0)
                self.assertIn(digest_name, rejected.stderr)

        malformed_digests = (
            "sha256:" + ("a" * 63),
            "sha512:" + ("a" * 64),
            "sha256:" + ("G" * 64),
            f"sha256:{canary}",
        )
        for digest_name in (
            "PORTKEY_GATEWAY_IMAGE_DIGEST",
            "PORTKEY_REDIS_IMAGE_DIGEST",
        ):
            for malformed in malformed_digests:
                with self.subTest(variable=digest_name, malformed=malformed[:7]):
                    rejected = subprocess.run(
                        ["bash", "-c", shell_command],
                        cwd=REPO_ROOT,
                        env={**environment, digest_name: malformed},
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertNotEqual(rejected.returncode, 0)
                    self.assertIn(digest_name, rejected.stderr)
                    self.assertNotIn(canary, rejected.stdout + rejected.stderr)

        for alias in ("latest", "LATEST", "edge", "main-latest"):
            with self.subTest(alias=alias):
                rejected = subprocess.run(
                    ["bash", "-c", shell_command],
                    cwd=REPO_ROOT,
                    env={**environment, "PORTKEY_GATEWAY_IMAGE_TAG": alias},
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(rejected.returncode, 0)
                self.assertIn("PORTKEY_GATEWAY_IMAGE_TAG", rejected.stderr)
                self.assertNotIn(canary, rejected.stdout + rejected.stderr)

    def test_portkey_helm_plan_accepts_helm3_and_rejects_helm4_before_render(self):
        script = SCRIPTS_DIR / "portkey-stack.sh"
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            command_log = temp / "commands.log"
            render_marker = temp / "rendered"

            helm = fake_bin / "helm"
            helm.write_text(
                r'''#!/usr/bin/env bash
printf 'helm %s\n' "$*" >>"$PORTKEY_TEST_COMMAND_LOG"
if [[ "${1:-}" == version && "${2:-}" == --short ]]; then
  printf '%s\n' "$PORTKEY_TEST_HELM_VERSION"
fi
exit 0
''',
                encoding="utf-8",
            )
            helm.chmod(0o700)

            shell_command = r'''
set -- help
source "$PORTKEY_TEST_SCRIPT" >/dev/null
stack_exists() { return 0; }
render_values() { : >"$1"; touch "$PORTKEY_TEST_RENDER_MARKER"; }
helm_plan
'''
            environment = {
                **os.environ,
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                "PORTKEY_ENV_FILE": str(REPO_ROOT / "does-not-exist"),
                "PORTKEY_TEST_SCRIPT": str(script),
                "PORTKEY_TEST_COMMAND_LOG": str(command_log),
                "PORTKEY_TEST_RENDER_MARKER": str(render_marker),
            }

            command_log.write_text("", encoding="utf-8")
            helm3 = subprocess.run(
                ["bash", "-c", shell_command],
                cwd=REPO_ROOT,
                env={**environment, "PORTKEY_TEST_HELM_VERSION": "v3.21.4+gtest"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(helm3.returncode, 0, helm3.stderr)
            self.assertTrue(render_marker.exists())
            helm3_commands = command_log.read_text(encoding="utf-8")
            self.assertIn("helm template", helm3_commands)
            self.assertIn("--post-renderer", helm3_commands)

            command_log.write_text("", encoding="utf-8")
            render_marker.unlink()
            helm4 = subprocess.run(
                ["bash", "-c", shell_command],
                cwd=REPO_ROOT,
                env={**environment, "PORTKEY_TEST_HELM_VERSION": "v4.2.0+gtest"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(helm4.returncode, 0)
            self.assertIn("Helm 3 is required", helm4.stderr)
            self.assertIn("Helm 4", helm4.stderr)
            self.assertFalse(render_marker.exists())
            helm4_commands = command_log.read_text(encoding="utf-8")
            self.assertEqual(
                helm4_commands.splitlines(),
                ["helm version --short"],
                helm4_commands,
            )

    def test_portkey_exit_traps_do_not_reference_expired_locals(self):
        script = (SCRIPTS_DIR / "portkey-stack.sh").read_text(encoding="utf-8")
        self.assertNotIn("trap 'rm -f \"$", script)
        self.assertNotIn("trap 'stop_tunnel; rm -rf \"$", script)

    def test_portkey_check_requires_v1_gateway_url(self):
        environ = self.portkey_environment()
        environ["PORTKEY_BASE_URL"] = "https://portkey.internal.example"
        result = subprocess.run(
            ["bash", str(SCRIPTS_DIR / "portkey-stack.sh"), "check"],
            cwd=REPO_ROOT,
            env=environ,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("/v1", result.stderr)

        environ["PORTKEY_BASE_URL"] = "http://portkey.internal.example/v1"
        result = subprocess.run(
            ["bash", str(SCRIPTS_DIR / "portkey-stack.sh"), "check"],
            cwd=REPO_ROOT,
            env=environ,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must use https", result.stderr)

    def test_portkey_rejects_group_or_world_readable_environment_file(self):
        with TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / ".env.deploy"
            secret = "mode-test-secret"
            env_file.write_text(f"PORTKEY_API_KEY={secret}\n", encoding="utf-8")
            env_file.chmod(0o644)
            environ = {
                **self.portkey_environment(),
                "PORTKEY_ENV_FILE": str(env_file),
            }

            rejected = subprocess.run(
                ["bash", str(SCRIPTS_DIR / "portkey-stack.sh"), "check"],
                cwd=REPO_ROOT,
                env=environ,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("mode 0600 or 0400", rejected.stderr)
            self.assertNotIn(secret, rejected.stdout + rejected.stderr)

            env_file.chmod(0o600)
            accepted = subprocess.run(
                ["bash", str(SCRIPTS_DIR / "portkey-stack.sh"), "check"],
                cwd=REPO_ROOT,
                env=environ,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            self.assertNotIn(secret, accepted.stdout + accepted.stderr)

    def test_portkey_rejects_public_nlb_and_guards_exposure_changes(self):
        environ = self.portkey_environment()
        environ["PORTKEY_INTERNAL_NLB"] = "false"
        result = subprocess.run(
            ["bash", str(SCRIPTS_DIR / "portkey-stack.sh"), "check"],
            cwd=REPO_ROOT,
            env=environ,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PORTKEY_INTERNAL_NLB must remain true", result.stderr)

        script = (SCRIPTS_DIR / "portkey-stack.sh").read_text(encoding="utf-8")
        match = re.search(
            r"(?ms)^require_safe_nlb_service_upgrade\(\).*?"
            r"(?=^load_balancer_controller_plan\(\))",
            script,
        )
        self.assertIsNotNone(match)
        self.assertIn("aws-load-balancer-scheme", match.group(0))
        self.assertIn("aws-load-balancer-ssl-cert", match.group(0))
        self.assertIn("aws-load-balancer-security-group-prefix-lists", match.group(0))
        self.assertIn('port.get("port") != 443', match.group(0))
        self.assertIn("get ingresses.networking.k8s.io", match.group(0))

    def test_portkey_check_requires_matching_tls_endpoint_inputs(self):
        rejected = (
            (
                {"PORTKEY_GATEWAY_HOSTNAME": ""},
                "PORTKEY_GATEWAY_HOSTNAME",
            ),
            (
                {"PORTKEY_GATEWAY_HOSTNAME": "another.internal.example"},
                "invalid or inconsistent",
            ),
            (
                {"PORTKEY_BASE_URL": "https://portkey.internal.example:8443/v1"},
                "invalid or inconsistent",
            ),
            (
                {
                    "PORTKEY_NLB_TLS_CERTIFICATE_ARN": self.certificate_arn(
                        "us-west-2"
                    )
                },
                "must be in AWS_REGION",
            ),
            (
                {"PORTKEY_NLB_ALLOWED_PREFIX_LIST_IDS": "pl-0123456789abcdef0, pl-1abcdef0"},
                "must not contain whitespace",
            ),
            (
                {
                    "PORTKEY_NLB_ALLOWED_PREFIX_LIST_IDS": (
                        "pl-0123456789abcdef0,pl-0123456789abcdef0"
                    )
                },
                "duplicate prefix-list ID",
            ),
        )
        for overrides, expected_error in rejected:
            with self.subTest(overrides=overrides):
                environ = self.portkey_environment()
                environ.update(overrides)
                result = subprocess.run(
                    ["bash", str(SCRIPTS_DIR / "portkey-stack.sh"), "check"],
                    cwd=REPO_ROOT,
                    env=environ,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected_error, result.stderr)
                self.assertNotIn(
                    "do-not-print-this-secret",
                    result.stdout + result.stderr,
                )

    def test_portkey_cloudformation_parameters_work_with_aws_cli_v1_and_v2(self):
        script = (SCRIPTS_DIR / "portkey-stack.sh").read_text(encoding="utf-8")
        match = re.search(r"(?ms)^deploy\(\) \{(.*?)^\}", script)
        self.assertIsNotNone(match, "missing deploy function")
        deploy = " ".join(match.group(1).replace("\\\n", " ").split())

        # `aws cloudformation deploy` parses parameter overrides as individual
        # key/value arguments in both supported CLI generations. A CloudFormation
        # JSON parameters file is not a portable --parameter-overrides value.
        self.assertNotIn('--parameter-overrides "file://$parameters"', deploy)
        self.assertIn('MantleProjectId="$BEDROCK_MANTLE_PROJECT_ID"', deploy)
        self.assertIn(
            'BedrockMantleRegion="$BEDROCK_MANTLE_REGION"',
            deploy,
        )
        self.assertIn('MantleModelIds="$PORTKEY_ALLOWED_MODELS"', deploy)

    def test_portkey_nlb_is_tls_only_private_and_lbc_owned(self):
        values = (
            REPO_ROOT / "deployment" / "portkey" / "values.yaml.tmpl"
        ).read_text(encoding="utf-8")

        self.assertIn(
            'service.beta.kubernetes.io/aws-load-balancer-type: "external"',
            values,
        )
        self.assertIn(
            'service.beta.kubernetes.io/aws-load-balancer-nlb-target-type: "ip"',
            values,
        )
        self.assertIn(
            "service.beta.kubernetes.io/aws-load-balancer-scheme:",
            values,
        )
        self.assertNotIn(
            "service.beta.kubernetes.io/aws-load-balancer-internal:",
            values,
        )
        self.assertIn("  port: 443", values)
        self.assertIn("  containerPort: 8787", values)
        self.assertNotIn("  port: 80", values)
        self.assertIn(
            "service.beta.kubernetes.io/aws-load-balancer-ssl-cert: "
            "__PORTKEY_NLB_TLS_CERTIFICATE_ARN__",
            values,
        )
        self.assertIn(
            'service.beta.kubernetes.io/aws-load-balancer-ssl-ports: "443"',
            values,
        )
        self.assertIn(
            "service.beta.kubernetes.io/aws-load-balancer-security-group-prefix-lists: "
            "__PORTKEY_NLB_ALLOWED_PREFIX_LIST_IDS__",
            values,
        )
        self.assertIn(
            'service.beta.kubernetes.io/aws-load-balancer-backend-protocol: "tcp"',
            values,
        )
        self.assertNotIn("0.0.0.0/0", values)
        self.assertNotIn("loadBalancerSourceRanges", values)
        self.assertRegex(values, r"(?m)^ingress:\n  enabled: false$")
        self.assertRegex(values, r"(?m)^mcpService:\n  enabled: false$")
        self.assertRegex(
            values,
            r"(?m)^redis:\n  serviceType: ClusterIP$",
        )
        self.assertNotIn("serviceType: NodePort", values)

    def test_portkey_local_port_forward_targets_service_tls_port(self):
        script = (SCRIPTS_DIR / "portkey-stack.sh").read_text(encoding="utf-8")
        match = re.search(
            r"(?ms)^prepare_runtime_url\(\) \{(.*?)^\}",
            script,
        )
        self.assertIsNotNone(match)
        prepare_runtime_url = match.group(1)
        self.assertIn("18787:gateway", prepare_runtime_url)
        self.assertNotIn("18787:80", prepare_runtime_url)
        self.assertIn(
            "RUNTIME_URL=http://127.0.0.1:18787/v1",
            prepare_runtime_url,
        )

    def test_portkey_service_upgrade_guard_accepts_only_reviewed_tls_shape(self):
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            kubectl = fake_bin / "kubectl"
            kubectl.write_text(
                """#!/usr/bin/env bash
case "$*" in
  *"get ingresses.networking.k8s.io"*) exit 0 ;;
  *"get service portkey-ai-gateway"*"--ignore-not-found -o name"*)
    [[ "${PORTKEY_TEST_SERVICE_ABSENT:-}" == 1 ]] || printf '%s' 'service/portkey-ai-gateway'
    ;;
  *"get service portkey-ai-gateway -o json"*) printf '%s' "$PORTKEY_TEST_SERVICE_JSON" ;;
esac
exit 0
""",
                encoding="utf-8",
            )
            kubectl.chmod(0o700)

            annotations = {
                "service.beta.kubernetes.io/aws-load-balancer-type": "external",
                "service.beta.kubernetes.io/aws-load-balancer-scheme": "internal",
                "service.beta.kubernetes.io/aws-load-balancer-ip-address-type": "ipv4",
                "service.beta.kubernetes.io/aws-load-balancer-nlb-target-type": "ip",
                "service.beta.kubernetes.io/aws-load-balancer-ssl-cert": (
                    self.certificate_arn()
                ),
                "service.beta.kubernetes.io/aws-load-balancer-ssl-ports": "443",
                "service.beta.kubernetes.io/aws-load-balancer-ssl-negotiation-policy": (
                    "ELBSecurityPolicy-TLS13-1-2-2021-06"
                ),
                "service.beta.kubernetes.io/aws-load-balancer-backend-protocol": "tcp",
                "service.beta.kubernetes.io/aws-load-balancer-security-group-prefix-lists": (
                    "pl-0123456789abcdef0"
                ),
                "service.beta.kubernetes.io/aws-load-balancer-healthcheck-path": (
                    "/v1/health"
                ),
                "service.beta.kubernetes.io/aws-load-balancer-healthcheck-protocol": (
                    "http"
                ),
                "service.beta.kubernetes.io/aws-load-balancer-healthcheck-port": "8787",
                "service.beta.kubernetes.io/aws-load-balancer-manage-backend-security-group-rules": (
                    "true"
                ),
            }

            def service_payload(service_annotations, port=443, extra_spec=None):
                spec = {
                    "type": "LoadBalancer",
                    "ports": [
                        {
                            "name": "gateway",
                            "port": port,
                            "protocol": "TCP",
                            "targetPort": "gateway",
                        }
                    ],
                }
                spec.update(extra_spec or {})
                return json.dumps(
                    {
                        "metadata": {"annotations": service_annotations},
                        "spec": spec,
                    }
                )

            environment = {
                **os.environ,
                "AWS_REGION": "us-east-1",
                "PORTKEY_ENV_FILE": str(temp / "does-not-exist"),
                "PORTKEY_NLB_TLS_CERTIFICATE_ARN": self.certificate_arn(),
                "PORTKEY_NLB_ALLOWED_PREFIX_LIST_IDS": "pl-0123456789abcdef0",
                "PORTKEY_TEST_SCRIPT": str(SCRIPTS_DIR / "portkey-stack.sh"),
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            }
            shell_command = (
                'set -- help; source "$PORTKEY_TEST_SCRIPT" >/dev/null; '
                "require_safe_nlb_service_upgrade"
            )

            accepted = subprocess.run(
                ["bash", "-c", shell_command],
                cwd=REPO_ROOT,
                env={
                    **environment,
                    "PORTKEY_TEST_SERVICE_JSON": service_payload(annotations),
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(accepted.returncode, 0, accepted.stderr)

            rotated_annotations = {
                **annotations,
                "service.beta.kubernetes.io/aws-load-balancer-ssl-cert": (
                    "arn:aws:acm:us-east-1:123456789012:certificate/"
                    "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
                ),
                "service.beta.kubernetes.io/aws-load-balancer-security-group-prefix-lists": (
                    "pl-0fedcba9876543210"
                ),
            }
            unconfirmed_rotation = subprocess.run(
                ["bash", "-c", shell_command],
                cwd=REPO_ROOT,
                env={
                    **environment,
                    "PORTKEY_TEST_SERVICE_JSON": service_payload(
                        rotated_annotations
                    ),
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(unconfirmed_rotation.returncode, 0)
            self.assertIn("CONFIRM_PORTKEY_NLB_TLS_UPDATE", unconfirmed_rotation.stderr)

            confirmed_rotation = subprocess.run(
                ["bash", "-c", shell_command],
                cwd=REPO_ROOT,
                env={
                    **environment,
                    "CONFIRM_PORTKEY_NLB_TLS_UPDATE": "portkeyai/portkey-ai-gateway",
                    "PORTKEY_TEST_SERVICE_JSON": service_payload(
                        rotated_annotations
                    ),
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(confirmed_rotation.returncode, 0, confirmed_rotation.stderr)

            rejected_services = {
                "legacy plaintext port": service_payload(annotations, port=80),
                "custom security group": service_payload(
                    {
                        **annotations,
                        "service.beta.kubernetes.io/aws-load-balancer-security-groups": (
                            "sg-0123456789abcdef0"
                        ),
                    }
                ),
                "source range override": service_payload(
                    annotations,
                    extra_spec={"loadBalancerSourceRanges": ["10.0.0.0/8"]},
                ),
            }
            for description, payload in rejected_services.items():
                with self.subTest(description=description):
                    rejected = subprocess.run(
                        ["bash", "-c", shell_command],
                        cwd=REPO_ROOT,
                        env={
                            **environment,
                            "PORTKEY_TEST_SERVICE_JSON": payload,
                        },
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertNotEqual(rejected.returncode, 0)
                    self.assertIn("no resources were changed", rejected.stderr)

            post_shell_command = (
                'set -- help; source "$PORTKEY_TEST_SCRIPT" >/dev/null; '
                "require_safe_nlb_service_upgrade post"
            )
            accepted_post = subprocess.run(
                ["bash", "-c", post_shell_command],
                cwd=REPO_ROOT,
                env={
                    **environment,
                    "PORTKEY_TEST_SERVICE_JSON": service_payload(annotations),
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(accepted_post.returncode, 0, accepted_post.stderr)

            post_rotation = subprocess.run(
                ["bash", "-c", post_shell_command],
                cwd=REPO_ROOT,
                env={
                    **environment,
                    "CONFIRM_PORTKEY_NLB_TLS_UPDATE": "portkeyai/portkey-ai-gateway",
                    "PORTKEY_TEST_SERVICE_JSON": service_payload(
                        rotated_annotations
                    ),
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(post_rotation.returncode, 0)
            self.assertIn("the Helm release changed", post_rotation.stderr)
            self.assertNotIn("no resources were changed", post_rotation.stderr)

            absent_pre = subprocess.run(
                ["bash", "-c", shell_command],
                cwd=REPO_ROOT,
                env={**environment, "PORTKEY_TEST_SERVICE_ABSENT": "1"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(absent_pre.returncode, 0, absent_pre.stderr)

            absent_post = subprocess.run(
                ["bash", "-c", post_shell_command],
                cwd=REPO_ROOT,
                env={**environment, "PORTKEY_TEST_SERVICE_ABSENT": "1"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(absent_post.returncode, 0)
            self.assertIn("the Helm release changed", absent_post.stderr)
            self.assertNotIn("no resources were changed", absent_post.stderr)

            post_helm_failure = subprocess.run(
                ["bash", "-c", post_shell_command],
                cwd=REPO_ROOT,
                env={
                    **environment,
                    "PORTKEY_TEST_SERVICE_JSON": service_payload(
                        annotations,
                        port=80,
                    ),
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(post_helm_failure.returncode, 0)
            self.assertIn("the Helm release changed", post_helm_failure.stderr)
            self.assertNotIn("no resources were changed", post_helm_failure.stderr)

        script_text = (SCRIPTS_DIR / "portkey-stack.sh").read_text(
            encoding="utf-8"
        )
        helm_deploy = re.search(
            r"(?ms)^helm_deploy\(\) \{(.*?)^\}",
            script_text,
        )
        self.assertIsNotNone(helm_deploy)
        body = helm_deploy.group(1)
        preflight_index = body.index("require_safe_nlb_service_upgrade pre")
        nodeport_index = body.index("remove_gateway_nodeport_allocation")
        helm_index = body.index("helm upgrade --install")
        postflight_index = body.index("require_safe_nlb_service_upgrade post")
        surface_index = body.index("validate_portkey_service_surface")
        self.assertLess(preflight_index, nodeport_index)
        self.assertLess(nodeport_index, helm_index)
        self.assertLess(helm_index, postflight_index)
        self.assertLess(postflight_index, surface_index)
        self.assertIn(
            'PORTKEY_POST_RENDER_SERVICE_NAME="$PORTKEY_GATEWAY_SERVICE"',
            body,
        )
        self.assertIn('--post-renderer "$PORTKEY_POST_RENDERER"', body)

        helm_plan = re.search(
            r"(?ms)^helm_plan\(\) \{(.*?)^\}",
            script_text,
        )
        self.assertIsNotNone(helm_plan)
        self.assertIn(
            'PORTKEY_POST_RENDER_SERVICE_NAME="$PORTKEY_GATEWAY_SERVICE"',
            helm_plan.group(1),
        )
        self.assertIn(
            '--post-renderer "$PORTKEY_POST_RENDERER"',
            helm_plan.group(1),
        )

    def test_portkey_gateway_nodeport_migration_is_scoped_and_idempotent(self):
        script = SCRIPTS_DIR / "portkey-stack.sh"
        shell_command = r'''
set -- help
source "$PORTKEY_TEST_SCRIPT" >/dev/null
require_safe_nlb_service_upgrade() {
  printf 'guard %s\n' "${1:-pre}" >>"$PORTKEY_TEST_COMMAND_LOG"
}
kubectl() {
  printf 'kubectl %s\n' "$*" >>"$PORTKEY_TEST_COMMAND_LOG"
  case "$*" in
    *"--ignore-not-found -o name"*)
      [[ "${PORTKEY_TEST_SERVICE_ABSENT:-}" == 1 ]] || printf '%s' 'service/portkey-ai-gateway'
      ;;
    *"get service portkey-ai-gateway -o json"*) printf '%s' "$PORTKEY_TEST_SERVICE_JSON" ;;
    *"patch service portkey-ai-gateway"*)
      [[ "${PORTKEY_TEST_PATCH_FAILURE:-}" != 1 ]] || return 42
      ;;
  esac
}
remove_gateway_nodeport_allocation
'''

        def service_payload(*, allocate=None, node_port=None, extra_port=False):
            port = {
                "name": "gateway",
                "port": 443,
                "protocol": "TCP",
                "targetPort": "gateway",
            }
            if node_port is not None:
                port["nodePort"] = node_port
            ports = [port]
            if extra_port:
                ports.append(
                    {
                        "name": "unexpected",
                        "port": 8443,
                        "protocol": "TCP",
                        "targetPort": "gateway",
                    }
                )
            spec = {"type": "LoadBalancer", "ports": ports}
            if allocate is not None:
                spec["allocateLoadBalancerNodePorts"] = allocate
            return json.dumps({"spec": spec}, separators=(",", ":"))

        with TemporaryDirectory() as temp_dir:
            command_log = Path(temp_dir) / "commands.log"
            environment = {
                **os.environ,
                "PORTKEY_ENV_FILE": str(REPO_ROOT / "does-not-exist"),
                "PORTKEY_TEST_SCRIPT": str(script),
                "PORTKEY_TEST_COMMAND_LOG": str(command_log),
            }

            command_log.write_text("", encoding="utf-8")
            migrated = subprocess.run(
                ["bash", "-c", shell_command],
                cwd=REPO_ROOT,
                env={
                    **environment,
                    "PORTKEY_TEST_SERVICE_JSON": service_payload(node_port=32443),
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            migration_commands = command_log.read_text(encoding="utf-8")
            expected_patch = (
                '[{"op":"add","path":"/spec/allocateLoadBalancerNodePorts",'
                '"value":false},{"op":"remove","path":'
                '"/spec/ports/0/nodePort"}]'
            )
            self.assertIn(expected_patch, migration_commands)
            self.assertLess(
                migration_commands.index("guard pre"),
                migration_commands.index("patch service portkey-ai-gateway"),
            )

            command_log.write_text("", encoding="utf-8")
            already_clean = subprocess.run(
                ["bash", "-c", shell_command],
                cwd=REPO_ROOT,
                env={
                    **environment,
                    "PORTKEY_TEST_SERVICE_JSON": service_payload(allocate=False),
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(already_clean.returncode, 0, already_clean.stderr)
            clean_commands = command_log.read_text(encoding="utf-8")
            self.assertIn("guard pre", clean_commands)
            self.assertNotIn("patch service", clean_commands)

            command_log.write_text("", encoding="utf-8")
            malformed = subprocess.run(
                ["bash", "-c", shell_command],
                cwd=REPO_ROOT,
                env={
                    **environment,
                    "PORTKEY_TEST_SERVICE_JSON": service_payload(extra_port=True),
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(malformed.returncode, 0)
            self.assertIn("could not construct", malformed.stderr)
            self.assertNotIn(
                "patch service", command_log.read_text(encoding="utf-8")
            )

            command_log.write_text("", encoding="utf-8")
            patch_failure = subprocess.run(
                ["bash", "-c", shell_command],
                cwd=REPO_ROOT,
                env={
                    **environment,
                    "PORTKEY_TEST_SERVICE_JSON": service_payload(node_port=32443),
                    "PORTKEY_TEST_PATCH_FAILURE": "1",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(patch_failure.returncode, 0)
            self.assertIn("could not atomically disable", patch_failure.stderr)

    def test_portkey_service_surface_postcheck_rejects_nodeport_paths(self):
        script = SCRIPTS_DIR / "portkey-stack.sh"
        shell_command = r'''
set -- help
source "$PORTKEY_TEST_SCRIPT" >/dev/null
kubectl() {
  [[ "$*" == *"get services -o json"* ]] || return 1
  printf '%s' "$PORTKEY_TEST_SERVICES_JSON"
}
validate_portkey_service_surface
'''

        def service(name, service_type, *, allocate=None, node_port=None):
            port = {"port": 443 if name != "redis" else 6379}
            if node_port is not None:
                port["nodePort"] = node_port
            spec = {"type": service_type, "ports": [port]}
            if allocate is not None:
                spec["allocateLoadBalancerNodePorts"] = allocate
            return {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": name},
                "spec": spec,
            }

        gateway = service(
            "portkey-ai-gateway",
            "LoadBalancer",
            allocate=False,
        )
        redis = service("redis", "ClusterIP")
        environment = {
            **os.environ,
            "PORTKEY_ENV_FILE": str(REPO_ROOT / "does-not-exist"),
            "PORTKEY_TEST_SCRIPT": str(script),
        }

        accepted = subprocess.run(
            ["bash", "-c", shell_command],
            cwd=REPO_ROOT,
            env={
                **environment,
                "PORTKEY_TEST_SERVICES_JSON": json.dumps(
                    {"items": [gateway, redis]}
                ),
            },
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)

        rejected_surfaces = {
            "gateway allocation defaults on": [
                service("portkey-ai-gateway", "LoadBalancer"),
                redis,
            ],
            "gateway nodeport": [
                service(
                    "portkey-ai-gateway",
                    "LoadBalancer",
                    allocate=False,
                    node_port=32443,
                ),
                redis,
            ],
            "redis nodeport service": [
                gateway,
                service("redis", "NodePort", node_port=30379),
            ],
            "redis allocated nodeport": [
                gateway,
                service("redis", "ClusterIP", node_port=30379),
            ],
            "second load balancer": [
                gateway,
                redis,
                service("unexpected", "LoadBalancer", allocate=False),
            ],
        }
        for description, services in rejected_surfaces.items():
            with self.subTest(description=description):
                rejected = subprocess.run(
                    ["bash", "-c", shell_command],
                    cwd=REPO_ROOT,
                    env={
                        **environment,
                        "PORTKEY_TEST_SERVICES_JSON": json.dumps(
                            {"items": services}
                        ),
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(rejected.returncode, 0)
                self.assertIn("unexpected Service exposure", rejected.stderr)

    def test_portkey_post_renderer_targets_one_gateway_without_echoing_input(self):
        post_renderer = (
            REPO_ROOT / "deployment" / "portkey" / "portkey-post-renderer.sh"
        )
        environment = {
            **os.environ,
            "PORTKEY_POST_RENDER_SERVICE_NAME": "portkey-ai-gateway",
        }
        gateway = """apiVersion: v1
kind: Service
metadata:
  name: portkey-ai-gateway
  labels:
    app.kubernetes.io/name: gateway
spec:
  type: LoadBalancer
  ports:
    - name: gateway
      port: 443
---
apiVersion: v1
kind: Service
metadata:
  name: redis
spec:
  type: ClusterIP
  ports:
    - port: 6379
"""
        rendered = subprocess.run(
            ["bash", str(post_renderer)],
            cwd=REPO_ROOT,
            env=environment,
            input=gateway,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        self.assertIn("allocateLoadBalancerNodePorts: false", rendered.stdout)
        self.assertEqual(rendered.stdout.count("type: LoadBalancer"), 1)
        self.assertEqual(rendered.stdout.count("type: ClusterIP"), 1)

        canary = "manifest-secret-canary"
        rejected_manifests = {
            "missing": gateway.replace("portkey-ai-gateway", "another-service"),
            "wrong type": gateway.replace("type: LoadBalancer", "type: ClusterIP", 1),
            "duplicate": gateway + "---\n" + gateway.split("---\n", 1)[0],
            "malformed": f"apiVersion: v1\nkind: Service\nmetadata: [{canary}\n",
        }
        for description, manifest in rejected_manifests.items():
            with self.subTest(description=description):
                rejected = subprocess.run(
                    ["bash", str(post_renderer)],
                    cwd=REPO_ROOT,
                    env=environment,
                    input=manifest,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(rejected.returncode, 0)
                self.assertIn(
                    "exactly one LoadBalancer Service named portkey-ai-gateway",
                    rejected.stderr,
                )
                self.assertNotIn(canary, rejected.stdout + rejected.stderr)

    def test_portkey_gateway_helm_failure_warns_release_may_have_changed(self):
        script = SCRIPTS_DIR / "portkey-stack.sh"
        shell_command = r'''
set -- help
source "$PORTKEY_TEST_SCRIPT" >/dev/null
helm_plan() { :; }
confirm_write() { :; }
require_command() { :; }
kube_context() { :; }
require_load_balancer_controller() { :; }
require_safe_nlb_service_upgrade() { :; }
remove_gateway_nodeport_allocation() { :; }
validate_portkey_service_surface() { :; }
render_values() { : >"$1"; }
helm() { return 42; }
helm_deploy
'''
        result = subprocess.run(
            ["bash", "-c", shell_command],
            cwd=REPO_ROOT,
            env={
                **os.environ,
                "PORTKEY_ENV_FILE": str(REPO_ROOT / "does-not-exist"),
                "PORTKEY_TEST_SCRIPT": str(script),
            },
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("may have changed the Portkey release", result.stderr)
        self.assertIn("reviewed TLS path", result.stderr)
        self.assertIn("Do not roll back", result.stderr)
        self.assertNotIn("no resources were changed", result.stderr)

    def test_portkey_fresh_cluster_installs_and_requires_lbc(self):
        script = (SCRIPTS_DIR / "portkey-stack.sh").read_text(encoding="utf-8")
        cluster = (
            REPO_ROOT / "deployment" / "portkey" / "eksctl-cluster.yaml.tmpl"
        ).read_text(encoding="utf-8")
        env_example = (
            REPO_ROOT / "deployment" / "portkey" / ".env.deploy.example"
        ).read_text(encoding="utf-8")

        def function_body(name):
            match = re.search(
                rf"(?ms)^{re.escape(name)}\(\) \{{(.*?)^\}}",
                script,
            )
            self.assertIsNotNone(match, f"missing {name} function")
            return match.group(1)

        self.assertNotIn("awsLoadBalancerController", cluster)
        self.assertNotIn("serviceAccounts:", cluster)
        self.assertRegex(
            env_example,
            r"(?m)^PORTKEY_LBC_HELM_CHART_VERSION=\d+\.\d+\.\d+$",
        )
        for tls_input in (
            "PORTKEY_GATEWAY_HOSTNAME=",
            "PORTKEY_NLB_TLS_CERTIFICATE_ARN=",
            "PORTKEY_NLB_ALLOWED_PREFIX_LIST_IDS=",
        ):
            self.assertIn(tls_input, env_example)

        install = function_body("install_load_balancer_controller")
        self.assertIn("eks/aws-load-balancer-controller", install)
        self.assertIn("render_load_balancer_controller_service_account", install)
        self.assertIn("serviceAccount.create=false", install)
        self.assertIn("watchNamespace=\"$PORTKEY_NAMESPACE\"", install)
        self.assertIn("enableServiceMutatorWebhook=false", install)
        self.assertIn("featureGates.ListenerRulesTagging=true", install)
        self.assertIn(
            "serviceAccount.name=aws-load-balancer-controller",
            install,
        )
        self.assertIn("rollout status", install)

        self.assertIn(
            "install_load_balancer_controller",
            function_body("cluster_deploy"),
        )
        self.assertIn(
            "require_load_balancer_controller",
            function_body("helm_deploy"),
        )
        self.assertIn(
            "require_safe_nlb_service_upgrade",
            function_body("helm_deploy"),
        )

    def test_portkey_lbc_service_account_uses_real_eksctl_ownership_label(self):
        script = SCRIPTS_DIR / "portkey-stack.sh"
        shell_command = r'''
set -- help
source "$PORTKEY_TEST_SCRIPT" >/dev/null
kubectl() {
  case "$*" in
    *"managed-by"*) printf '%s' "$PORTKEY_TEST_MANAGED_BY" ;;
    *"role-arn"*) printf '%s' 'arn:aws:iam::123456789012:role/lbc' ;;
  esac
}
aws_cli() { printf '%s\n' "$PORTKEY_TEST_APPLICATION_TAG"; }
load_balancer_controller_service_account_is_managed
'''
        environment = {
            **os.environ,
            "PORTKEY_ENV_FILE": str(REPO_ROOT / "does-not-exist"),
            "PORTKEY_TEST_SCRIPT": str(script),
            "PORTKEY_TEST_APPLICATION_TAG": "guidance-codex-portkey",
        }

        managed = subprocess.run(
            ["bash", "-c", shell_command],
            cwd=REPO_ROOT,
            env={**environment, "PORTKEY_TEST_MANAGED_BY": "eksctl"},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(managed.returncode, 0, managed.stderr)

        for unrelated_label in ("guidance-codex", "platform-team", ""):
            with self.subTest(managed_by=unrelated_label):
                unmanaged = subprocess.run(
                    ["bash", "-c", shell_command],
                    cwd=REPO_ROOT,
                    env={
                        **environment,
                        "PORTKEY_TEST_MANAGED_BY": unrelated_label,
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(unmanaged.returncode, 0)

        for application_tag in ("", "another-application"):
            with self.subTest(application_tag=application_tag):
                unowned_stack = subprocess.run(
                    ["bash", "-c", shell_command],
                    cwd=REPO_ROOT,
                    env={
                        **environment,
                        "PORTKEY_TEST_MANAGED_BY": "eksctl",
                        "PORTKEY_TEST_APPLICATION_TAG": application_tag,
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(unowned_stack.returncode, 0)

    def test_portkey_lbc_policy_is_checked_in_and_region_vpc_tag_scoped(self):
        policy_path = (
            REPO_ROOT / "deployment" / "portkey" / "lbc-iam-policy.json.tmpl"
        )
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        serialized = json.dumps(policy)
        actions = {
            action
            for statement in policy["Statement"]
            for action in (
                statement["Action"]
                if isinstance(statement["Action"], list)
                else [statement["Action"]]
            )
        }

        self.assertIn("__AWS_ACCOUNT_ID__", serialized)
        self.assertIn("__VPC_ID__", serialized)
        self.assertIn("__CLUSTER_NAME__", serialized)
        self.assertIn("__AWS_REGION__", serialized)
        self.assertNotIn("us-east-1", serialized)
        self.assertIn("ec2:Vpc", serialized)
        self.assertIn('"elasticloadbalancing:Scheme": "internal"', serialized)
        self.assertNotIn("wafv2:", serialized)
        self.assertNotIn("shield:", serialized)
        self.assertNotIn("acm:", serialized)
        self.assertNotIn("elasticloadbalancing:CreateRule", actions)
        for required_action in (
            "elasticloadbalancing:DescribeListenerCertificates",
            "elasticloadbalancing:AddListenerCertificates",
            "elasticloadbalancing:RemoveListenerCertificates",
        ):
            self.assertIn(required_action, actions)

        create_listener = next(
            statement
            for statement in policy["Statement"]
            if statement["Action"] == "elasticloadbalancing:CreateListener"
        )
        self.assertEqual(
            create_listener["Condition"]["ForAnyValue:StringEquals"][
                "elasticloadbalancing:ListenerProtocol"
            ],
            ["TLS"],
        )
        self.assertEqual(
            create_listener["Condition"]["ForAnyValue:StringEquals"][
                "elasticloadbalancing:SecurityPolicy"
            ],
            ["ELBSecurityPolicy-TLS13-1-2-2021-06"],
        )
        self.assertIn(":loadbalancer/net/", create_listener["Resource"])

        modify_listener = next(
            statement
            for statement in policy["Statement"]
            if statement["Action"] == "elasticloadbalancing:ModifyListener"
        )
        self.assertEqual(
            modify_listener["Condition"]["ForAnyValue:StringEquals"][
                "elasticloadbalancing:ListenerProtocol"
            ],
            ["TLS"],
        )
        self.assertEqual(
            modify_listener["Condition"]["ForAnyValue:StringEquals"][
                "elasticloadbalancing:SecurityPolicy"
            ],
            ["ELBSecurityPolicy-TLS13-1-2-2021-06"],
        )

        manage_listener_certificates = next(
            statement
            for statement in policy["Statement"]
            if "elasticloadbalancing:AddListenerCertificates"
            in statement["Action"]
        )
        self.assertIn(":listener/net/", manage_listener_certificates["Resource"])

        script = (SCRIPTS_DIR / "portkey-stack.sh").read_text(encoding="utf-8")
        self.assertIn("render_load_balancer_controller_service_account()", script)
        self.assertIn('"__AWS_REGION__": os.environ["AWS_REGION"]', script)
        self.assertIn('"attachPolicy": policy', script)
        self.assertIn("found_placeholders != set(replacements)", script)
        self.assertNotIn('if "__" in policy_text', script)
        self.assertNotIn("wellKnownPolicies", script)

    def test_portkey_lbc_plan_renders_alternate_region_and_partition(self):
        script = SCRIPTS_DIR / "portkey-stack.sh"
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            rendered_lbc = temp / "rendered-lbc.json"
            real_python = shutil.which("python3") or sys.executable

            python = fake_bin / "python3"
            python.write_text(
                """#!/usr/bin/env bash
if [[ "${1:-}" == '-m' && "${2:-}" == 'json.tool' ]]; then
  cp "$3" "$PORTKEY_TEST_RENDERED_LBC"
fi
exec "$PORTKEY_TEST_REAL_PYTHON" "$@"
""",
                encoding="utf-8",
            )
            python.chmod(0o700)

            helm = fake_bin / "helm"
            helm.write_text(
                "#!/usr/bin/env bash\n"
                "if [[ \"${1:-}\" == version && \"${2:-}\" == --short ]]; "
                "then printf '%s\\n' 'v3.21.4+gtest'; fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            helm.chmod(0o700)

            environ = self.portkey_environment()
            environ.update(
                {
                    "AWS_REGION": "us-gov-west-1",
                    "BEDROCK_MANTLE_REGION": "us-gov-west-1",
                    "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                    "PORTKEY_TEST_REAL_PYTHON": real_python,
                    "PORTKEY_TEST_RENDERED_LBC": str(rendered_lbc),
                }
            )
            result = subprocess.run(
                ["bash", str(script), "lbc-plan"],
                cwd=REPO_ROOT,
                env=environ,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            rendered = json.loads(rendered_lbc.read_text(encoding="utf-8"))
            self.assertEqual(rendered["metadata"]["region"], "us-gov-west-1")
            policy = rendered["iam"]["serviceAccounts"][0]["attachPolicy"]
            serialized = json.dumps(policy)
            self.assertNotIn("__", serialized)
            self.assertNotIn("us-east-1", serialized)

            requested_regions = []
            resource_arns = []
            for statement in policy["Statement"]:
                conditions = statement.get("Condition", {})
                region = conditions.get("StringEquals", {}).get(
                    "aws:RequestedRegion"
                )
                if region:
                    requested_regions.append(region)
                resources = statement.get("Resource", [])
                if isinstance(resources, str):
                    resources = [resources]
                resource_arns.extend(
                    resource
                    for resource in resources
                    if resource.startswith("arn:")
                )

            self.assertTrue(requested_regions)
            self.assertEqual(set(requested_regions), {"us-gov-west-1"})
            self.assertTrue(resource_arns)
            for arn in resource_arns:
                self.assertTrue(arn.startswith("arn:aws-us-gov:"), arn)
                self.assertIn(":us-gov-west-1:", arn)

    def test_portkey_reuses_only_a_compatible_controller_watch_scope(self):
        script = SCRIPTS_DIR / "portkey-stack.sh"
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            policy_marker = temp / "policy.json"
            command_log = temp / "commands.log"
            command_log.write_text("", encoding="utf-8")
            current_policy = self.rendered_lbc_policy()
            policy_marker.write_text(json.dumps(current_policy), encoding="utf-8")

            aws = fake_bin / "aws"
            aws.write_text(
                """#!/usr/bin/env bash
printf 'aws %s\n' "$*" >>"$PORTKEY_TEST_COMMAND_LOG"
case "$*" in
  *"sts get-caller-identity"*"--query Account"*) printf '%s\n' '123456789012' ;;
  *"sts get-caller-identity"*) printf '%s\n' '{}' ;;
  *"cluster.identity.oidc.issuer"*) printf '%s\n' 'https://oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE' ;;
  *"cluster.resourcesVpcConfig.vpcId"*) printf '%s\n' 'vpc-0123456789abcdef0' ;;
  *"acm describe-certificate"*) printf '%s\n' '{"Status":"ISSUED","DomainName":"portkey.internal.example","SubjectAlternativeNames":["portkey.internal.example"]}' ;;
  *"ec2 describe-managed-prefix-lists"*) printf '%s\n' '{"PrefixLists":[{"PrefixListId":"pl-0123456789abcdef0","OwnerId":"123456789012","AddressFamily":"IPv4","State":"create-complete","MaxEntries":20}]}' ;;
  *"ec2 get-managed-prefix-list-entries"*) printf '%s\n' '{"Entries":[{"Cidr":"10.0.0.0/8"}]}' ;;
  *"iam get-role-policy"*) cat "$PORTKEY_TEST_POLICY_MARKER" ;;
  *"iam get-role"*) printf '%s\n' '{"Role":{"Arn":"arn:aws:iam::123456789012:role/platform-lbc","PermissionsBoundary":{"PermissionsBoundaryType":"Policy","PermissionsBoundaryArn":"arn:aws:iam::123456789012:policy/platform-boundary"},"AssumeRolePolicyDocument":{"Statement":[{"Effect":"Allow","Action":"sts:AssumeRoleWithWebIdentity","Principal":{"Federated":"arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE"},"Condition":{"StringEquals":{"oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE:aud":"sts.amazonaws.com","oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE:sub":"system:serviceaccount:kube-system:aws-load-balancer-controller"}}},{"Effect":"Allow","Action":["sts:AssumeRoleWithWebIdentity","sts:TagSession"],"Principal":{"Federated":["arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/OTHER","arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/ANOTHER"]},"Condition":{"StringEquals":{"oidc.eks.us-east-1.amazonaws.com/id/OTHER:aud":["sts.amazonaws.com","example.invalid"],"oidc.eks.us-east-1.amazonaws.com/id/OTHER:sub":["system:serviceaccount:other:one","system:serviceaccount:other:two"]}}}]}}}' ;;
  *"iam list-role-policies"*) printf '%s\n' '["TLS"]' ;;
  *"iam list-attached-role-policies"*) printf '%s\n' '[]' ;;
esac
exit 0
""",
                encoding="utf-8",
            )
            aws.chmod(0o700)

            eksctl = fake_bin / "eksctl"
            eksctl.write_text(
                """#!/usr/bin/env bash
if [[ "${1:-}" == version ]]; then printf '%s\n' "${PORTKEY_TEST_EKSCTL_VERSION:-0.230.0}"; exit 0; fi
printf 'eksctl %s\n' "$*" >>"$PORTKEY_TEST_COMMAND_LOG"
exit 0
""",
                encoding="utf-8",
            )
            eksctl.chmod(0o700)

            helm = fake_bin / "helm"
            helm.write_text(
                """#!/usr/bin/env bash
printf 'helm %s\n' "$*" >>"$PORTKEY_TEST_COMMAND_LOG"
if [[ "${1:-}" == version && "${2:-}" == --short ]]; then printf '%s\n' 'v3.21.4+gtest'; fi
exit 0
""",
                encoding="utf-8",
            )
            helm.chmod(0o700)

            kubectl = fake_bin / "kubectl"
            kubectl.write_text(
                """#!/usr/bin/env bash
printf 'kubectl %s\n' "$*" >>"$PORTKEY_TEST_COMMAND_LOG"
case "$*" in
  *"get deployment aws-load-balancer-controller --ignore-not-found -o name"*) printf '%s' 'deployment.apps/aws-load-balancer-controller' ;;
  *"rollout status"*) exit 0 ;;
  *"get crd targetgroupbindings.elbv2.k8s.aws"*) printf '%s' 'customresourcedefinition.apiextensions.k8s.io/targetgroupbindings.elbv2.k8s.aws' ;;
  *"get serviceaccount aws-load-balancer-controller"*"role-arn"*) printf '%s' 'arn:aws:iam::123456789012:role/platform-lbc' ;;
  *"get serviceaccount aws-load-balancer-controller"*"managed-by"*) printf '%s' 'platform-team' ;;
  *"get serviceaccount aws-load-balancer-controller"*) printf '%s' 'serviceaccount/aws-load-balancer-controller' ;;
  *"get deployment aws-load-balancer-controller -o json"*)
    label_version="${PORTKEY_TEST_LBC_LABEL_VERSION:-v3.4.2}"
    image_version="${PORTKEY_TEST_LBC_IMAGE_VERSION:-v3.4.2}"
    watch="${PORTKEY_TEST_LBC_WATCH-portkeyai}"
    args='["--cluster-name=codex-portkey"]'
    if [[ "$watch" != all ]]; then
      args='["--cluster-name=codex-portkey","--watch-namespace='"$watch"'"]'
    fi
    if [[ -n "${PORTKEY_TEST_LBC_EXTRA_ARG:-}" ]]; then
      args="${args%]},\\\"$PORTKEY_TEST_LBC_EXTRA_ARG\\\"]"
    fi
    printf '{"metadata":{"labels":{"app.kubernetes.io/version":"%s"}},"spec":{"template":{"spec":{"serviceAccountName":"aws-load-balancer-controller","containers":[{"name":"aws-load-balancer-controller","image":"public.ecr.aws/eks/aws-load-balancer-controller:%s","args":%s}]}}}}' "$label_version" "$image_version" "$args"
    ;;
esac
exit 0
""",
                encoding="utf-8",
            )
            kubectl.chmod(0o700)

            environ = {
                **os.environ,
                "AWS_REGION": "us-east-1",
                "PORTKEY_EXTERNAL_LBC_POLICY_CONFIRMED": "true",
                "PORTKEY_GATEWAY_HOSTNAME": "portkey.internal.example",
                "PORTKEY_NLB_TLS_CERTIFICATE_ARN": self.certificate_arn(),
                "PORTKEY_NLB_ALLOWED_PREFIX_LIST_IDS": "pl-0123456789abcdef0",
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                "PORTKEY_ENV_FILE": str(temp / "does-not-exist"),
                "PORTKEY_TEST_POLICY_MARKER": str(policy_marker),
                "PORTKEY_TEST_COMMAND_LOG": str(command_log),
            }
            unconfirmed_external = subprocess.run(
                ["bash", str(script), "lbc-status"],
                cwd=REPO_ROOT,
                env={
                    **environ,
                    "PORTKEY_EXTERNAL_LBC_POLICY_CONFIRMED": "false",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(unconfirmed_external.returncode, 0)
            self.assertIn(
                "PORTKEY_EXTERNAL_LBC_POLICY_CONFIRMED=true",
                unconfirmed_external.stderr,
            )
            compatible = subprocess.run(
                ["bash", str(script), "lbc-status"],
                cwd=REPO_ROOT,
                env={**environ, "PORTKEY_TEST_EKSCTL_VERSION": "0.230.0"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(compatible.returncode, 0, compatible.stderr)

            external_reuse = subprocess.run(
                ["bash", str(script), "lbc-deploy"],
                cwd=REPO_ROOT,
                env={
                    **environ,
                    "CONFIRM_AWS_WRITE": "1",
                    "PORTKEY_TEST_EKSCTL_VERSION": "0.230.0",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(external_reuse.returncode, 0, external_reuse.stderr)
            self.assertIn(
                "Using an existing externally managed, ready",
                external_reuse.stdout,
            )
            external_commands = command_log.read_text(encoding="utf-8")
            self.assertNotIn("eksctl create", external_commands)
            self.assertNotIn("eksctl update", external_commands)
            self.assertNotIn(
                "eksctl utils associate-iam-oidc-provider",
                external_commands,
            )
            self.assertNotIn("eksctl delete", external_commands)
            self.assertNotIn("helm upgrade", external_commands)
            self.assertNotIn("helm uninstall", external_commands)
            for aws_mutation in (
                "cloudformation deploy",
                "cloudformation delete-stack",
                "iam create-",
                "iam update-",
                "iam put-",
                "iam attach-",
                "iam detach-",
                "iam delete-",
            ):
                self.assertNotIn(aws_mutation, external_commands)
            self.assertNotRegex(
                external_commands,
                r"(?m)^kubectl .* (?:apply|create|patch|replace|delete)(?: |$)",
            )

            wrong_namespace = subprocess.run(
                ["bash", str(script), "lbc-status"],
                cwd=REPO_ROOT,
                env={**environ, "PORTKEY_TEST_LBC_WATCH": "another-namespace"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(wrong_namespace.returncode, 0)
            self.assertIn("controller watches", wrong_namespace.stderr)

            wrong_image_version = subprocess.run(
                ["bash", str(script), "lbc-status"],
                cwd=REPO_ROOT,
                env={**environ, "PORTKEY_TEST_LBC_IMAGE_VERSION": "v3.3.0"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(wrong_image_version.returncode, 0)
            self.assertIn("expected image tag v3.4.2", wrong_image_version.stderr)
            self.assertIn(
                "labels cannot substitute",
                wrong_image_version.stderr,
            )

            for incompatible_flag in (
                "--feature-gates=NLBSecurityGroup=false",
                "--feature-gates=NLBSecurityGroup=0",
                "--feature-gates=NLBSecurityGroup=f",
                "--enable-backend-security-group=false",
                "--enable-backend-security-group=0",
                "--enable-backend-security-group=f",
                "--disable-restricted-sg-rules=true",
                "--disable-restricted-sg-rules=1",
                "--disable-restricted-sg-rules=t",
                "--watch-namespace=portkeyai",
            ):
                with self.subTest(incompatible_flag=incompatible_flag):
                    result = subprocess.run(
                        ["bash", str(script), "lbc-status"],
                        cwd=REPO_ROOT,
                        env={
                            **environ,
                            "PORTKEY_TEST_LBC_EXTRA_ARG": incompatible_flag,
                        },
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("compatibility check failed", result.stderr)

            empty_watch = subprocess.run(
                ["bash", str(script), "lbc-status"],
                cwd=REPO_ROOT,
                env={**environ, "PORTKEY_TEST_LBC_WATCH": ""},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(empty_watch.returncode, 0)
            self.assertIn("controller watches", empty_watch.stderr)

            for compatible_flag in (
                "--feature-gates=NLBSecurityGroup=1",
                "--feature-gates=NLBSecurityGroup=t",
                "--enable-backend-security-group=1",
                "--enable-backend-security-group=t",
                "--disable-restricted-sg-rules=0",
                "--disable-restricted-sg-rules=f",
            ):
                with self.subTest(compatible_flag=compatible_flag):
                    result = subprocess.run(
                        ["bash", str(script), "lbc-status"],
                        cwd=REPO_ROOT,
                        env={
                            **environ,
                            "PORTKEY_TEST_LBC_EXTRA_ARG": compatible_flag,
                        },
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)

            missing_remove = json.loads(json.dumps(current_policy))
            for statement in missing_remove["Statement"]:
                actions = statement.get("Action")
                if isinstance(actions, list) and (
                    "elasticloadbalancing:RemoveListenerCertificates" in actions
                ):
                    actions.remove(
                        "elasticloadbalancing:RemoveListenerCertificates"
                    )
            explicit_deny = json.loads(json.dumps(current_policy))
            explicit_deny["Statement"].append(
                {
                    "Effect": "Deny",
                    "Action": "elasticloadbalancing:RemoveListenerCertificates",
                    "Resource": "*",
                }
            )
            not_resource_deny = json.loads(json.dumps(current_policy))
            not_resource_deny["Statement"].append(
                {
                    "Effect": "Deny",
                    "Action": "elasticloadbalancing:*ListenerCertificates",
                    "NotResource": (
                        "arn:aws:elasticloadbalancing:us-east-1:123456789012:"
                        "listener/net/exempt/*"
                    ),
                }
            )
            for description, policy in (
                ("missing-remove", missing_remove),
                ("deny-remove", explicit_deny),
                ("deny-not-resource", not_resource_deny),
            ):
                with self.subTest(policy_mode=description):
                    policy_marker.write_text(json.dumps(policy), encoding="utf-8")
                    result = subprocess.run(
                        ["bash", str(script), "lbc-status"],
                        cwd=REPO_ROOT,
                        env=environ,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("cannot be proven TLS-capable", result.stderr)
            policy_marker.write_text(json.dumps(current_policy), encoding="utf-8")

    def test_portkey_lbc_deploy_runs_irsa_chart_and_readiness_flow(self):
        script = SCRIPTS_DIR / "portkey-stack.sh"
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            command_log = temp / "commands.log"
            helm_marker = temp / "fresh-helm-applied"
            policy_marker = temp / "policy.json"
            template_marker = temp / "template.json"
            current_policy = self.rendered_lbc_policy()
            policy_marker.write_text(json.dumps(current_policy), encoding="utf-8")
            template_marker.write_text(
                json.dumps(self.lbc_stack_template(current_policy)),
                encoding="utf-8",
            )

            aws = fake_bin / "aws"
            aws.write_text(
                """#!/usr/bin/env bash
printf 'aws %s\n' "$*" >>"$PORTKEY_TEST_COMMAND_LOG"
case "$*" in
  *"sts get-caller-identity"*"--query Account"*) printf '%s\n' '123456789012' ;;
  *"sts get-caller-identity"*) printf '%s\n' '{}' ;;
  *"cluster.identity.oidc.issuer"*) printf '%s\n' 'https://oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE' ;;
  *"cluster.resourcesVpcConfig.vpcId"*) printf '%s\n' 'vpc-0123456789abcdef0' ;;
  *"acm describe-certificate"*) printf '%s\n' '{"Status":"ISSUED","DomainName":"portkey.internal.example","SubjectAlternativeNames":["portkey.internal.example"]}' ;;
  *"ec2 describe-managed-prefix-lists"*) printf '%s\n' '{"PrefixLists":[{"PrefixListId":"pl-0123456789abcdef0","OwnerId":"123456789012","AddressFamily":"IPv4","State":"create-complete","MaxEntries":20}]}' ;;
  *"ec2 get-managed-prefix-list-entries"*) printf '%s\n' '{"Entries":[{"Cidr":"10.0.0.0/8"}]}' ;;
  *"cloudformation describe-stacks"*) printf '%s\n' '{"Stacks":[{"StackName":"eksctl-codex-portkey-addon-iamserviceaccount-kube-system-aws-load-balancer-controller","StackStatus":"CREATE_COMPLETE","Tags":[{"Key":"alpha.eksctl.io/cluster-name","Value":"codex-portkey"},{"Key":"alpha.eksctl.io/iamserviceaccount-name","Value":"kube-system/aws-load-balancer-controller"},{"Key":"Application","Value":"guidance-codex-portkey"}],"Outputs":[{"OutputKey":"Role1","OutputValue":"arn:aws:iam::123456789012:role/platform-lbc"}]}]}' ;;
  *"cloudformation get-template"*) cat "$PORTKEY_TEST_TEMPLATE_MARKER" ;;
  *"cloudformation describe-stack-resource"*) printf '%s\n' 'TLS' ;;
  *"iam get-role"*"PermissionsBoundaryArn"*) printf '%s\n' 'None' ;;
  *"iam get-role-policy"*) cat "$PORTKEY_TEST_POLICY_MARKER" ;;
  *"iam get-role"*) printf '%s\n' '{"Role":{"Arn":"arn:aws:iam::123456789012:role/platform-lbc","AssumeRolePolicyDocument":{"Statement":[{"Effect":"Allow","Action":"sts:AssumeRoleWithWebIdentity","Principal":{"Federated":"arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE"},"Condition":{"StringEquals":{"oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE:aud":"sts.amazonaws.com","oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE:sub":"system:serviceaccount:kube-system:aws-load-balancer-controller"}}}]}}}' ;;
  *"iam list-role-policies"*) printf '%s\n' '["TLS"]' ;;
  *"iam list-attached-role-policies"*) printf '%s\n' '[]' ;;
esac
exit 0
""",
                encoding="utf-8",
            )
            aws.chmod(0o700)

            eksctl = fake_bin / "eksctl"
            eksctl.write_text(
                """#!/usr/bin/env bash
if [[ "${1:-}" == version ]]; then printf '%s\n' "${PORTKEY_TEST_EKSCTL_VERSION:-0.229.0}"; exit 0; fi
printf 'eksctl %s\n' "$*" >>"$PORTKEY_TEST_COMMAND_LOG"
if [[ "${1:-} ${2:-}" == 'create iamserviceaccount' ]]; then
  previous=''
  for argument in "$@"; do
    if [[ "$previous" == '--config-file' ]]; then
      python3 -c 'import json,sys; payload=json.load(open(sys.argv[1])); print(json.dumps(payload["iam"]["serviceAccounts"][0]["attachPolicy"]))' "$argument" >"$PORTKEY_TEST_POLICY_MARKER"
    fi
    previous="$argument"
  done
fi
exit 0
""",
                encoding="utf-8",
            )
            eksctl.chmod(0o700)

            kubectl = fake_bin / "kubectl"
            kubectl.write_text(
                """#!/usr/bin/env bash
printf 'kubectl %s\n' "$*" >>"$PORTKEY_TEST_COMMAND_LOG"
case "$*" in
  *"release-namespace"*) [[ -f "$PORTKEY_TEST_HELM_MARKER" ]] && printf '%s' 'kube-system' ;;
  *"release-name"*) [[ -f "$PORTKEY_TEST_HELM_MARKER" ]] && printf '%s' 'aws-load-balancer-controller' ;;
  *"get deployment aws-load-balancer-controller -o json"*)
    [[ -f "$PORTKEY_TEST_HELM_MARKER" ]] && printf '%s' '{"metadata":{"labels":{"app.kubernetes.io/version":"v3.4.2"}},"spec":{"template":{"spec":{"serviceAccountName":"aws-load-balancer-controller","containers":[{"name":"aws-load-balancer-controller","image":"public.ecr.aws/eks/aws-load-balancer-controller:v3.4.2","args":["--cluster-name=codex-portkey","--aws-region=us-east-1","--aws-vpc-id=vpc-0123456789abcdef0","--watch-namespace=portkeyai","--feature-gates=ListenerRulesTagging=true","--enable-shield=false","--enable-waf=false","--enable-wafv2=false"]}]}}}}'
    ;;
  *"get deployment aws-load-balancer-controller"*) [[ -f "$PORTKEY_TEST_HELM_MARKER" ]] && printf '%s' 'deployment.apps/aws-load-balancer-controller' ;;
  *"get serviceaccount aws-load-balancer-controller"*"role-arn"*) printf '%s' 'arn:aws:iam::123456789012:role/platform-lbc' ;;
  *"get serviceaccount aws-load-balancer-controller"*"managed-by"*) printf '%s' 'platform-team' ;;
  *"get serviceaccount aws-load-balancer-controller"*) [[ "${PORTKEY_TEST_EXISTING_SA:-}" == 1 ]] && printf '%s' 'serviceaccount/aws-load-balancer-controller'; exit 0 ;;
  *"get services,ingresses.networking.k8s.io"*) printf '%s' '{"items":[]}' ;;
esac
exit 0
""",
                encoding="utf-8",
            )
            kubectl.chmod(0o700)

            helm = fake_bin / "helm"
            helm.write_text(
                """#!/usr/bin/env bash
printf 'helm %s\n' "$*" >>"$PORTKEY_TEST_COMMAND_LOG"
if [[ "${1:-}" == version && "${2:-}" == --short ]]; then printf '%s\n' 'v3.21.4+gtest'; exit 0; fi
if [[ "$*" == *"upgrade --install aws-load-balancer-controller"* ]]; then
  : >"$PORTKEY_TEST_HELM_MARKER"
fi
exit 0
""",
                encoding="utf-8",
            )
            helm.chmod(0o700)

            environ = {
                **os.environ,
                "AWS_REGION": "us-east-1",
                "CONFIRM_AWS_WRITE": "1",
                "PORTKEY_GATEWAY_HOSTNAME": "portkey.internal.example",
                "PORTKEY_NLB_TLS_CERTIFICATE_ARN": self.certificate_arn(),
                "PORTKEY_NLB_ALLOWED_PREFIX_LIST_IDS": "pl-0123456789abcdef0",
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                "PORTKEY_ENV_FILE": str(temp / "does-not-exist"),
                "PORTKEY_TEST_COMMAND_LOG": str(command_log),
                "PORTKEY_TEST_HELM_MARKER": str(helm_marker),
                "PORTKEY_TEST_POLICY_MARKER": str(policy_marker),
                "PORTKEY_TEST_TEMPLATE_MARKER": str(template_marker),
            }
            for unsupported_version in ("0.230.0", "0.229.0-rc.1"):
                with self.subTest(managed_create_eksctl=unsupported_version):
                    command_log.write_text("", encoding="utf-8")
                    rejected_version = subprocess.run(
                        ["bash", str(script), "lbc-deploy"],
                        cwd=REPO_ROOT,
                        env={
                            **environ,
                            "PORTKEY_TEST_EKSCTL_VERSION": unsupported_version,
                        },
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertNotEqual(rejected_version.returncode, 0)
                    self.assertIn(
                        "eksctl 0.229.0 exactly is required",
                        rejected_version.stderr,
                    )
                    rejected_commands = command_log.read_text(encoding="utf-8")
                    self.assertNotIn(
                        "eksctl utils associate-iam-oidc-provider",
                        rejected_commands,
                    )
                    self.assertNotIn(
                        "eksctl create iamserviceaccount", rejected_commands
                    )
                    self.assertNotIn("helm upgrade --install", rejected_commands)

            command_log.write_text("", encoding="utf-8")
            result = subprocess.run(
                ["bash", str(script), "lbc-deploy"],
                cwd=REPO_ROOT,
                env=environ,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            commands = command_log.read_text(encoding="utf-8")
            self.assertIn("eksctl utils associate-iam-oidc-provider", commands)
            self.assertIn("eksctl create iamserviceaccount", commands)
            self.assertIn("helm template aws-load-balancer-controller", commands)
            self.assertLess(
                commands.index("helm template aws-load-balancer-controller"),
                commands.index("eksctl create iamserviceaccount"),
            )
            self.assertIn("helm upgrade --install aws-load-balancer-controller", commands)
            self.assertIn("--version 3.4.2", commands)
            self.assertIn("--set vpcId=vpc-0123456789abcdef0", commands)
            self.assertIn("--set watchNamespace=portkeyai", commands)
            self.assertIn("--set enableServiceMutatorWebhook=false", commands)
            self.assertIn(
                "--set controllerConfig.featureGates.ListenerRulesTagging=true",
                commands,
            )
            self.assertIn(
                "kubectl -n kube-system rollout status "
                "deployment/aws-load-balancer-controller",
                commands,
            )
            self.assertIn(
                "kubectl get crd targetgroupbindings.elbv2.k8s.aws",
                commands,
            )

            helm_marker.unlink(missing_ok=True)
            command_log.write_text("", encoding="utf-8")
            unowned_service_account = subprocess.run(
                ["bash", str(script), "lbc-deploy"],
                cwd=REPO_ROOT,
                env={**environ, "PORTKEY_TEST_EXISTING_SA": "1"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(unowned_service_account.returncode, 0)
            self.assertIn("refusing to overwrite", unowned_service_account.stderr)
            commands = command_log.read_text(encoding="utf-8")
            self.assertNotIn("eksctl create iamserviceaccount", commands)
            self.assertNotIn("helm upgrade --install", commands)

    def test_portkey_lbc_deploy_repairs_an_unready_owned_release(self):
        script = SCRIPTS_DIR / "portkey-stack.sh"
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            command_log = temp / "commands.log"
            helm_marker = temp / "helm-applied"
            policy_marker = temp / "policy.json"
            template_marker = temp / "template.json"
            desired_template_marker = temp / "desired-template.json"
            current_policy = self.rendered_lbc_policy()
            policy_marker.write_text(json.dumps(current_policy), encoding="utf-8")
            template_marker.write_text(
                json.dumps(self.lbc_stack_template(current_policy)),
                encoding="utf-8",
            )
            desired_template_marker.write_text(
                json.dumps(self.lbc_stack_template(current_policy)),
                encoding="utf-8",
            )

            aws = fake_bin / "aws"
            aws.write_text(
                """#!/usr/bin/env bash
printf 'aws %s\n' "$*" >>"$PORTKEY_TEST_COMMAND_LOG"
case "$*" in
  *"sts get-caller-identity"*"--query Account"*) printf '%s\n' '123456789012' ;;
  *"sts get-caller-identity"*) printf '%s\n' '{}' ;;
  *"cluster.identity.oidc.issuer"*) printf '%s\n' 'https://oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE' ;;
  *"cluster.resourcesVpcConfig.vpcId"*) printf '%s\n' 'vpc-0123456789abcdef0' ;;
  *"acm describe-certificate"*) printf '%s\n' '{"Status":"ISSUED","DomainName":"portkey.internal.example","SubjectAlternativeNames":["portkey.internal.example"]}' ;;
  *"ec2 describe-managed-prefix-lists"*) printf '%s\n' '{"PrefixLists":[{"PrefixListId":"pl-0123456789abcdef0","OwnerId":"123456789012","AddressFamily":"IPv4","State":"create-complete","MaxEntries":20}]}' ;;
  *"ec2 get-managed-prefix-list-entries"*) printf '%s\n' '{"Entries":[{"Cidr":"10.0.0.0/8"}]}' ;;
  *"cloudformation describe-stacks"*"Application"*) printf '%s\n' 'guidance-codex-portkey' ;;
  *"cloudformation describe-stacks"*) printf '%s\n' '{"Stacks":[{"StackName":"eksctl-codex-portkey-addon-iamserviceaccount-kube-system-aws-load-balancer-controller","StackStatus":"UPDATE_COMPLETE","Tags":[{"Key":"alpha.eksctl.io/cluster-name","Value":"codex-portkey"},{"Key":"alpha.eksctl.io/iamserviceaccount-name","Value":"kube-system/aws-load-balancer-controller"},{"Key":"Application","Value":"guidance-codex-portkey"}],"Outputs":[{"OutputKey":"Role1","OutputValue":"arn:aws:iam::123456789012:role/lbc"}]}]}' ;;
  *"cloudformation get-template"*) cat "$PORTKEY_TEST_TEMPLATE_MARKER" ;;
  *"cloudformation describe-stack-resource"*) printf '%s\n' 'lbc-policy' ;;
  *"iam get-role"*"PermissionsBoundaryArn"*) printf '%s\n' 'None' ;;
  *"iam get-role-policy"*) cat "$PORTKEY_TEST_POLICY_MARKER" ;;
  *"iam get-role"*) printf '%s\n' '{"Role":{"Arn":"arn:aws:iam::123456789012:role/lbc","AssumeRolePolicyDocument":{"Statement":[{"Effect":"Allow","Action":"sts:AssumeRoleWithWebIdentity","Principal":{"Federated":"arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE"},"Condition":{"StringEquals":{"oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE:aud":"sts.amazonaws.com","oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE:sub":"system:serviceaccount:kube-system:aws-load-balancer-controller"}}}]}}}' ;;
  *"iam list-role-policies"*) printf '%s\n' '["lbc-policy"]' ;;
  *"iam list-attached-role-policies"*) printf '%s\n' '[]' ;;
esac
exit 0
""",
                encoding="utf-8",
            )
            aws.chmod(0o700)

            eksctl = fake_bin / "eksctl"
            eksctl.write_text(
                """#!/usr/bin/env bash
if [[ "${1:-}" == version ]]; then printf '%s\n' "${PORTKEY_TEST_EKSCTL_VERSION:-0.229.0}"; exit 0; fi
printf 'eksctl %s\n' "$*" >>"$PORTKEY_TEST_COMMAND_LOG"
if [[ "${1:-} ${2:-}" == 'update iamserviceaccount' ]]; then
  previous=''
  for argument in "$@"; do
    if [[ "$previous" == '--config-file' ]]; then
      python3 -c 'import json,sys; payload=json.load(open(sys.argv[1])); print(json.dumps(payload["iam"]["serviceAccounts"][0]["attachPolicy"]))' "$argument" >"$PORTKEY_TEST_POLICY_MARKER"
      cp "$PORTKEY_TEST_DESIRED_TEMPLATE_MARKER" "$PORTKEY_TEST_TEMPLATE_MARKER"
    fi
    previous="$argument"
  done
fi
exit 0
""",
                encoding="utf-8",
            )
            eksctl.chmod(0o700)

            kubectl = fake_bin / "kubectl"
            kubectl.write_text(
                """#!/usr/bin/env bash
printf 'kubectl %s\n' "$*" >>"$PORTKEY_TEST_COMMAND_LOG"
legacy_service='{"kind":"Service","metadata":{"name":"portkey-ai-gateway","namespace":"portkeyai","uid":"gateway-uid","annotations":{"service.beta.kubernetes.io/aws-load-balancer-type":"external","service.beta.kubernetes.io/aws-load-balancer-scheme":"internal","service.beta.kubernetes.io/aws-load-balancer-nlb-target-type":"ip"}},"spec":{"type":"LoadBalancer","ports":[{"name":"gateway","port":80,"protocol":"TCP","targetPort":"gateway"}]}}'
tls_service='{"kind":"Service","metadata":{"name":"portkey-ai-gateway","namespace":"portkeyai","uid":"gateway-uid","annotations":{"service.beta.kubernetes.io/aws-load-balancer-type":"external","service.beta.kubernetes.io/aws-load-balancer-scheme":"internal","service.beta.kubernetes.io/aws-load-balancer-ip-address-type":"ipv4","service.beta.kubernetes.io/aws-load-balancer-nlb-target-type":"ip","service.beta.kubernetes.io/aws-load-balancer-ssl-cert":"arn:aws:acm:us-east-1:123456789012:certificate/11111111-2222-3333-4444-555555555555","service.beta.kubernetes.io/aws-load-balancer-ssl-ports":"443","service.beta.kubernetes.io/aws-load-balancer-ssl-negotiation-policy":"ELBSecurityPolicy-TLS13-1-2-2021-06","service.beta.kubernetes.io/aws-load-balancer-backend-protocol":"tcp","service.beta.kubernetes.io/aws-load-balancer-security-group-prefix-lists":"pl-0123456789abcdef0","service.beta.kubernetes.io/aws-load-balancer-healthcheck-path":"/v1/health","service.beta.kubernetes.io/aws-load-balancer-healthcheck-protocol":"http","service.beta.kubernetes.io/aws-load-balancer-healthcheck-port":"8787","service.beta.kubernetes.io/aws-load-balancer-manage-backend-security-group-rules":"true"}},"spec":{"type":"LoadBalancer","ports":[{"name":"gateway","port":443,"protocol":"TCP","targetPort":"gateway"}]}}'
case "$*" in
  *"rollout status"*"--timeout=2m"*)
    [[ "${PORTKEY_TEST_READY_CONTROLLER:-}" == 1 ]] && exit 0
    exit 1
    ;;
  *"rollout status"*"--timeout=5m"*) exit 0 ;;
  *"release-namespace"*) printf '%s' 'kube-system' ;;
  *"release-name"*)
    if [[ "${PORTKEY_TEST_POST_BAD_OWNERSHIP:-}" == 1 && -f "$PORTKEY_TEST_HELM_MARKER" ]]; then
      printf '%s' 'platform-controller'
    else
      printf '%s' "${PORTKEY_TEST_RELEASE_NAME:-aws-load-balancer-controller}"
    fi
    ;;
  *"get deployment aws-load-balancer-controller -o json"*)
    if [[ "${PORTKEY_TEST_POST_BAD_REPOSITORY:-}" == 1 && -f "$PORTKEY_TEST_HELM_MARKER" ]]; then
      repository='example.invalid/controller'
    else
      repository="${PORTKEY_TEST_LBC_REPOSITORY:-public.ecr.aws/eks/aws-load-balancer-controller}"
    fi
    if [[ "${PORTKEY_TEST_BROAD_SCOPE:-}" == 1 && ! -f "$PORTKEY_TEST_HELM_MARKER" ]]; then
      args='["--cluster-name=codex-portkey","--aws-region=us-east-1","--aws-vpc-id=vpc-0123456789abcdef0","--feature-gates=ListenerRulesTagging=true","--enable-shield=false","--enable-waf=false","--enable-wafv2=false"]'
    else
      args='["--cluster-name=codex-portkey","--aws-region=us-east-1","--aws-vpc-id=vpc-0123456789abcdef0","--watch-namespace=portkeyai","--feature-gates=ListenerRulesTagging=true","--enable-shield=false","--enable-waf=false","--enable-wafv2=false"]'
    fi
    printf '{"metadata":{"labels":{"app.kubernetes.io/version":"v3.4.2"}},"spec":{"template":{"spec":{"serviceAccountName":"aws-load-balancer-controller","containers":[{"name":"aws-load-balancer-controller","image":"%s:v3.4.2","args":%s}]}}}}' "$repository" "$args"
    ;;
  *"get deployment aws-load-balancer-controller"*) printf '%s' 'deployment.apps/aws-load-balancer-controller' ;;
  *"get serviceaccount aws-load-balancer-controller"*"role-arn"*) printf '%s' 'arn:aws:iam::123456789012:role/lbc' ;;
  *"get serviceaccount aws-load-balancer-controller"*"managed-by"*) printf '%s' "${PORTKEY_TEST_MANAGED_BY:-eksctl}" ;;
  *"get serviceaccount aws-load-balancer-controller"*) printf '%s' 'serviceaccount/aws-load-balancer-controller' ;;
  *"get service portkey-ai-gateway"*"--ignore-not-found -o name"*)
    if [[ "${PORTKEY_TEST_LEGACY_GATEWAY:-}" == 1 || "${PORTKEY_TEST_CURRENT_TLS_GATEWAY:-}" == 1 ]]; then
      printf '%s' 'service/portkey-ai-gateway'
    fi
    ;;
  *"get service portkey-ai-gateway"*"-o jsonpath="*) printf '%s' 'gateway-uid' ;;
  *"get service portkey-ai-gateway -o json"*)
    if [[ "${PORTKEY_TEST_LEGACY_GATEWAY:-}" == 1 ]]; then
      printf '%s' "$legacy_service"
    elif [[ "${PORTKEY_TEST_CURRENT_TLS_GATEWAY:-}" == 1 ]]; then
      printf '%s' "$tls_service"
    fi
    ;;
  *"get services,ingresses.networking.k8s.io"*)
    if [[ "${PORTKEY_TEST_UNRELATED_LB:-}" == 1 ]]; then
      printf '%s' '{"items":[{"kind":"Service","metadata":{"name":"another-nlb","namespace":"another-namespace"},"spec":{"type":"LoadBalancer"}}]}'
    elif [[ "${PORTKEY_TEST_UNRELATED_INGRESS:-}" == 1 ]]; then
      printf '%s' '{"items":[{"kind":"Ingress","metadata":{"name":"another-ingress","namespace":"another-namespace"}}]}'
    elif [[ "${PORTKEY_TEST_LEGACY_GATEWAY:-}" == 1 ]]; then
      printf '%s' '{"items":['"$legacy_service"']}'
    elif [[ "${PORTKEY_TEST_CURRENT_TLS_GATEWAY:-}" == 1 ]]; then
      printf '%s' '{"items":['"$tls_service"']}'
    else
      printf '%s' '{"items":[]}'
    fi
    ;;
  *"get crd targetgroupbindings.elbv2.k8s.aws"*)
    [[ "${PORTKEY_TEST_TGB_CRD_ERROR:-}" == 1 ]] && exit 23
    printf '%s' 'customresourcedefinition.apiextensions.k8s.io/targetgroupbindings.elbv2.k8s.aws'
    ;;
  *"get targetgroupbindings.elbv2.k8s.aws"*)
    [[ "${PORTKEY_TEST_TGB_LIST_ERROR:-}" == 1 ]] && exit 24
    if [[ "${PORTKEY_TEST_WRONG_PORT_TGB:-}" == 1 ]]; then
      printf '%s' '{"items":[{"kind":"TargetGroupBinding","metadata":{"name":"portkey-ai-gateway-wrong-port","namespace":"portkeyai","labels":{"service.k8s.aws/stack-namespace":"portkeyai","service.k8s.aws/stack-name":"portkey-ai-gateway"}},"spec":{"serviceRef":{"name":"portkey-ai-gateway","port":"admin"}}}]}'
    elif [[ "${PORTKEY_TEST_DUPLICATE_TGB:-}" == 1 ]]; then
      printf '%s' '{"items":[{"kind":"TargetGroupBinding","metadata":{"name":"portkey-ai-gateway-one","namespace":"portkeyai","labels":{"service.k8s.aws/stack-namespace":"portkeyai","service.k8s.aws/stack-name":"portkey-ai-gateway"}},"spec":{"serviceRef":{"name":"portkey-ai-gateway","port":443}}},{"kind":"TargetGroupBinding","metadata":{"name":"portkey-ai-gateway-two","namespace":"portkeyai","labels":{"service.k8s.aws/stack-namespace":"portkeyai","service.k8s.aws/stack-name":"portkey-ai-gateway"}},"spec":{"serviceRef":{"name":"portkey-ai-gateway","port":443}}}]}'
    elif [[ "${PORTKEY_TEST_UNRELATED_TGB:-}" == 1 ]]; then
      printf '%s' '{"items":[{"kind":"TargetGroupBinding","metadata":{"name":"another-binding","namespace":"another-namespace","labels":{"service.k8s.aws/stack-namespace":"another-namespace","service.k8s.aws/stack-name":"another-service"}},"spec":{"serviceRef":{"name":"another-service","port":"gateway"}}}]}'
    elif [[ "${PORTKEY_TEST_LEGACY_GATEWAY:-}" == 1 || "${PORTKEY_TEST_CURRENT_TLS_GATEWAY:-}" == 1 ]]; then
      printf '%s' '{"items":[{"kind":"TargetGroupBinding","metadata":{"name":"portkey-ai-gateway","namespace":"portkeyai","labels":{"service.k8s.aws/stack-namespace":"portkeyai","service.k8s.aws/stack-name":"portkey-ai-gateway"}},"spec":{"serviceRef":{"name":"portkey-ai-gateway","port":443}}}]}'
    else
      printf '%s' '{"items":[]}'
    fi
    ;;
  *"api-resources"*"gateway.networking.k8s.io"*)
    [[ "${PORTKEY_TEST_UNRELATED_GATEWAY:-}" == 1 ]] && printf '%s' 'gateways.gateway.networking.k8s.io'
    ;;
  *"get gateways.gateway.networking.k8s.io"*)
    [[ "${PORTKEY_TEST_UNRELATED_GATEWAY:-}" == 1 ]] && printf '%s' 'gateway.gateway.networking.k8s.io/another-gateway'
    ;;
esac
exit 0
""",
                encoding="utf-8",
            )
            kubectl.chmod(0o700)

            helm = fake_bin / "helm"
            helm.write_text(
                """#!/usr/bin/env bash
printf 'helm %s\n' "$*" >>"$PORTKEY_TEST_COMMAND_LOG"
if [[ "${1:-}" == version && "${2:-}" == --short ]]; then printf '%s\n' 'v3.21.4+gtest'; exit 0; fi
if [[ "$*" == *"upgrade --install aws-load-balancer-controller"* ]]; then
  : >"$PORTKEY_TEST_HELM_MARKER"
fi
exit 0
""",
                encoding="utf-8",
            )
            helm.chmod(0o700)

            environ = {
                **os.environ,
                "AWS_REGION": "us-east-1",
                "CONFIRM_AWS_WRITE": "1",
                "PORTKEY_GATEWAY_HOSTNAME": "portkey.internal.example",
                "PORTKEY_NLB_TLS_CERTIFICATE_ARN": self.certificate_arn(),
                "PORTKEY_NLB_ALLOWED_PREFIX_LIST_IDS": "pl-0123456789abcdef0",
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                "PORTKEY_ENV_FILE": str(temp / "does-not-exist"),
                "PORTKEY_TEST_COMMAND_LOG": str(command_log),
                "PORTKEY_TEST_DESIRED_TEMPLATE_MARKER": str(
                    desired_template_marker
                ),
                "PORTKEY_TEST_HELM_MARKER": str(helm_marker),
                "PORTKEY_TEST_POLICY_MARKER": str(policy_marker),
                "PORTKEY_TEST_TEMPLATE_MARKER": str(template_marker),
            }
            result = subprocess.run(
                ["bash", str(script), "lbc-deploy"],
                cwd=REPO_ROOT,
                env={**environ, "PORTKEY_TEST_CURRENT_TLS_GATEWAY": "1"},
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(
                result.returncode,
                0,
                result.stderr + command_log.read_text(encoding="utf-8"),
            )
            self.assertIn("Retrying the existing walkthrough-managed", result.stdout)
            commands = command_log.read_text(encoding="utf-8")
            self.assertIn("helm status aws-load-balancer-controller", commands)
            self.assertNotIn("eksctl update iamserviceaccount", commands)
            self.assertIn("helm upgrade --install aws-load-balancer-controller", commands)
            self.assertNotIn("eksctl create iamserviceaccount", commands)

            command_log.write_text("", encoding="utf-8")
            repeated = subprocess.run(
                ["bash", str(script), "lbc-deploy"],
                cwd=REPO_ROOT,
                env={**environ, "PORTKEY_TEST_CURRENT_TLS_GATEWAY": "1"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            repeated_commands = command_log.read_text(encoding="utf-8")
            self.assertNotIn("eksctl update iamserviceaccount", repeated_commands)
            self.assertNotIn("eksctl create iamserviceaccount", repeated_commands)
            self.assertIn(
                "helm upgrade --install aws-load-balancer-controller",
                repeated_commands,
            )

            post_install_cases = (
                (
                    {"PORTKEY_TEST_POST_BAD_OWNERSHIP": "1"},
                    "resulting Deployment is not owned by the expected Helm release",
                ),
                (
                    {"PORTKEY_TEST_POST_BAD_REPOSITORY": "1"},
                    "resulting Deployment does not match the reviewed image",
                ),
            )
            for overrides, expected_error in post_install_cases:
                with self.subTest(post_install_controller=overrides):
                    helm_marker.unlink(missing_ok=True)
                    command_log.write_text("", encoding="utf-8")
                    post_install_failure = subprocess.run(
                        ["bash", str(script), "lbc-deploy"],
                        cwd=REPO_ROOT,
                        env={
                            **environ,
                            "PORTKEY_TEST_CURRENT_TLS_GATEWAY": "1",
                            **overrides,
                        },
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertNotEqual(post_install_failure.returncode, 0)
                    self.assertIn(expected_error, post_install_failure.stderr)
                    post_install_commands = command_log.read_text(
                        encoding="utf-8"
                    )
                    self.assertIn(
                        "helm upgrade --install aws-load-balancer-controller",
                        post_install_commands,
                    )
                    self.assertNotIn(
                        "eksctl update iamserviceaccount",
                        post_install_commands,
                    )
            helm_marker.unlink(missing_ok=True)

            command_log.write_text("", encoding="utf-8")
            ready_managed = subprocess.run(
                ["bash", str(script), "lbc-deploy"],
                cwd=REPO_ROOT,
                env={**environ, "PORTKEY_TEST_READY_CONTROLLER": "1"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(ready_managed.returncode, 0, ready_managed.stderr)
            ready_commands = command_log.read_text(encoding="utf-8")
            self.assertNotIn("eksctl update iamserviceaccount", ready_commands)
            self.assertNotIn("helm upgrade --install", ready_commands)

            ready_drift_cases = (
                (
                    {"PORTKEY_TEST_RELEASE_NAME": "platform-controller"},
                    "not owned by the expected Helm release",
                ),
                (
                    {"PORTKEY_TEST_LBC_REPOSITORY": "example.invalid/controller"},
                    "unexpected image repository",
                ),
                (
                    {"PORTKEY_TEST_BROAD_SCOPE": "1"},
                    "cleanup requires --watch-namespace=portkeyai",
                ),
            )
            for overrides, expected_error in ready_drift_cases:
                with self.subTest(ready_managed_drift=overrides):
                    command_log.write_text("", encoding="utf-8")
                    rejected_ready = subprocess.run(
                        ["bash", str(script), "lbc-deploy"],
                        cwd=REPO_ROOT,
                        env={
                            **environ,
                            "PORTKEY_TEST_READY_CONTROLLER": "1",
                            **overrides,
                        },
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertNotEqual(rejected_ready.returncode, 0)
                    self.assertIn(expected_error, rejected_ready.stderr)
                    rejected_ready_commands = command_log.read_text(
                        encoding="utf-8"
                    )
                    self.assertNotIn(
                        "eksctl update iamserviceaccount",
                        rejected_ready_commands,
                    )
                    self.assertNotIn(
                        "helm upgrade --install", rejected_ready_commands
                    )

            unready_dependency_cases = (
                (
                    {"PORTKEY_TEST_UNRELATED_LB": "1"},
                    "another Ingress or LoadBalancer Service",
                ),
                (
                    {"PORTKEY_TEST_UNRELATED_INGRESS": "1"},
                    "another Ingress or LoadBalancer Service",
                ),
                (
                    {"PORTKEY_TEST_UNRELATED_TGB": "1"},
                    "stale or unrelated TargetGroupBinding",
                ),
                (
                    {"PORTKEY_TEST_UNRELATED_GATEWAY": "1"},
                    "Gateway API exposure exists",
                ),
                (
                    {
                        "PORTKEY_TEST_CURRENT_TLS_GATEWAY": "1",
                        "PORTKEY_TEST_WRONG_PORT_TGB": "1",
                    },
                    "stale or unrelated TargetGroupBinding",
                ),
                (
                    {
                        "PORTKEY_TEST_CURRENT_TLS_GATEWAY": "1",
                        "PORTKEY_TEST_DUPLICATE_TGB": "1",
                    },
                    "stale or unrelated TargetGroupBinding",
                ),
            )
            for overrides, expected_error in unready_dependency_cases:
                with self.subTest(unready_dependency=overrides):
                    command_log.write_text("", encoding="utf-8")
                    blocked_dependency = subprocess.run(
                        ["bash", str(script), "lbc-deploy"],
                        cwd=REPO_ROOT,
                        env={
                            **environ,
                            "PORTKEY_TEST_BROAD_SCOPE": "1",
                            **overrides,
                        },
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertNotEqual(blocked_dependency.returncode, 0)
                    self.assertIn(expected_error, blocked_dependency.stderr)
                    dependency_commands = command_log.read_text(encoding="utf-8")
                    self.assertNotIn(
                        "eksctl update iamserviceaccount", dependency_commands
                    )
                    self.assertNotIn("helm upgrade --install", dependency_commands)

            for failure_flag, expected_error in (
                (
                    "PORTKEY_TEST_TGB_CRD_ERROR",
                    "could not determine whether TargetGroupBinding resources exist",
                ),
                (
                    "PORTKEY_TEST_TGB_LIST_ERROR",
                    "could not inspect TargetGroupBinding dependents",
                ),
            ):
                with self.subTest(tgb_discovery_failure=failure_flag):
                    command_log.write_text("", encoding="utf-8")
                    discovery_failure = subprocess.run(
                        ["bash", str(script), "lbc-deploy"],
                        cwd=REPO_ROOT,
                        env={
                            **environ,
                            "PORTKEY_TEST_BROAD_SCOPE": "1",
                            failure_flag: "1",
                        },
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertNotEqual(discovery_failure.returncode, 0)
                    self.assertIn(expected_error, discovery_failure.stderr)
                    discovery_commands = command_log.read_text(encoding="utf-8")
                    self.assertNotIn(
                        "eksctl update iamserviceaccount", discovery_commands
                    )
                    self.assertNotIn("helm upgrade --install", discovery_commands)

            custom_role_template = self.lbc_stack_template(current_policy)
            custom_role_template["TemplateBody"]["Resources"]["Role1"][
                "Properties"
            ]["Path"] = "/custom/"
            template_marker.write_text(
                json.dumps(custom_role_template), encoding="utf-8"
            )
            command_log.write_text("", encoding="utf-8")
            custom_role_result = subprocess.run(
                ["bash", str(script), "lbc-deploy"],
                cwd=REPO_ROOT,
                env={**environ, "PORTKEY_TEST_BROAD_SCOPE": "1"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(custom_role_result.returncode, 0)
            self.assertIn(
                "controller IAM stack validation failed",
                custom_role_result.stderr,
            )
            custom_role_commands = command_log.read_text(encoding="utf-8")
            self.assertNotIn(
                "eksctl update iamserviceaccount", custom_role_commands
            )
            self.assertNotIn("helm upgrade --install", custom_role_commands)
            template_marker.write_text(
                json.dumps(self.lbc_stack_template(current_policy)),
                encoding="utf-8",
            )

            custom_policy = json.loads(json.dumps(current_policy))
            network_reads = next(
                statement
                for statement in custom_policy["Statement"]
                if statement.get("Sid")
                == "ReadNetworkLoadBalancerStateInDeploymentRegion"
            )
            network_reads["Action"].remove(
                "elasticloadbalancing:DescribeTargetHealth"
            )
            policy_marker.write_text(json.dumps(custom_policy), encoding="utf-8")
            template_marker.write_text(
                json.dumps(self.lbc_stack_template(custom_policy)),
                encoding="utf-8",
            )
            command_log.write_text("", encoding="utf-8")
            custom_policy_result = subprocess.run(
                ["bash", str(script), "lbc-deploy"],
                cwd=REPO_ROOT,
                env={**environ, "PORTKEY_TEST_BROAD_SCOPE": "1"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(custom_policy_result.returncode, 0)
            self.assertIn(
                "neither the exact reviewed legacy TCP policy nor the desired TLS policy",
                custom_policy_result.stderr,
            )
            custom_commands = command_log.read_text(encoding="utf-8")
            self.assertEqual(
                custom_commands.count(
                    "kubectl get services,ingresses.networking.k8s.io"
                ),
                1,
            )
            self.assertNotIn("eksctl update iamserviceaccount", custom_commands)
            self.assertNotIn("helm upgrade --install", custom_commands)

            old_tcp_policy = self.legacy_lbc_policy()
            policy_marker.write_text(json.dumps(old_tcp_policy), encoding="utf-8")
            template_marker.write_text(
                json.dumps(self.lbc_stack_template(old_tcp_policy)),
                encoding="utf-8",
            )
            command_log.write_text("", encoding="utf-8")
            legacy_gateway = subprocess.run(
                ["bash", str(script), "lbc-deploy"],
                cwd=REPO_ROOT,
                env={**environ, "PORTKEY_TEST_LEGACY_GATEWAY": "1"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(legacy_gateway.returncode, 0)
            self.assertIn(
                "existing Portkey exposure cannot be updated safely in place",
                legacy_gateway.stderr,
            )
            legacy_commands = command_log.read_text(encoding="utf-8")
            self.assertNotIn("eksctl update iamserviceaccount", legacy_commands)
            self.assertNotIn("helm upgrade --install", legacy_commands)

            for unsupported_version in ("0.230.0", "0.229.0-rc.1"):
                with self.subTest(managed_update_eksctl=unsupported_version):
                    command_log.write_text("", encoding="utf-8")
                    rejected_version = subprocess.run(
                        ["bash", str(script), "lbc-deploy"],
                        cwd=REPO_ROOT,
                        env={
                            **environ,
                            "PORTKEY_TEST_BROAD_SCOPE": "1",
                            "PORTKEY_TEST_EKSCTL_VERSION": unsupported_version,
                        },
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertNotEqual(rejected_version.returncode, 0)
                    self.assertIn(
                        "eksctl 0.229.0 exactly is required",
                        rejected_version.stderr,
                    )
                    rejected_commands = command_log.read_text(encoding="utf-8")
                    self.assertIn(
                        "kubectl get services,ingresses.networking.k8s.io "
                        "--all-namespaces",
                        rejected_commands,
                    )
                    self.assertNotIn(
                        "eksctl update iamserviceaccount", rejected_commands
                    )
                    self.assertNotIn("helm upgrade --install", rejected_commands)

            command_log.write_text("", encoding="utf-8")
            zero_dependency_migration = subprocess.run(
                ["bash", str(script), "lbc-deploy"],
                cwd=REPO_ROOT,
                env={**environ, "PORTKEY_TEST_BROAD_SCOPE": "1"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                zero_dependency_migration.returncode,
                0,
                zero_dependency_migration.stderr,
            )
            migration_commands = command_log.read_text(encoding="utf-8")
            self.assertIn(
                "kubectl get services,ingresses.networking.k8s.io "
                "--all-namespaces --ignore-not-found -o json",
                migration_commands,
            )
            self.assertIn(
                "kubectl get targetgroupbindings.elbv2.k8s.aws "
                "--all-namespaces --ignore-not-found -o json",
                migration_commands,
            )
            self.assertNotIn("kubectl --all-namespaces get", migration_commands)
            self.assertLess(
                migration_commands.index("kubectl get services,ingresses"),
                migration_commands.index(
                    "helm template aws-load-balancer-controller"
                ),
            )
            self.assertLess(
                migration_commands.index(
                    "helm template aws-load-balancer-controller"
                ),
                migration_commands.index("eksctl update iamserviceaccount"),
            )
            self.assertLess(
                migration_commands.index("eksctl update iamserviceaccount"),
                migration_commands.index(
                    "helm upgrade --install aws-load-balancer-controller"
                ),
            )

            policy_marker.write_text(json.dumps(current_policy), encoding="utf-8")
            template_marker.write_text(
                json.dumps(self.lbc_stack_template(current_policy)),
                encoding="utf-8",
            )

            command_log.write_text("", encoding="utf-8")
            policy_marker.write_text(
                json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [],
                    }
                ),
                encoding="utf-8",
            )
            drifted_policy = subprocess.run(
                ["bash", str(script), "lbc-deploy"],
                cwd=REPO_ROOT,
                env=environ,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(drifted_policy.returncode, 0)
            self.assertIn("has drifted from its CloudFormation stack", drifted_policy.stderr)
            drift_commands = command_log.read_text(encoding="utf-8")
            self.assertNotIn("eksctl update iamserviceaccount", drift_commands)
            self.assertNotIn("helm upgrade --install", drift_commands)
            policy_marker.write_text(json.dumps(current_policy), encoding="utf-8")

            command_log.write_text("", encoding="utf-8")
            unmanaged = subprocess.run(
                ["bash", str(script), "lbc-deploy"],
                cwd=REPO_ROOT,
                env={**environ, "PORTKEY_TEST_MANAGED_BY": "platform-team"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(unmanaged.returncode, 0)
            self.assertIn("not owned by this walkthrough", unmanaged.stderr)
            self.assertNotIn(
                "helm upgrade --install",
                command_log.read_text(encoding="utf-8"),
            )

    def test_portkey_lbc_cleanup_is_owned_confirmed_and_dependency_safe(self):
        script = SCRIPTS_DIR / "portkey-stack.sh"
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            command_log = temp / "commands.log"
            stack_deleted_marker = temp / "stack-deleted"
            policy_marker = temp / "policy.json"
            template_marker = temp / "template.json"
            current_policy = self.rendered_lbc_policy()
            policy_marker.write_text(json.dumps(current_policy), encoding="utf-8")
            template_marker.write_text(
                json.dumps(self.lbc_stack_template(current_policy)),
                encoding="utf-8",
            )

            aws = fake_bin / "aws"
            aws.write_text(
                """#!/usr/bin/env bash
printf 'aws %s\n' "$*" >>"$PORTKEY_TEST_COMMAND_LOG"
case "$*" in
  *"sts get-caller-identity"*"--query Account"*) printf '%s\n' '123456789012' ;;
  *"sts get-caller-identity"*) printf '%s\n' '{}' ;;
  *"cluster.identity.oidc.issuer"*) printf '%s\n' 'https://oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE' ;;
  *"cluster.resourcesVpcConfig.vpcId"*) printf '%s\n' 'vpc-0123456789abcdef0' ;;
  *"cloudformation describe-stacks"*"Stacks[0].StackName"*)
    if [[ "${PORTKEY_TEST_STACK_ABSENT:-}" == 1 || -f "$PORTKEY_TEST_STACK_DELETED_MARKER" ]]; then
      printf '%s\n' 'An error occurred (ValidationError) when calling the DescribeStacks operation: Stack does not exist' >&2
      exit 255
    fi
    printf '%s\n' 'eksctl-codex-portkey-addon-iamserviceaccount-kube-system-aws-load-balancer-controller'
    ;;
  *"cloudformation describe-stacks"*"Application"*) printf '%s\n' 'guidance-codex-portkey' ;;
  *"cloudformation describe-stacks"*) printf '%s\n' '{"Stacks":[{"StackName":"eksctl-codex-portkey-addon-iamserviceaccount-kube-system-aws-load-balancer-controller","StackStatus":"UPDATE_COMPLETE","Tags":[{"Key":"alpha.eksctl.io/cluster-name","Value":"codex-portkey"},{"Key":"alpha.eksctl.io/iamserviceaccount-name","Value":"kube-system/aws-load-balancer-controller"},{"Key":"Application","Value":"guidance-codex-portkey"}],"Outputs":[{"OutputKey":"Role1","OutputValue":"arn:aws:iam::123456789012:role/lbc"}]}]}' ;;
  *"cloudformation get-template"*) cat "$PORTKEY_TEST_TEMPLATE_MARKER" ;;
  *"cloudformation describe-stack-resource"*) printf '%s\n' 'lbc-policy' ;;
  *"iam get-role-policy"*) cat "$PORTKEY_TEST_POLICY_MARKER" ;;
  *"iam get-role"*) printf '%s\n' '{"Role":{"Arn":"arn:aws:iam::123456789012:role/lbc","AssumeRolePolicyDocument":{"Statement":[{"Effect":"Allow","Action":"sts:AssumeRoleWithWebIdentity","Principal":{"Federated":"arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE"},"Condition":{"StringEquals":{"oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE:aud":"sts.amazonaws.com","oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE:sub":"system:serviceaccount:kube-system:aws-load-balancer-controller"}}}]}}}' ;;
  *"iam list-role-policies"*) printf '%s\n' '["lbc-policy"]' ;;
  *"iam list-attached-role-policies"*) printf '%s\n' '[]' ;;
esac
exit 0
""",
                encoding="utf-8",
            )
            aws.chmod(0o700)

            eksctl = fake_bin / "eksctl"
            eksctl.write_text(
                """#!/usr/bin/env bash
if [[ "${1:-}" == version ]]; then printf '%s\n' "${PORTKEY_TEST_EKSCTL_VERSION:-0.229.0}"; exit 0; fi
printf 'eksctl %s\n' "$*" >>"$PORTKEY_TEST_COMMAND_LOG"
if [[ "${1:-} ${2:-}" == 'delete iamserviceaccount' ]]; then
  : >"$PORTKEY_TEST_STACK_DELETED_MARKER"
fi
exit 0
""",
                encoding="utf-8",
            )
            eksctl.chmod(0o700)

            kubectl = fake_bin / "kubectl"
            kubectl.write_text(
                """#!/usr/bin/env bash
printf 'kubectl %s\n' "$*" >>"$PORTKEY_TEST_COMMAND_LOG"
case "$*" in
  *"release-namespace"*) [[ "${PORTKEY_TEST_RELEASE_ABSENT:-}" == 1 ]] || printf '%s' 'kube-system' ;;
  *"release-name"*) [[ "${PORTKEY_TEST_RELEASE_ABSENT:-}" == 1 ]] || printf '%s' 'aws-load-balancer-controller' ;;
  *"get deployment aws-load-balancer-controller -o json"*) printf '%s' '{"metadata":{"labels":{"app.kubernetes.io/version":"v3.4.2"}},"spec":{"template":{"spec":{"serviceAccountName":"aws-load-balancer-controller","containers":[{"name":"aws-load-balancer-controller","image":"public.ecr.aws/eks/aws-load-balancer-controller:v3.4.2","args":["--cluster-name=codex-portkey","--aws-region=us-east-1","--aws-vpc-id=vpc-0123456789abcdef0","--watch-namespace=portkeyai","--feature-gates=ListenerRulesTagging=true","--enable-shield=false","--enable-waf=false","--enable-wafv2=false"]}]}}}}' ;;
  *"get deployment aws-load-balancer-controller"*) [[ "${PORTKEY_TEST_DEPLOYMENT_ABSENT:-}" == 1 ]] || printf '%s' 'deployment.apps/aws-load-balancer-controller' ;;
  *"get serviceaccount aws-load-balancer-controller"*"managed-by"*) printf '%s' 'eksctl' ;;
  *"get serviceaccount aws-load-balancer-controller"*"role-arn"*) printf '%s' 'arn:aws:iam::123456789012:role/lbc' ;;
  *"get serviceaccount aws-load-balancer-controller"*) [[ "${PORTKEY_TEST_SA_ABSENT:-}" == 1 ]] || printf '%s' 'serviceaccount/aws-load-balancer-controller'; exit 0 ;;
  *"get crd targetgroupbindings.elbv2.k8s.aws"*) [[ "${PORTKEY_TEST_CRD_UNREADABLE:-}" == 1 ]] && exit 1; exit 0 ;;
  *"get targetgroupbindings.elbv2.k8s.aws"*) printf '%s\n' "${PORTKEY_TEST_BINDING:-}" ;;
  *"get services,ingresses.networking.k8s.io"*)
    if [[ "${PORTKEY_TEST_LB_SERVICE:-}" == 1 ]]; then
      printf '%s' '{"items":[{"kind":"Service","metadata":{"name":"pending-nlb"},"spec":{"type":"LoadBalancer"}}]}'
    else
      printf '%s' '{"items":[]}'
    fi
    ;;
  *"api-resources"*"gateway.networking.k8s.io"*) [[ "${PORTKEY_TEST_GATEWAY:-}" == 1 ]] && printf '%s\n' 'gateways.gateway.networking.k8s.io' ;;
  *"get gateways.gateway.networking.k8s.io"*) printf '%s\n' "${PORTKEY_TEST_GATEWAY_NAME:-}" ;;
esac
exit 0
""",
                encoding="utf-8",
            )
            kubectl.chmod(0o700)

            helm = fake_bin / "helm"
            helm.write_text(
                """#!/usr/bin/env bash
printf 'helm %s\n' "$*" >>"$PORTKEY_TEST_COMMAND_LOG"
if [[ "${1:-}" == version && "${2:-}" == --short ]]; then printf '%s\n' 'v3.21.4+gtest'; exit 0; fi
if [[ "${1:-}" == status && "${PORTKEY_TEST_RELEASE_ABSENT:-}" == 1 ]]; then
  exit 1
fi
exit 0
""",
                encoding="utf-8",
            )
            helm.chmod(0o700)

            base_environment = {
                **os.environ,
                "AWS_REGION": "us-east-1",
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                "PORTKEY_ENV_FILE": str(temp / "does-not-exist"),
                "PORTKEY_TEST_COMMAND_LOG": str(command_log),
                "PORTKEY_TEST_POLICY_MARKER": str(policy_marker),
                "PORTKEY_TEST_STACK_DELETED_MARKER": str(stack_deleted_marker),
                "PORTKEY_TEST_TEMPLATE_MARKER": str(template_marker),
            }

            def assert_no_cleanup_mutations():
                commands = command_log.read_text(encoding="utf-8")
                self.assertNotIn("helm uninstall", commands)
                self.assertNotIn("eksctl delete iamserviceaccount", commands)
                self.assertNotIn("cloudformation delete-stack", commands)
                self.assertNotRegex(
                    commands,
                    r"(?m)^kubectl .* (?:delete|patch|replace|apply|create)(?: |$)",
                )

            orphaned_stack = subprocess.run(
                ["bash", str(script), "lbc-cleanup"],
                cwd=REPO_ROOT,
                env={
                    **base_environment,
                    "CONFIRM_LBC_DELETE": "codex-portkey",
                    "PORTKEY_TEST_DEPLOYMENT_ABSENT": "1",
                    "PORTKEY_TEST_RELEASE_ABSENT": "1",
                    "PORTKEY_TEST_SA_ABSENT": "1",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(orphaned_stack.returncode, 0)
            self.assertIn("IAM stack still exists", orphaned_stack.stderr)
            assert_no_cleanup_mutations()

            command_log.write_text("", encoding="utf-8")
            blocked = subprocess.run(
                ["bash", str(script), "lbc-cleanup"],
                cwd=REPO_ROOT,
                env={
                    **base_environment,
                    "CONFIRM_LBC_DELETE": "codex-portkey",
                    "PORTKEY_TEST_BINDING": "targetgroupbinding.elbv2.k8s.aws/shared",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("TargetGroupBinding dependencies", blocked.stderr)
            assert_no_cleanup_mutations()

            command_log.write_text("", encoding="utf-8")
            unreadable_dependencies = subprocess.run(
                ["bash", str(script), "lbc-cleanup"],
                cwd=REPO_ROOT,
                env={
                    **base_environment,
                    "CONFIRM_LBC_DELETE": "codex-portkey",
                    "PORTKEY_TEST_CRD_UNREADABLE": "1",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(unreadable_dependencies.returncode, 0)
            self.assertIn("cannot prove controller cleanup is safe", unreadable_dependencies.stderr)
            assert_no_cleanup_mutations()

            command_log.write_text("", encoding="utf-8")
            load_balancer_service = subprocess.run(
                ["bash", str(script), "lbc-cleanup"],
                cwd=REPO_ROOT,
                env={
                    **base_environment,
                    "CONFIRM_LBC_DELETE": "codex-portkey",
                    "PORTKEY_TEST_LB_SERVICE": "1",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(load_balancer_service.returncode, 0)
            self.assertIn("controller dependencies remain", load_balancer_service.stderr)
            assert_no_cleanup_mutations()

            command_log.write_text("", encoding="utf-8")
            missing_marker = subprocess.run(
                ["bash", str(script), "lbc-cleanup"],
                cwd=REPO_ROOT,
                env={
                    **base_environment,
                    "CONFIRM_LBC_DELETE": "codex-portkey",
                    "PORTKEY_TEST_SA_ABSENT": "1",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(missing_marker.returncode, 0)
            self.assertIn("ownership marker", missing_marker.stderr)
            assert_no_cleanup_mutations()

            command_log.write_text("", encoding="utf-8")
            policy_marker.write_text(
                json.dumps({"Version": "2012-10-17", "Statement": []}),
                encoding="utf-8",
            )
            drifted_policy = subprocess.run(
                ["bash", str(script), "lbc-cleanup"],
                cwd=REPO_ROOT,
                env={**base_environment, "CONFIRM_LBC_DELETE": "codex-portkey"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(drifted_policy.returncode, 0)
            self.assertIn("has drifted from its CloudFormation stack", drifted_policy.stderr)
            assert_no_cleanup_mutations()
            policy_marker.write_text(json.dumps(current_policy), encoding="utf-8")

            command_log.write_text("", encoding="utf-8")
            unconfirmed = subprocess.run(
                ["bash", str(script), "lbc-cleanup"],
                cwd=REPO_ROOT,
                env=base_environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(unconfirmed.returncode, 0)
            self.assertIn("CONFIRM_LBC_DELETE=codex-portkey", unconfirmed.stderr)
            assert_no_cleanup_mutations()

            command_log.write_text("", encoding="utf-8")
            cleaned = subprocess.run(
                ["bash", str(script), "lbc-cleanup"],
                cwd=REPO_ROOT,
                env={
                    **base_environment,
                    "CONFIRM_LBC_DELETE": "codex-portkey",
                    "PORTKEY_TEST_EKSCTL_VERSION": "0.230.0",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(cleaned.returncode, 0, cleaned.stderr)
            commands = command_log.read_text(encoding="utf-8")
            self.assertIn("helm uninstall aws-load-balancer-controller", commands)
            self.assertIn("eksctl delete iamserviceaccount", commands)

    def test_portkey_helm_plan_keeps_secrets_in_mode_0600_temp_file(self):
        script = SCRIPTS_DIR / "portkey-stack.sh"
        secrets = {
            "PORTKEY_DOCKER_USERNAME": "registry-user-secret",
            "PORTKEY_DOCKER_PASSWORD": "registry-password-secret",
            "PORTKEY_CLIENT_AUTH": "client-auth-secret",
            "PORTKEY_ORGANIZATION_ID": "organization-secret",
            "PORTKEY_API_KEY": "workspace-service-api-key-secret",
        }

        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            values_path_marker = temp / "values-path"
            values_mode_marker = temp / "values-mode"
            rendered_values_marker = temp / "rendered-values.yaml"
            service_account_file = temp / "service-account.json"
            stack_file = temp / "gateway-iam-stack.json"
            role_file = temp / "gateway-role.json"
            service_account_file.write_text(
                json.dumps(self.gateway_service_account_payload()),
                encoding="utf-8",
            )
            stack_file.write_text(
                json.dumps(self.gateway_iam_stack_payload()),
                encoding="utf-8",
            )
            role_file.write_text(
                json.dumps(self.gateway_role_payload("us-west-2")),
                encoding="utf-8",
            )

            aws = fake_bin / "aws"
            aws.write_text(
                """#!/usr/bin/env bash
case "$*" in
  *"sts get-caller-identity"*"--query Account"*) printf '%s\\n' '123456789012' ;;
  *"cluster.identity.oidc.issuer"*) printf '%s\\n' 'https://oidc.eks.us-west-2.amazonaws.com/id/EXAMPLE' ;;
  *"acm describe-certificate"*) printf '%s\\n' '{"Status":"ISSUED","DomainName":"portkey.internal.example","SubjectAlternativeNames":["portkey.internal.example"]}' ;;
  *"ec2 describe-managed-prefix-lists"*) printf '%s\\n' '{"PrefixLists":[{"PrefixListId":"pl-0123456789abcdef0","OwnerId":"123456789012","AddressFamily":"IPv4","State":"create-complete","MaxEntries":20}]}' ;;
  *"ec2 get-managed-prefix-list-entries"*) printf '%s\\n' '{"Entries":[{"Cidr":"10.0.0.0/8"}]}' ;;
  *"cloudformation describe-stacks --stack-name eksctl-"*"StackName"*) printf '%s\\n' 'eksctl-codex-portkey-addon-iamserviceaccount-portkeyai-gateway-sa' ;;
  *"cloudformation describe-stacks --stack-name eksctl-"*"--output json"*) cat "$PORTKEY_TEST_GATEWAY_IAM_STACK_JSON" ;;
  *"GatewayManagedPolicyArn"*) printf '%s\\n' 'arn:aws:iam::123456789012:policy/portkey' ;;
  *GatewayLogBucketName*) printf '%s\\n' 'portkey-log-bucket' ;;
  *"iam get-role"*) cat "$PORTKEY_TEST_GATEWAY_ROLE_JSON" ;;
  *"iam list-attached-role-policies"*) printf '%s\\n' '["arn:aws:iam::123456789012:policy/portkey"]' ;;
  *"iam list-role-policies"*) printf '%s\\n' '[]' ;;
esac
exit 0
""",
                encoding="utf-8",
            )
            aws.chmod(0o700)

            kubectl = fake_bin / "kubectl"
            kubectl.write_text(
                """#!/usr/bin/env bash
case "$*" in
  *"get serviceaccount gateway-sa"*"--ignore-not-found -o name"*) printf '%s' 'serviceaccount/gateway-sa' ;;
  *"get serviceaccount gateway-sa"*"role-arn"*) printf '%s' 'arn:aws:iam::123456789012:role/portkey-gateway-role' ;;
  *"get serviceaccount gateway-sa -o json"*) cat "$PORTKEY_TEST_GATEWAY_SERVICE_ACCOUNT_JSON" ;;
esac
exit 0
""",
                encoding="utf-8",
            )
            kubectl.chmod(0o700)

            helm = fake_bin / "helm"
            helm.write_text(
                """#!/usr/bin/env bash
if [[ "${1:-}" == version && "${2:-}" == --short ]]; then printf '%s\n' 'v3.21.4+gtest'; exit 0; fi
for name in PORTKEY_DOCKER_USERNAME PORTKEY_DOCKER_PASSWORD PORTKEY_CLIENT_AUTH PORTKEY_ORGANIZATION_ID PORTKEY_API_KEY; do
  [[ -z "${!name+x}" ]] || exit 99
done
IFS=',' read -r -a canaries <<<"$PORTKEY_TEST_SECRET_CANARIES"
for argument in "$@"; do
  for canary in "${canaries[@]}"; do
    [[ "$argument" != *"$canary"* ]] || exit 98
  done
done
previous=''
for argument in "$@"; do
  if [[ "$previous" == '-f' ]]; then
    printf '%s' "$argument" >"$PORTKEY_TEST_VALUES_PATH"
    python3 -c 'import os,sys; print(oct(os.stat(sys.argv[1]).st_mode & 0o777))' \
      "$argument" >"$PORTKEY_TEST_VALUES_MODE"
    cp "$argument" "$PORTKEY_TEST_RENDERED_VALUES"
  fi
  previous="$argument"
done
exit 0
""",
                encoding="utf-8",
            )
            helm.chmod(0o700)

            environ = {
                **os.environ,
                **secrets,
                "AWS_REGION": "us-west-2",
                "BEDROCK_MANTLE_REGION": "us-east-2",
                "PORTKEY_GATEWAY_HOSTNAME": "portkey.internal.example",
                "PORTKEY_NLB_TLS_CERTIFICATE_ARN": self.certificate_arn(
                    "us-west-2"
                ),
                "PORTKEY_NLB_ALLOWED_PREFIX_LIST_IDS": "pl-0123456789abcdef0",
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                "PORTKEY_ENV_FILE": str(temp / "does-not-exist"),
                "PORTKEY_HELM_CHART_VERSION": "1.7.7",
                "PORTKEY_GATEWAY_IMAGE_TAG": "2026.08.03",
                "PORTKEY_GATEWAY_IMAGE_DIGEST": self.GATEWAY_IMAGE_DIGEST,
                "PORTKEY_REDIS_IMAGE_TAG": "7.2.10-alpine",
                "PORTKEY_REDIS_IMAGE_DIGEST": self.REDIS_IMAGE_DIGEST,
                "PORTKEY_TEST_VALUES_PATH": str(values_path_marker),
                "PORTKEY_TEST_VALUES_MODE": str(values_mode_marker),
                "PORTKEY_TEST_RENDERED_VALUES": str(rendered_values_marker),
                "PORTKEY_TEST_SECRET_CANARIES": ",".join(secrets.values()),
                "PORTKEY_TEST_GATEWAY_SERVICE_ACCOUNT_JSON": str(
                    service_account_file
                ),
                "PORTKEY_TEST_GATEWAY_IAM_STACK_JSON": str(stack_file),
                "PORTKEY_TEST_GATEWAY_ROLE_JSON": str(role_file),
            }
            result = subprocess.run(
                ["bash", str(script), "helm-plan"],
                cwd=REPO_ROOT,
                env=environ,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            combined_output = result.stdout + result.stderr
            for secret in secrets.values():
                self.assertNotIn(secret, combined_output)
            self.assertEqual(values_mode_marker.read_text().strip(), "0o600")
            rendered_values = Path(values_path_marker.read_text())
            self.assertFalse(rendered_values.exists())
            captured_values = rendered_values_marker.read_text(encoding="utf-8")
            self.assertIn('LOG_STORE_REGION: "us-west-2"', captured_values)
            self.assertIn("  port: 443", captured_values)
            self.assertIn("  containerPort: 8787", captured_values)
            self.assertIn(self.certificate_arn("us-west-2"), captured_values)
            self.assertIn("pl-0123456789abcdef0", captured_values)
            self.assertIn(
                f'tag: "2026.08.03@{self.GATEWAY_IMAGE_DIGEST}"',
                captured_values,
            )
            self.assertIn(
                f'tag: "7.2.10-alpine@{self.REDIS_IMAGE_DIGEST}"',
                captured_values,
            )
            self.assertNotIn("  port: 80", captured_values)
            self.assertNotIn("0.0.0.0/0", captured_values)
            self.assertNotIn(secrets["PORTKEY_API_KEY"], captured_values)
            self.assertNotIn("__PORTKEY_", captured_values)

    def test_portkey_helm_plan_rejects_untrusted_tls_resources_and_world_coverage(self):
        script = SCRIPTS_DIR / "portkey-stack.sh"
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fake_bin = temp / "bin"
            fake_bin.mkdir()

            aws = fake_bin / "aws"
            aws.write_text(
                """#!/usr/bin/env bash
case "$*" in
  *"sts get-caller-identity"*"--query Account"*) printf '%s\\n' '123456789012' ;;
  *"acm describe-certificate"*) printf '{"Status":"%s","DomainName":"portkey.internal.example","SubjectAlternativeNames":["portkey.internal.example"]}\\n' "${PORTKEY_TEST_CERT_STATUS:-ISSUED}" ;;
  *"ec2 describe-managed-prefix-lists"*) printf '{"PrefixLists":[{"PrefixListId":"pl-0123456789abcdef0","OwnerId":"%s","AddressFamily":"IPv4","State":"create-complete","MaxEntries":20}]}\\n' "${PORTKEY_TEST_PREFIX_OWNER:-123456789012}" ;;
  *"ec2 get-managed-prefix-list-entries"*)
    if [[ "${PORTKEY_TEST_WORLD_COVERAGE:-false}" == true ]]; then
      printf '%s\\n' '{"Entries":[{"Cidr":"0.0.0.0/1"},{"Cidr":"128.0.0.0/1"}]}'
    else
      printf '%s\\n' '{"Entries":[{"Cidr":"10.0.0.0/8"}]}'
    fi
    ;;
  *GatewayLogBucketName*) printf '%s\\n' 'portkey-log-bucket' ;;
esac
exit 0
""",
                encoding="utf-8",
            )
            aws.chmod(0o700)

            kubectl = fake_bin / "kubectl"
            kubectl.write_text(
                """#!/usr/bin/env bash
case "$*" in
  *serviceaccount*) printf '%s' 'arn:aws:iam::123456789012:role/portkey' ;;
esac
exit 0
""",
                encoding="utf-8",
            )
            kubectl.chmod(0o700)

            helm = fake_bin / "helm"
            helm.write_text(
                "#!/usr/bin/env bash\n"
                "if [[ \"${1:-}\" == version && \"${2:-}\" == --short ]]; "
                "then printf '%s\\n' 'v3.21.4+gtest'; fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            helm.chmod(0o700)

            base_environment = {
                **os.environ,
                "AWS_REGION": "us-east-1",
                "BEDROCK_MANTLE_REGION": "us-east-1",
                "PORTKEY_GATEWAY_HOSTNAME": "portkey.internal.example",
                "PORTKEY_NLB_TLS_CERTIFICATE_ARN": self.certificate_arn(),
                "PORTKEY_NLB_ALLOWED_PREFIX_LIST_IDS": "pl-0123456789abcdef0",
                "PORTKEY_DOCKER_USERNAME": "registry-user",
                "PORTKEY_DOCKER_PASSWORD": "registry-password",
                "PORTKEY_CLIENT_AUTH": "client-auth",
                "PORTKEY_ORGANIZATION_ID": "organization-id",
                "PORTKEY_HELM_CHART_VERSION": "1.7.7",
                "PORTKEY_GATEWAY_IMAGE_TAG": "2026.08.03",
                "PORTKEY_GATEWAY_IMAGE_DIGEST": self.GATEWAY_IMAGE_DIGEST,
                "PORTKEY_REDIS_IMAGE_TAG": "7.2.10-alpine",
                "PORTKEY_REDIS_IMAGE_DIGEST": self.REDIS_IMAGE_DIGEST,
                "PORTKEY_ENV_FILE": str(temp / "does-not-exist"),
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            }
            rejected = (
                (
                    {
                        "PORTKEY_TEST_CERT_STATUS": "EXPIRED",
                        "PORTKEY_NLB_TLS_AWS_VALIDATED": "true",
                        "_PORTKEY_NLB_TLS_AWS_VALIDATED": "true",
                    },
                    "certificate must be ISSUED",
                ),
                (
                    {"PORTKEY_TEST_PREFIX_OWNER": "999999999999"},
                    "customer-managed, active, IPv4",
                ),
                (
                    {"PORTKEY_TEST_WORLD_COVERAGE": "true"},
                    "must not cover the entire IPv4 address space",
                ),
            )
            for overrides, expected_error in rejected:
                with self.subTest(overrides=overrides):
                    result = subprocess.run(
                        ["bash", str(script), "helm-plan"],
                        cwd=REPO_ROOT,
                        env={**base_environment, **overrides},
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(expected_error, result.stderr)
                    self.assertNotIn(
                        "registry-password",
                        result.stdout + result.stderr,
                    )

        script_text = (SCRIPTS_DIR / "portkey-stack.sh").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("PORTKEY_NLB_TLS_AWS_VALIDATED", script_text)
        self.assertNotIn("_PORTKEY_NLB_TLS_AWS_VALIDATED", script_text)
        render_values = re.search(
            r"(?ms)^render_values\(\) \{(.*?)^\}", script_text
        )
        self.assertIsNotNone(render_values)
        self.assertIn("validate_nlb_tls_aws", render_values.group(1))
        helm_deploy = re.search(
            r"(?ms)^helm_deploy\(\) \{(.*?)^\}", script_text
        )
        self.assertIsNotNone(helm_deploy)
        deploy_body = helm_deploy.group(1)
        self.assertLess(
            deploy_body.index("require_safe_nlb_service_upgrade pre"),
            deploy_body.index('render_values "$values"'),
        )
        self.assertLess(
            deploy_body.index('render_values "$values"'),
            deploy_body.index("helm upgrade --install"),
        )


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
