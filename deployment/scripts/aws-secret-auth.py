#!/usr/bin/env python3
"""Resolve one Secrets Manager JSON field for auth or a child process."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Optional


def resolve_secret_field(
    *,
    aws_cli: str,
    region: str,
    secret_id: str,
    field: str,
    profile: Optional[str] = None,
) -> str:
    command = [aws_cli]
    if profile:
        command.extend(["--profile", profile])
    command.extend(
        [
            "secretsmanager",
            "get-secret-value",
            "--secret-id",
            secret_id,
            "--version-stage",
            "AWSCURRENT",
            "--region",
            region,
            "--query",
            "SecretString",
            "--output",
            "text",
        ]
    )
    result = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Secrets Manager lookup failed: "
            + (result.stderr.strip() or "unknown AWS CLI error")
        )
    try:
        secret = json.loads(result.stdout)
        value = secret[field]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise RuntimeError(
            f"Secrets Manager value does not contain string field {field!r}"
        ) from error
    if not isinstance(value, str) or not value:
        raise RuntimeError(
            f"Secrets Manager value does not contain string field {field!r}"
        )
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--aws-cli", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--secret-id", required=True)
    parser.add_argument("--field", required=True)
    parser.add_argument("--profile")
    subparsers = parser.add_subparsers(dest="action", required=True)

    subparsers.add_parser(
        "print-token",
        help="Print the field for a trusted authentication command.",
    )
    exec_parser = subparsers.add_parser(
        "exec-env",
        help="Set the field in a child-process environment without printing it.",
    )
    exec_parser.add_argument("--env", required=True)
    exec_parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    value = resolve_secret_field(
        aws_cli=args.aws_cli,
        region=args.region,
        secret_id=args.secret_id,
        field=args.field,
        profile=args.profile,
    )
    if args.action == "print-token":
        # Codex consumes stdout as the credential-provider protocol response.
        # This is not operational logging.
        # lgtm[py/clear-text-logging-sensitive-data]
        sys.stdout.write(value + "\n")
        return 0

    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise RuntimeError("exec-env requires a command after --")
    child_env = os.environ.copy()
    child_env[args.env] = value
    return subprocess.run(command, env=child_env, check=False).returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
