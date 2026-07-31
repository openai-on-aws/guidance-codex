#!/usr/bin/env python3
"""Create a scoped LiteLLM key and store it without printing secret material."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Optional
import urllib.error
import urllib.request


def generate_key(
    admin_url: str,
    master_key: str,
    key_alias: str,
    models: list[str],
    *,
    user_id: Optional[str] = None,
    team_id: Optional[str] = None,
    max_budget: Optional[float] = None,
    budget_duration: Optional[str] = None,
    tpm_limit: Optional[int] = None,
    rpm_limit: Optional[int] = None,
) -> str:
    key_options = {
        "key_alias": key_alias,
        "models": models,
        "user_id": user_id,
        "team_id": team_id,
        "max_budget": max_budget,
        "budget_duration": budget_duration,
        "tpm_limit": tpm_limit,
        "rpm_limit": rpm_limit,
    }
    payload = json.dumps(
        {key: value for key, value in key_options.items() if value is not None}
    ).encode()
    request = urllib.request.Request(
        f"{admin_url.rstrip('/')}/key/generate",
        data=payload,
        headers={
            "Authorization": f"Bearer {master_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(
            f"LiteLLM key generation failed with HTTP {error.code}"
        ) from error
    key = result.get("key")
    if not isinstance(key, str) or not key:
        raise RuntimeError("LiteLLM key generation response did not contain a key")
    return key


def run_aws(
    aws_cli: str,
    arguments: list[str],
    *,
    secret_input: str | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [aws_cli, *arguments],
        input=secret_input,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


def secret_exists(*, aws_cli: str, region: str, secret_id: str) -> bool:
    result = run_aws(
        aws_cli,
        [
            "secretsmanager",
            "describe-secret",
            "--secret-id",
            secret_id,
            "--region",
            region,
            "--output",
            "json",
        ],
    )
    if result.returncode == 0:
        return True
    if "ResourceNotFoundException" in result.stderr:
        return False
    raise RuntimeError(
        f"Secrets Manager lookup failed: {result.stderr.strip() or 'unknown error'}"
    )


def store_key(
    *,
    aws_cli: str,
    region: str,
    secret_id: str,
    kms_key_id: str,
    key: str,
) -> None:
    secret_json = json.dumps({"LITELLM_API_KEY": key})
    operation = [
        "secretsmanager",
        "create-secret",
        "--name",
        secret_id,
        "--description",
        "Scoped LiteLLM API key for Codex",
        "--kms-key-id",
        kms_key_id,
        "--secret-string",
        "file:///dev/stdin",
        "--region",
        region,
        "--output",
        "json",
    ]
    for attempt in range(3):
        result = run_aws(aws_cli, operation, secret_input=secret_json)
        if result.returncode == 0:
            return
        if attempt < 2:
            time.sleep(2**attempt)
    raise RuntimeError(
        f"Secrets Manager write failed: {result.stderr.strip() or 'unknown error'}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--admin-url", required=True)
    parser.add_argument("--secret-id", required=True)
    parser.add_argument("--kms-key-id", required=True)
    parser.add_argument("--aws-cli", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--key-alias", required=True)
    parser.add_argument("--models", default="gpt-5.5")
    parser.add_argument("--user-id")
    parser.add_argument("--team-id")
    parser.add_argument("--max-budget", type=float)
    parser.add_argument("--budget-duration")
    parser.add_argument("--tpm-limit", type=int)
    parser.add_argument("--rpm-limit", type=int)
    args = parser.parse_args()

    master_key = os.environ.get("LITELLM_MASTER_KEY")
    if not master_key:
        raise RuntimeError("LITELLM_MASTER_KEY was not provided by the secret resolver")
    models = [model.strip() for model in args.models.split(",") if model.strip()]
    if not models:
        raise RuntimeError("--models must contain at least one model")

    if secret_exists(
        aws_cli=args.aws_cli,
        region=args.region,
        secret_id=args.secret_id,
    ):
        print("Scoped LiteLLM key secret already exists.")
        return 0

    key = generate_key(
        args.admin_url,
        master_key,
        args.key_alias,
        models,
        user_id=args.user_id,
        team_id=args.team_id,
        max_budget=args.max_budget,
        budget_duration=args.budget_duration,
        tpm_limit=args.tpm_limit,
        rpm_limit=args.rpm_limit,
    )
    store_key(
        aws_cli=args.aws_cli,
        region=args.region,
        secret_id=args.secret_id,
        kms_key_id=args.kms_key_id,
        key=key,
    )
    print("Stored scoped LiteLLM key in Secrets Manager.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.TimeoutExpired, urllib.error.URLError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
