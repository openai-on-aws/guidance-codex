#!/usr/bin/env python3
"""Validate the Responses API contract required by Codex."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


HEADER_NAME_PATTERN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


def parse_header_env(specs: list[str], environ: dict[str, str]) -> dict[str, str]:
    headers = {}
    for spec in specs:
        if "=" not in spec:
            raise ValueError(
                f"invalid --header-env {spec!r}; expected HEADER=ENV_VAR"
            )
        header_name, env_name = spec.split("=", 1)
        if not HEADER_NAME_PATTERN.fullmatch(header_name) or not env_name:
            raise ValueError(
                f"invalid --header-env {spec!r}; expected HEADER=ENV_VAR"
            )
        value = environ.get(env_name)
        if not value:
            raise ValueError(f"environment variable {env_name!r} is not set")
        if "\r" in value or "\n" in value:
            raise ValueError(f"environment variable {env_name!r} contains a newline")
        headers[header_name] = value
    return headers


def build_headers(api_key: str | None, extra_headers: dict[str, str]) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    has_custom_authorization = any(
        name.lower() == "authorization" for name in extra_headers
    )
    if api_key and not has_custom_authorization:
        headers["Authorization"] = f"Bearer {api_key}"
    headers.update(extra_headers)
    if not any(name.lower() == "authorization" for name in headers):
        raise ValueError(
            "set the API-key environment variable or provide Authorization "
            "with --header-env"
        )
    return headers


def send_request(
    base_url: str,
    headers: dict[str, str],
    payload: dict,
    timeout: int,
) -> tuple[str, bytes]:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content_type = response.headers.get("Content-Type", "")
            body = response.read()
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"gateway returned HTTP {error.code}: {body}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"gateway request failed: {error.reason}") from error
    return content_type, body


def send_response(
    base_url: str,
    headers: dict[str, str],
    payload: dict,
    timeout: int,
) -> dict:
    _, body = send_request(base_url, headers, payload, timeout)
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("gateway did not return a JSON response") from error


def require_listed_model(
    base_url: str,
    headers: dict[str, str],
    model: str,
    timeout: int,
    attempts: int = 1,
    delay_seconds: float = 0,
) -> None:
    provider = model.split("/", 1)[0].lstrip("@") if "/" in model else ""
    query = urllib.parse.urlencode({"provider": provider, "limit": 100})
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            f"{base_url.rstrip('/')}/models?{query}",
            headers=headers,
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as error:
            error.read()
            raise RuntimeError(
                f"model catalog returned HTTP {error.code}"
            ) from error
        except urllib.error.URLError as error:
            raise RuntimeError(
                f"model catalog request failed: {error.reason}"
            ) from error
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("model catalog did not return JSON") from error
        listed = {
            item.get("id")
            for item in payload.get("data", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        if model in listed:
            return
        if attempt < attempts:
            time.sleep(delay_seconds)
    raise RuntimeError(
        f"model catalog does not expose required model {model!r} "
        f"after {attempts} attempt(s)"
    )


def parse_sse(body: bytes) -> list[tuple[str | None, dict]]:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RuntimeError("streaming response was not UTF-8") from error

    events = []
    event_name = None
    data_lines = []

    def flush() -> None:
        nonlocal event_name, data_lines
        if not data_lines:
            event_name = None
            return
        data = "\n".join(data_lines)
        event_name_for_record = event_name
        event_name = None
        data_lines = []
        if data == "[DONE]":
            return
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as error:
            raise RuntimeError("streaming event contained invalid JSON") from error
        if not isinstance(payload, dict):
            raise RuntimeError("streaming event data must be a JSON object")
        events.append((event_name_for_record, payload))

    for line in text.splitlines():
        if not line:
            flush()
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    flush()
    return events


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


def response_output_text(response: dict) -> str:
    chunks = []
    for item in response.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text" and isinstance(
                content.get("text"), str
            ):
                chunks.append(content["text"])
    return "".join(chunks)


def validate_continuation(response: dict, expected_text: str) -> None:
    actual = response_output_text(response)
    if expected_text not in actual:
        raise RuntimeError(
            "continuation request did not recall state from previous_response_id"
        )


def validate_expected_model(model: str, expected_model: str) -> None:
    upstream_model = model.rsplit("/", 1)[-1]
    if upstream_model != expected_model:
        raise RuntimeError(
            f"configured model must resolve to {expected_model!r}; got {model!r}"
        )


def validate_reasoning(response: dict) -> None:
    reasoning_items = [
        item for item in response.get("output", []) if item.get("type") == "reasoning"
    ]
    if not reasoning_items:
        raise RuntimeError("response did not include a reasoning output item")


def validate_stream(content_type: str, body: bytes) -> None:
    if "text/event-stream" not in content_type.lower():
        raise RuntimeError(
            "streaming request did not return Content-Type text/event-stream"
        )
    events = parse_sse(body)
    if len(events) < 2:
        raise RuntimeError("streaming response did not contain multiple SSE events")

    completed_response = None
    for event_name, payload in events:
        event_type = payload.get("type") or event_name
        if event_type == "response.completed":
            completed_response = payload.get("response")
            break
    if not isinstance(completed_response, dict):
        observed = [
            {
                "event": event_name,
                "type": payload.get("type"),
                "keys": sorted(payload),
            }
            for event_name, payload in events
        ]
        raise RuntimeError(
            "streaming response did not include response.completed; "
            f"observed events: {observed}"
        )
    if completed_response.get("status") != "completed":
        raise RuntimeError("streaming terminal response was not completed")
    validate_response(completed_response, "streaming request")


def validate_tool_call(response: dict, tool_name: str) -> None:
    calls = [
        item
        for item in response.get("output", [])
        if item.get("type") == "function_call" and item.get("name") == tool_name
    ]
    if not calls:
        raise RuntimeError(f"tool request did not produce a {tool_name!r} function call")
    if not calls[0].get("call_id"):
        raise RuntimeError("tool request function call is missing call_id")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base-url",
        default=os.environ.get("GATEWAY_BASE_URL"),
        help="Gateway API base ending in /v1 (or set GATEWAY_BASE_URL).",
    )
    parser.add_argument(
        "--api-key-env",
        default="GATEWAY_API_KEY",
        help="Environment variable containing the bearer key.",
    )
    parser.add_argument(
        "--header-env",
        action="append",
        default=[],
        metavar="HEADER=ENV_VAR",
        help="Add a secret header from an environment variable; repeat as needed.",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("GATEWAY_MODEL", "gpt-5.6-sol"),
    )
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument(
        "--skip-streaming",
        action="store_true",
        help="Skip the SSE streaming check.",
    )
    parser.add_argument(
        "--include-tool-call",
        action="store_true",
        help="Also require a Responses function-tool call.",
    )
    parser.add_argument(
        "--expected-model",
        help="Require the configured model to resolve to this exact upstream model ID.",
    )
    parser.add_argument(
        "--require-reasoning",
        action="store_true",
        help="Require the initial response to include a reasoning output item.",
    )
    parser.add_argument(
        "--require-model-listed",
        action="store_true",
        help="Require GET /models to expose the exact configured model.",
    )
    parser.add_argument(
        "--model-list-attempts",
        type=int,
        default=1,
        help="Number of exact-model discovery attempts (default: 1).",
    )
    parser.add_argument(
        "--model-list-delay",
        type=float,
        default=10,
        help="Seconds between model discovery attempts (default: 10).",
    )
    args = parser.parse_args()
    if not args.base_url:
        parser.error("--base-url is required")
    if args.model_list_attempts < 1:
        parser.error("--model-list-attempts must be at least 1")
    if args.model_list_delay < 0:
        parser.error("--model-list-delay must be non-negative")
    try:
        extra_headers = parse_header_env(args.header_env, os.environ)
        args.headers = build_headers(
            os.environ.get(args.api_key_env),
            extra_headers,
        )
    except ValueError as error:
        parser.error(str(error))
    return args


def main() -> int:
    args = parse_args()
    if args.expected_model:
        validate_expected_model(args.model, args.expected_model)
    if args.require_model_listed:
        require_listed_model(
            args.base_url,
            args.headers,
            args.model,
            args.timeout,
            args.model_list_attempts,
            args.model_list_delay,
        )
    continuation_value = "CODEX_GATEWAY_7F3A"
    first = send_response(
        args.base_url,
        args.headers,
        {
            "model": args.model,
            "input": (
                f"Remember the exact value {continuation_value}. "
                "Reply with the single word READY."
            ),
            "reasoning": {"effort": "low"},
            "text": {"verbosity": "low"},
            "prompt_cache_key": "enterprise-gateway-contract",
            "store": True,
        },
        args.timeout,
    )
    first_id = validate_response(first, "initial request")
    if args.require_reasoning:
        validate_reasoning(first)

    follow_up = send_response(
        args.base_url,
        args.headers,
        {
            "model": args.model,
            "input": "Reply with only the exact value I asked you to remember.",
            "previous_response_id": first_id,
            "reasoning": {"effort": "low"},
            "text": {"verbosity": "low"},
            "store": False,
        },
        args.timeout,
    )
    validate_response(follow_up, "continuation request")
    validate_continuation(follow_up, continuation_value)

    checks = ["fields", "response shape", "continuation"]
    if args.require_model_listed:
        checks.append("model catalog")
    if args.expected_model:
        checks.append(f"model {args.expected_model}")
    if args.require_reasoning:
        checks.append("reasoning")
    if not args.skip_streaming:
        stream_headers = {**args.headers, "Accept": "text/event-stream"}
        content_type, body = send_request(
            args.base_url,
            stream_headers,
            {
                "model": args.model,
                "input": "Reply with the single word STREAMING.",
                "stream": True,
                "store": False,
            },
            args.timeout,
        )
        validate_stream(content_type, body)
        checks.append("streaming")

    if args.include_tool_call:
        tool_name = "get_contract_value"
        tool_response = send_response(
            args.base_url,
            args.headers,
            {
                "model": args.model,
                "input": f"Call {tool_name} exactly once with value READY.",
                "tools": [
                    {
                        "type": "function",
                        "name": tool_name,
                        "description": "Returns a contract-test value.",
                        "parameters": {
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                            "required": ["value"],
                            "additionalProperties": False,
                        },
                        "strict": True,
                    }
                ],
                "tool_choice": {"type": "function", "name": tool_name},
                "store": False,
            },
            args.timeout,
        )
        validate_response(tool_response, "tool request")
        validate_tool_call(tool_response, tool_name)
        checks.append("function tool call")

    print(f"Responses contract passed: {', '.join(checks)}.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"Responses contract failed: {error}", file=sys.stderr)
        raise SystemExit(1)
