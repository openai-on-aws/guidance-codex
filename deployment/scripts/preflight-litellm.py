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


def run(command: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def validate_digest_reference(value: str) -> bool:
    return bool(DIGEST_REFERENCE.fullmatch(value))


def parse_ecr_reference(value: str) -> dict[str, str] | None:
    match = ECR_REFERENCE.fullmatch(value)
    return match.groupdict() if match else None


def validate_cidr(value: str) -> bool:
    try:
        ipaddress.ip_network(value, strict=False)
    except ValueError:
        return False
    return True


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
                "ALB_CERTIFICATE_ARN",
                "ALLOWED_CIDR",
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
        errors.append("ALLOWED_CIDR is not a valid IPv4 or IPv6 network")

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

    if stage == "deploy" and not environ.get("GATEWAY_DOMAIN_NAME"):
        warnings.append(
            "GATEWAY_DOMAIN_NAME is not set; the raw ALB hostname is unsuitable "
            "for a trusted Codex endpoint"
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

    if not shutil.which("aws"):
        errors.append("AWS CLI is not installed")
        aws_version = ""
    else:
        result = run(["aws", "--version"])
        aws_version = (result.stdout or result.stderr).strip()
        if result.returncode != 0:
            errors.append("AWS CLI version check failed")
        elif not aws_version.startswith("aws-cli/2."):
            errors.append(f"AWS CLI v2 is required; found {aws_version or 'unknown'}")

    identity = None
    if not errors or shutil.which("aws"):
        result = run(
            [
                "aws",
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
    elif args.check_ecr_image and image_parts and shutil.which("aws"):
        result = run(
            [
                "aws",
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
            "CLI v2, AWS identity, environment, certificate, CIDR, "
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
