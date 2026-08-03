import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
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
    def portkey_environment(self):
        return {
            **os.environ,
            "PORTKEY_ENV_FILE": str(REPO_ROOT / "does-not-exist"),
            "AWS_REGION": "us-east-1",
            "PORTKEY_BASE_URL": "https://portkey.internal.example/v1",
            "PORTKEY_PROVIDER_SLUG": "bedrock-mantle-validation",
            "PORTKEY_MODEL": "@bedrock-mantle-validation/openai.gpt-5.5",
            "PORTKEY_API_KEY": "do-not-print-this-secret",
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

    def test_portkey_check_rejects_model_fallback(self):
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
        self.assertIn("openai.gpt-5.5", result.stderr)
        self.assertNotIn("do-not-print-this-secret", result.stdout + result.stderr)

    def test_portkey_irsa_role_scopes_service_account_and_mantle(self):
        template = (
            REPO_ROOT / "deployment" / "portkey" / "hybrid-infrastructure.yaml"
        ).read_text(encoding="utf-8")
        self.assertIn("AWS::IAM::ManagedPolicy", template)
        script = (SCRIPTS_DIR / "portkey-stack.sh").read_text(encoding="utf-8")
        self.assertIn("eksctl create iamserviceaccount", script)
        self.assertIn("bedrock-mantle:Model: !Ref MantleModelId", template)
        self.assertIn("bedrock-mantle:CreateInference", template)
        self.assertNotIn("bedrock:InvokeModel", template)

    def test_portkey_hybrid_values_do_not_contain_committed_secrets(self):
        values = (
            REPO_ROOT / "deployment" / "portkey" / "values.yaml.tmpl"
        ).read_text(encoding="utf-8")
        self.assertIn("__PORTKEY_CLIENT_AUTH__", values)
        self.assertIn("__PORTKEY_DOCKER_PASSWORD__", values)
        self.assertNotIn("api.portkey.ai/v1", values)

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

    def test_portkey_rejects_public_plaintext_nlb_and_guards_scheme_changes(self):
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
        self.assertIn("public plaintext NLB", result.stderr)

        script = (SCRIPTS_DIR / "portkey-stack.sh").read_text(encoding="utf-8")
        match = re.search(
            r"(?ms)^require_safe_nlb_service_upgrade\(\) \{(.*?)^\}",
            script,
        )
        self.assertIsNotNone(match)
        self.assertIn("aws-load-balancer-scheme", match.group(1))
        self.assertIn('"$current_scheme" != internal', match.group(1))

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
        self.assertIn('MantleModelId="$MANTLE_MODEL_ID"', deploy)

    def test_portkey_nlb_is_owned_by_aws_load_balancer_controller(self):
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
        self.assertIn('"aws:RequestedRegion": "us-east-1"', serialized)
        self.assertIn("ec2:Vpc", serialized)
        self.assertIn('"elasticloadbalancing:Scheme": "internal"', serialized)
        self.assertNotIn("wafv2:", serialized)
        self.assertNotIn("shield:", serialized)
        self.assertNotIn("acm:", serialized)
        self.assertNotIn("elasticloadbalancing:CreateRule", actions)

        script = (SCRIPTS_DIR / "portkey-stack.sh").read_text(encoding="utf-8")
        self.assertIn("render_load_balancer_controller_service_account()", script)
        self.assertIn('"attachPolicy": policy', script)
        self.assertIn("found_placeholders != set(replacements)", script)
        self.assertNotIn('if "__" in policy_text', script)
        self.assertNotIn("wellKnownPolicies", script)

    def test_portkey_reuses_only_a_compatible_controller_watch_scope(self):
        script = SCRIPTS_DIR / "portkey-stack.sh"
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fake_bin = temp / "bin"
            fake_bin.mkdir()

            aws = fake_bin / "aws"
            aws.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            aws.chmod(0o700)

            kubectl = fake_bin / "kubectl"
            kubectl.write_text(
                """#!/usr/bin/env bash
case "$*" in
  *"get deployment aws-load-balancer-controller --ignore-not-found -o name"*) printf '%s' 'deployment.apps/aws-load-balancer-controller' ;;
  *"rollout status"*) exit 0 ;;
  *"get crd targetgroupbindings.elbv2.k8s.aws"*) exit 0 ;;
  *"get deployment aws-load-balancer-controller -o json"*)
    label_version="${PORTKEY_TEST_LBC_LABEL_VERSION:-v3.4.2}"
    image_version="${PORTKEY_TEST_LBC_IMAGE_VERSION:-v3.4.2}"
    watch="${PORTKEY_TEST_LBC_WATCH:-portkeyai}"
    args='["--cluster-name=codex-portkey"]'
    if [[ "$watch" != all ]]; then
      args='["--cluster-name=codex-portkey","--watch-namespace='"$watch"'"]'
    fi
    printf '{"metadata":{"labels":{"app.kubernetes.io/version":"%s"}},"spec":{"template":{"spec":{"containers":[{"name":"aws-load-balancer-controller","image":"public.ecr.aws/eks/aws-load-balancer-controller:%s","args":%s}]}}}}' "$label_version" "$image_version" "$args"
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
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                "PORTKEY_ENV_FILE": str(temp / "does-not-exist"),
            }
            compatible = subprocess.run(
                ["bash", str(script), "lbc-status"],
                cwd=REPO_ROOT,
                env=environ,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(compatible.returncode, 0, compatible.stderr)

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

    def test_portkey_lbc_deploy_runs_irsa_chart_and_readiness_flow(self):
        script = SCRIPTS_DIR / "portkey-stack.sh"
        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            command_log = temp / "commands.log"

            aws = fake_bin / "aws"
            aws.write_text(
                """#!/usr/bin/env bash
printf 'aws %s\n' "$*" >>"$PORTKEY_TEST_COMMAND_LOG"
case "$*" in
  *"sts get-caller-identity"*"--query Account"*) printf '%s\n' '123456789012' ;;
  *"sts get-caller-identity"*) printf '%s\n' '{}' ;;
  *"cluster.identity.oidc.issuer"*) printf '%s\n' 'https://oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE' ;;
  *"cluster.resourcesVpcConfig.vpcId"*) printf '%s\n' 'vpc-0123456789abcdef0' ;;
esac
exit 0
""",
                encoding="utf-8",
            )
            aws.chmod(0o700)

            eksctl = fake_bin / "eksctl"
            eksctl.write_text(
                """#!/usr/bin/env bash
if [[ "${1:-}" == version ]]; then printf '%s\n' '0.229.0'; exit 0; fi
printf 'eksctl %s\n' "$*" >>"$PORTKEY_TEST_COMMAND_LOG"
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
  *"get deployment aws-load-balancer-controller"*) exit 0 ;;
  *"get serviceaccount aws-load-balancer-controller"*"role-arn"*) printf '%s' 'arn:aws:iam::123456789012:role/platform-lbc' ;;
  *"get serviceaccount aws-load-balancer-controller"*"managed-by"*) printf '%s' 'platform-team' ;;
  *"get serviceaccount aws-load-balancer-controller"*) [[ "${PORTKEY_TEST_EXISTING_SA:-}" == 1 ]] && printf '%s' 'serviceaccount/aws-load-balancer-controller'; exit 0 ;;
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
exit 0
""",
                encoding="utf-8",
            )
            helm.chmod(0o700)

            environ = {
                **os.environ,
                "AWS_REGION": "us-east-1",
                "CONFIRM_AWS_WRITE": "1",
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                "PORTKEY_ENV_FILE": str(temp / "does-not-exist"),
                "PORTKEY_TEST_COMMAND_LOG": str(command_log),
            }
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

            aws = fake_bin / "aws"
            aws.write_text(
                """#!/usr/bin/env bash
printf 'aws %s\n' "$*" >>"$PORTKEY_TEST_COMMAND_LOG"
case "$*" in
  *"sts get-caller-identity"*"--query Account"*) printf '%s\n' '123456789012' ;;
  *"sts get-caller-identity"*) printf '%s\n' '{}' ;;
  *"cluster.identity.oidc.issuer"*) printf '%s\n' 'https://oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE' ;;
  *"cluster.resourcesVpcConfig.vpcId"*) printf '%s\n' 'vpc-0123456789abcdef0' ;;
esac
exit 0
""",
                encoding="utf-8",
            )
            aws.chmod(0o700)

            eksctl = fake_bin / "eksctl"
            eksctl.write_text(
                """#!/usr/bin/env bash
if [[ "${1:-}" == version ]]; then printf '%s\n' '0.229.0'; exit 0; fi
printf 'eksctl %s\n' "$*" >>"$PORTKEY_TEST_COMMAND_LOG"
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
  *"rollout status"*"--timeout=2m"*) exit 1 ;;
  *"rollout status"*"--timeout=5m"*) exit 0 ;;
  *"release-namespace"*) printf '%s' 'kube-system' ;;
  *"release-name"*) printf '%s' 'aws-load-balancer-controller' ;;
  *"get deployment aws-load-balancer-controller"*) printf '%s' 'deployment.apps/aws-load-balancer-controller' ;;
  *"get serviceaccount aws-load-balancer-controller"*"role-arn"*) printf '%s' 'arn:aws:iam::123456789012:role/lbc' ;;
  *"get serviceaccount aws-load-balancer-controller"*"managed-by"*) printf '%s' "${PORTKEY_TEST_MANAGED_BY:-guidance-codex}" ;;
  *"get serviceaccount aws-load-balancer-controller"*) printf '%s' 'serviceaccount/aws-load-balancer-controller' ;;
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
exit 0
""",
                encoding="utf-8",
            )
            helm.chmod(0o700)

            environ = {
                **os.environ,
                "AWS_REGION": "us-east-1",
                "CONFIRM_AWS_WRITE": "1",
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                "PORTKEY_ENV_FILE": str(temp / "does-not-exist"),
                "PORTKEY_TEST_COMMAND_LOG": str(command_log),
            }
            result = subprocess.run(
                ["bash", str(script), "lbc-deploy"],
                cwd=REPO_ROOT,
                env=environ,
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
            self.assertIn("helm upgrade --install aws-load-balancer-controller", commands)
            self.assertNotIn("eksctl create iamserviceaccount", commands)

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

            aws = fake_bin / "aws"
            aws.write_text(
                """#!/usr/bin/env bash
case "$*" in
  *"sts get-caller-identity"*) printf '%s\n' '{}' ;;
  *"cluster.identity.oidc.issuer"*) printf '%s\n' 'https://oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE' ;;
esac
exit 0
""",
                encoding="utf-8",
            )
            aws.chmod(0o700)

            eksctl = fake_bin / "eksctl"
            eksctl.write_text(
                """#!/usr/bin/env bash
if [[ "${1:-}" == version ]]; then printf '%s\n' '0.229.0'; exit 0; fi
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
  *"release-namespace"*) printf '%s' 'kube-system' ;;
  *"release-name"*) printf '%s' 'aws-load-balancer-controller' ;;
  *"get deployment aws-load-balancer-controller -o json"*) printf '%s' '{"metadata":{"labels":{"app.kubernetes.io/version":"v3.4.2"}},"spec":{"template":{"spec":{"containers":[{"name":"aws-load-balancer-controller","image":"public.ecr.aws/eks/aws-load-balancer-controller:v3.4.2","args":["--cluster-name=codex-portkey","--watch-namespace=portkeyai"]}]}}}}' ;;
  *"get deployment aws-load-balancer-controller"*) printf '%s' 'deployment.apps/aws-load-balancer-controller' ;;
  *"get serviceaccount aws-load-balancer-controller"*"managed-by"*) printf '%s' 'guidance-codex' ;;
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
            }
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
            self.assertNotIn("helm uninstall", command_log.read_text(encoding="utf-8"))

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
            self.assertNotIn("helm uninstall", command_log.read_text(encoding="utf-8"))

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
            self.assertNotIn("helm uninstall", command_log.read_text(encoding="utf-8"))

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
            self.assertNotIn("helm uninstall", command_log.read_text(encoding="utf-8"))

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
            self.assertNotIn("helm uninstall", command_log.read_text(encoding="utf-8"))

            command_log.write_text("", encoding="utf-8")
            cleaned = subprocess.run(
                ["bash", str(script), "lbc-cleanup"],
                cwd=REPO_ROOT,
                env={**base_environment, "CONFIRM_LBC_DELETE": "codex-portkey"},
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
        }

        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            values_path_marker = temp / "values-path"
            values_mode_marker = temp / "values-mode"

            aws = fake_bin / "aws"
            aws.write_text(
                """#!/usr/bin/env bash
case "$*" in
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
                """#!/usr/bin/env bash
for name in PORTKEY_DOCKER_USERNAME PORTKEY_DOCKER_PASSWORD PORTKEY_CLIENT_AUTH PORTKEY_ORGANIZATION_ID PORTKEY_API_KEY; do
  [[ -z "${!name+x}" ]] || exit 99
done
previous=''
for argument in "$@"; do
  if [[ "$previous" == '-f' ]]; then
    printf '%s' "$argument" >"$PORTKEY_TEST_VALUES_PATH"
    python3 -c 'import os,sys; print(oct(os.stat(sys.argv[1]).st_mode & 0o777))' \
      "$argument" >"$PORTKEY_TEST_VALUES_MODE"
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
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                "PORTKEY_ENV_FILE": str(temp / "does-not-exist"),
                "PORTKEY_HELM_CHART_VERSION": "1.7.7",
                "PORTKEY_GATEWAY_IMAGE_TAG": "2026.08.03",
                "PORTKEY_REDIS_IMAGE_TAG": "7.2.10-alpine",
                "PORTKEY_TEST_VALUES_PATH": str(values_path_marker),
                "PORTKEY_TEST_VALUES_MODE": str(values_mode_marker),
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
