import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest

import yaml


REPO_ROOT = Path(__file__).resolve().parents[3]
VALUES_TEMPLATE = REPO_ROOT / "deployment" / "portkey" / "values.yaml.tmpl"
POST_RENDERER = (
    REPO_ROOT / "deployment" / "portkey" / "portkey-post-renderer.sh"
)

CHART_VERSION = "1.7.7"
HELM_REPOSITORY = "https://portkey-ai.github.io/helm"
RELEASE_NAME = "portkey-ai"
NAMESPACE = "portkeyai"
GATEWAY_SERVICE = f"{RELEASE_NAME}-gateway"

GATEWAY_DIGEST = "sha256:" + ("a" * 64)
REDIS_DIGEST = "sha256:" + ("b" * 64)
GATEWAY_IMAGE = (
    f"docker.io/portkeyai/gateway_enterprise:2.13.0@{GATEWAY_DIGEST}"
)
REDIS_IMAGE = f"docker.io/redis:7.2.10-alpine@{REDIS_DIGEST}"
CERTIFICATE_ARN = (
    "arn:aws:acm:us-east-1:123456789012:certificate/"
    "11111111-2222-3333-4444-555555555555"
)
PREFIX_LIST_ID = "pl-0123456789abcdef0"


def run_helm(arguments, environment):
    return subprocess.run(
        ["helm", *arguments],
        check=True,
        capture_output=True,
        env=environment,
        text=True,
    )


def render_test_values(output_path):
    replacements = {
        "PORTKEY_GATEWAY_IMAGE_REFERENCE": (
            f"2.13.0@{GATEWAY_DIGEST}"
        ),
        "PORTKEY_REDIS_IMAGE_REFERENCE": (
            f"7.2.10-alpine@{REDIS_DIGEST}"
        ),
        "PORTKEY_DOCKER_USERNAME": "ci-dummy-user",
        "PORTKEY_DOCKER_PASSWORD": "ci-dummy-password",
        "PORTKEY_CLIENT_AUTH": "ci-dummy-client-auth",
        "PORTKEY_ORGANIZATION_ID": "ci-dummy-organization",
        "PORTKEY_LOG_BUCKET": "ci-dummy-log-bucket",
        "PORTKEY_LOG_STORE_REGION": "us-east-1",
        "PORTKEY_SERVICE_ACCOUNT": "gateway-sa",
        "PORTKEY_SERVICE_ROLE_ARN": (
            "arn:aws:iam::123456789012:role/ci-dummy-portkey-role"
        ),
        "PORTKEY_NLB_TLS_CERTIFICATE_ARN": CERTIFICATE_ARN,
        "PORTKEY_NLB_ALLOWED_PREFIX_LIST_IDS": PREFIX_LIST_ID,
    }

    rendered = VALUES_TEMPLATE.read_text(encoding="utf-8")
    for name, value in replacements.items():
        rendered = rendered.replace(f"__{name}__", json.dumps(value))

    if "__PORTKEY_" in rendered:
        raise AssertionError("Portkey values template contains an unresolved placeholder")

    output_path.write_text(rendered, encoding="utf-8")
    output_path.chmod(0o600)


class TestPortkeyChartRender(unittest.TestCase):
    def test_pinned_chart_preserves_private_service_boundary(self):
        self.assertIsNotNone(shutil.which("helm"), "helm is required for this test")
        self.assertIsNotNone(
            shutil.which("kubectl"),
            "kubectl with kustomize support is required for this test",
        )
        self.assertTrue(POST_RENDERER.is_file(), "Portkey post-renderer is missing")

        with TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            environment = {
                "HOME": str(temporary_path / "home"),
                "PATH": os.environ.get("PATH", ""),
                "HELM_CACHE_HOME": str(temporary_path / "helm-cache"),
                "HELM_CONFIG_HOME": str(temporary_path / "helm-config"),
                "HELM_DATA_HOME": str(temporary_path / "helm-data"),
                "PORTKEY_POST_RENDER_SERVICE_NAME": GATEWAY_SERVICE,
            }
            for variable in (
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "NO_PROXY",
                "http_proxy",
                "https_proxy",
                "no_proxy",
                "SSL_CERT_DIR",
                "SSL_CERT_FILE",
            ):
                if variable in os.environ:
                    environment[variable] = os.environ[variable]
            values_path = temporary_path / "values.yaml"
            render_test_values(values_path)

            helm_version = run_helm(["version", "--short"], environment)
            self.assertRegex(helm_version.stdout, r"^v3\.")
            run_helm(
                [
                    "repo",
                    "add",
                    "portkey-ai",
                    HELM_REPOSITORY,
                    "--force-update",
                ],
                environment,
            )
            result = run_helm(
                [
                    "template",
                    RELEASE_NAME,
                    "portkey-ai/gateway",
                    "--version",
                    CHART_VERSION,
                    "--namespace",
                    NAMESPACE,
                    "--values",
                    str(values_path),
                    "--post-renderer",
                    str(POST_RENDERER),
                ],
                environment,
            )

        documents = [
            document
            for document in yaml.safe_load_all(result.stdout)
            if document is not None
        ]
        services = {
            document["metadata"]["name"]: document
            for document in documents
            if document.get("kind") == "Service"
        }

        self.assertIn(GATEWAY_SERVICE, services)
        self.assertIn("redis", services)
        self.assertEqual(
            [
                name
                for name, service in services.items()
                if service["spec"].get("type") == "LoadBalancer"
            ],
            [GATEWAY_SERVICE],
        )
        self.assertFalse(
            any(
                service["spec"].get("type") == "NodePort"
                for service in services.values()
            )
        )

        gateway = services[GATEWAY_SERVICE]
        gateway_spec = gateway["spec"]
        self.assertEqual(gateway_spec.get("type"), "LoadBalancer")
        self.assertIs(gateway_spec.get("allocateLoadBalancerNodePorts"), False)
        self.assertTrue(
            all("nodePort" not in port for port in gateway_spec.get("ports", []))
        )

        redis_spec = services["redis"]["spec"]
        self.assertEqual(redis_spec.get("type"), "ClusterIP")
        self.assertTrue(
            all("nodePort" not in port for port in redis_spec.get("ports", []))
        )

        annotations = gateway["metadata"].get("annotations", {})
        expected_annotations = {
            "service.beta.kubernetes.io/aws-load-balancer-type": "external",
            "service.beta.kubernetes.io/aws-load-balancer-scheme": "internal",
            "service.beta.kubernetes.io/aws-load-balancer-ip-address-type": (
                "ipv4"
            ),
            "service.beta.kubernetes.io/aws-load-balancer-nlb-target-type": "ip",
            "service.beta.kubernetes.io/aws-load-balancer-ssl-cert": (
                CERTIFICATE_ARN
            ),
            "service.beta.kubernetes.io/aws-load-balancer-ssl-ports": "443",
            "service.beta.kubernetes.io/aws-load-balancer-ssl-negotiation-policy": (
                "ELBSecurityPolicy-TLS13-1-2-2021-06"
            ),
            "service.beta.kubernetes.io/aws-load-balancer-backend-protocol": (
                "tcp"
            ),
            "service.beta.kubernetes.io/aws-load-balancer-security-group-prefix-lists": (
                PREFIX_LIST_ID
            ),
            "service.beta.kubernetes.io/aws-load-balancer-healthcheck-path": (
                "/v1/health"
            ),
            "service.beta.kubernetes.io/aws-load-balancer-healthcheck-protocol": (
                "http"
            ),
            "service.beta.kubernetes.io/aws-load-balancer-healthcheck-port": (
                "8787"
            ),
            "service.beta.kubernetes.io/aws-load-balancer-manage-backend-security-group-rules": (
                "true"
            ),
        }
        for name, expected_value in expected_annotations.items():
            self.assertEqual(annotations.get(name), expected_value)

        workload_images = {}
        workload_pull_policies = {}
        for document in documents:
            if document.get("kind") not in {
                "DaemonSet",
                "Deployment",
                "Job",
                "StatefulSet",
            }:
                continue
            pod_spec = document["spec"]["template"]["spec"]
            containers = [
                *pod_spec.get("initContainers", []),
                *pod_spec.get("containers", []),
            ]
            workload_name = document["metadata"]["name"]
            workload_images[workload_name] = [
                container["image"]
                for container in containers
            ]
            workload_pull_policies[workload_name] = [
                container.get("imagePullPolicy")
                for container in containers
            ]

        self.assertEqual(
            workload_images,
            {
                GATEWAY_SERVICE: [GATEWAY_IMAGE],
                "redis": [REDIS_IMAGE],
            },
        )
        self.assertEqual(
            workload_pull_policies,
            {
                GATEWAY_SERVICE: ["Always"],
                "redis": ["IfNotPresent"],
            },
        )
        self.assertTrue(
            all(
                "@sha256:" in image
                for images in workload_images.values()
                for image in images
            )
        )


if __name__ == "__main__":
    unittest.main()
