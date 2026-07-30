#!/usr/bin/env python3
"""Read-only preflight checks for the LiteLLM AWS walkthrough."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys


DIGEST_REFERENCE = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")
ECR_REFERENCE = re.compile(
    r"^(?P<account>[0-9]{12})\.dkr\.ecr\.(?P<region>[a-z0-9-]+)"
    r"\.amazonaws\.com(?:\.cn)?/(?P<repository>[a-z0-9._/-]+)"
    r"@(?P<digest>sha256:[0-9a-f]{64})$"
)
CERTIFICATE_ARN = re.compile(
    r"^arn:[^:]+:acm:(?P<region>[a-z0-9-]+):[0-9]{12}:certificate/.+$"
)
HOSTED_ZONE_ID = re.compile(r"^(?:/hostedzone/)?Z[A-Z0-9]+$")


def run(command: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def find_aws_cli(environ: dict[str, str]) -> tuple[str | None, str]:
    candidates = [
        environ.get("AWS_CLI"),
        shutil.which("aws"),
        "/usr/local/bin/aws",
        "/opt/homebrew/bin/aws",
    ]
    checked = set()
    fallback_version = ""
    for candidate in candidates:
        if not candidate or candidate in checked:
            continue
        checked.add(candidate)
        if not os.path.isfile(candidate) or not os.access(candidate, os.X_OK):
            continue
        result = run([candidate, "--version"])
        version = (result.stdout or result.stderr).strip()
        fallback_version = fallback_version or version
        if result.returncode == 0 and version.startswith("aws-cli/2."):
            return candidate, version
    return None, fallback_version


def validate_digest_reference(value: str) -> bool:
    return bool(DIGEST_REFERENCE.fullmatch(value))


def parse_ecr_reference(value: str) -> dict[str, str] | None:
    match = ECR_REFERENCE.fullmatch(value)
    return match.groupdict() if match else None


def validate_cidr(value: str) -> bool:
    try:
        network = ipaddress.ip_network(value, strict=False)
    except ValueError:
        return False
    return network.version == 4 and network.prefixlen > 0


def hostname_in_zone(hostname: str, zone_name: str) -> bool:
    hostname = hostname.rstrip(".").lower()
    zone_name = zone_name.rstrip(".").lower()
    return hostname == zone_name or hostname.endswith(f".{zone_name}")


def check_environment(
    environ: dict[str, str], stage: str = "deploy"
) -> tuple[list[str], list[str]]:
    errors = []
    warnings = []
    required = ["AWS_REGION", "LITELLM_BASE_IMAGE"]
    if stage == "deploy":
        required.extend(
            [
                "BEDROCK_REGION",
                "ALLOWED_CIDR",
                "GATEWAY_DOMAIN_NAME",
                "LITELLM_IMAGE",
            ]
        )
    for name in required:
        if not environ.get(name):
            errors.append(f"{name} is not set")

    base_image = environ.get("LITELLM_BASE_IMAGE", "")
    if base_image and not validate_digest_reference(base_image):
        errors.append("LITELLM_BASE_IMAGE must use an immutable sha256 digest")

    image = environ.get("LITELLM_IMAGE", "")
    if stage == "deploy" and image and not parse_ecr_reference(image):
        errors.append("LITELLM_IMAGE must be an ECR image with a sha256 digest")

    cidr = environ.get("ALLOWED_CIDR", "") if stage == "deploy" else ""
    if cidr and not validate_cidr(cidr):
        errors.append("ALLOWED_CIDR must be a restricted IPv4 network")

    certificate = (
        environ.get("ALB_CERTIFICATE_ARN", "") if stage == "deploy" else ""
    )
    certificate_match = CERTIFICATE_ARN.fullmatch(certificate) if certificate else None
    if certificate and not certificate_match:
        errors.append("ALB_CERTIFICATE_ARN is not an ACM certificate ARN")
    elif certificate_match and certificate_match.group("region") != environ.get(
        "AWS_REGION"
    ):
        errors.append("ALB certificate and gateway stack must be in the same region")

    hosted_zone = (
        environ.get("ROUTE53_HOSTED_ZONE_ID", "") if stage == "deploy" else ""
    )
    if hosted_zone and not HOSTED_ZONE_ID.fullmatch(hosted_zone):
        errors.append("ROUTE53_HOSTED_ZONE_ID is not a valid hosted zone ID")
    if stage == "deploy" and not certificate and not hosted_zone:
        errors.append(
            "Set ROUTE53_HOSTED_ZONE_ID for a managed certificate or "
            "ALB_CERTIFICATE_ARN for an existing certificate"
        )
    return errors, warnings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--stage",
        choices=["build", "deploy"],
        default="deploy",
        help="Run checks needed before the image build or stack deployment.",
    )
    parser.add_argument(
        "--check-ecr-image",
        action="store_true",
        help="Confirm the configured image digest exists in ECR.",
    )
    args = parser.parse_args()

    errors, warnings = check_environment(os.environ, args.stage)

    aws_cli, aws_version = find_aws_cli(os.environ)
    if not aws_cli:
        errors.append(f"AWS CLI v2 is required; found {aws_version or 'nothing'}")

    identity = None
    if aws_cli:
        result = run(
            [
                aws_cli,
                "sts",
                "get-caller-identity",
                "--region",
                os.environ.get("AWS_REGION", "us-east-1"),
                "--output",
                "json",
            ]
        )
        if result.returncode != 0:
            errors.append("AWS credentials are unavailable or expired")
        else:
            try:
                identity = json.loads(result.stdout)
            except json.JSONDecodeError:
                errors.append("AWS identity response was not valid JSON")

    hosted_zone = os.environ.get("ROUTE53_HOSTED_ZONE_ID", "")
    domain_name = os.environ.get("GATEWAY_DOMAIN_NAME", "")
    if args.stage == "deploy" and hosted_zone and aws_cli:
        result = run(
            [
                aws_cli,
                "route53",
                "get-hosted-zone",
                "--id",
                hosted_zone,
                "--output",
                "json",
            ]
        )
        if result.returncode != 0:
            errors.append("ROUTE53_HOSTED_ZONE_ID was not found or is not accessible")
        else:
            try:
                zone_name = json.loads(result.stdout)["HostedZone"]["Name"]
            except (json.JSONDecodeError, KeyError, TypeError):
                errors.append("Route 53 hosted zone response was not valid JSON")
            else:
                if not hostname_in_zone(domain_name, zone_name):
                    errors.append(
                        "GATEWAY_DOMAIN_NAME is not inside ROUTE53_HOSTED_ZONE_ID"
                    )

    if args.stage == "build" and not shutil.which("docker"):
        errors.append("Docker is not installed")
    elif args.stage == "build":
        for command, label in [
            (["docker", "info"], "Docker daemon"),
            (["docker", "buildx", "version"], "Docker buildx"),
        ]:
            result = run(command)
            if result.returncode != 0:
                errors.append(f"{label} is unavailable")

    image = os.environ.get("LITELLM_IMAGE", "")
    image_parts = parse_ecr_reference(image) if image else None
    if image_parts and identity:
        if image_parts["account"] != identity.get("Account"):
            errors.append("LITELLM_IMAGE account does not match the active AWS account")
        if image_parts["region"] != os.environ.get("AWS_REGION"):
            errors.append("LITELLM_IMAGE region does not match AWS_REGION")

    if args.check_ecr_image and args.stage != "deploy":
        errors.append("--check-ecr-image requires --stage deploy")
    elif args.check_ecr_image and image_parts and aws_cli:
        result = run(
            [
                aws_cli,
                "ecr",
                "describe-images",
                "--repository-name",
                image_parts["repository"],
                "--image-ids",
                f"imageDigest={image_parts['digest']}",
                "--region",
                image_parts["region"],
                "--output",
                "json",
            ]
        )
        if result.returncode != 0:
            errors.append("LITELLM_IMAGE digest was not found in ECR")

    for warning in warnings:
        print(f"WARN: {warning}")
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    if errors:
        return 1

    if args.stage == "build":
        summary = "CLI v2, AWS identity, Docker, buildx, and base-image digest"
    else:
        summary = (
            "CLI v2, AWS identity, environment, TLS, DNS, CIDR, "
            "and immutable deployment image"
        )
    print(f"LiteLLM {args.stage} preflight passed: {summary}.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.TimeoutExpired as error:
        print(f"ERROR: command timed out: {error.cmd}", file=sys.stderr)
        raise SystemExit(1)
