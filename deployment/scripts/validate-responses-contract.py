#!/usr/bin/env python3
"""Validate the minimum Responses API contract required by Codex."""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def send_response(base_url: str, api_key: str, payload: dict, timeout: int) -> dict:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"gateway returned HTTP {error.code}: {body}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"gateway request failed: {error.reason}") from error

    try:
        return json.loads(body)
    except json.JSONDecodeError as error:
        raise RuntimeError("gateway did not return a JSON response") from error


def validate_response(response: dict, request_name: str) -> str:
    if response.get("object") != "response":
        raise RuntimeError(f"{request_name}: object must be 'response'")
    if not response.get("id"):
        raise RuntimeError(f"{request_name}: response id is missing")
    if response.get("status") not in {"completed", "in_progress"}:
        raise RuntimeError(
            f"{request_name}: unexpected status {response.get('status')!r}"
        )
    if not isinstance(response.get("output"), list):
        raise RuntimeError(f"{request_name}: output must be an array")
    return response["id"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base-url",
        default=os.environ.get("GATEWAY_BASE_URL"),
        help="Gateway API base ending in /v1 (or set GATEWAY_BASE_URL).",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("GATEWAY_API_KEY"),
        help="Gateway key (or set GATEWAY_API_KEY).",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("GATEWAY_MODEL", "gpt-5.5"),
    )
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    if not args.base_url or not args.api_key:
        parser.error("--base-url and --api-key are required")
    return args


def main() -> int:
    args = parse_args()
    first = send_response(
        args.base_url,
        args.api_key,
        {
            "model": args.model,
            "input": "Reply with the single word READY.",
            "reasoning": {"effort": "low"},
            "text": {"verbosity": "low"},
            "prompt_cache_key": "enterprise-gateway-contract",
            "store": True,
        },
        args.timeout,
    )
    first_id = validate_response(first, "initial request")

    follow_up = send_response(
        args.base_url,
        args.api_key,
        {
            "model": args.model,
            "input": "Reply with the single word COMPLETE.",
            "previous_response_id": first_id,
            "reasoning": {"effort": "low"},
            "text": {"verbosity": "low"},
            "store": False,
        },
        args.timeout,
    )
    validate_response(follow_up, "continuation request")

    print("Responses contract passed: fields, response shape, and continuation.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"Responses contract failed: {error}", file=sys.stderr)
        raise SystemExit(1)
