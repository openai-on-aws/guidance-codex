#!/usr/bin/env python3
"""Check that local Markdown link targets exist."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys
from urllib.parse import unquote


LINK_PATTERN = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
EXTERNAL_SCHEMES = ("http://", "https://", "mailto:", "tel:", "data:")


def local_target(raw_target: str) -> str | None:
    target = raw_target.strip()
    if target.startswith("<") and ">" in target:
        target = target[1 : target.index(">")]
    else:
        target = target.split(maxsplit=1)[0]
    if (
        not target
        or target.startswith("#")
        or target.startswith(EXTERNAL_SCHEMES)
        or "<" in target
        or ">" in target
    ):
        return None
    target = target.split("#", 1)[0].split("?", 1)[0]
    return unquote(target) or None


def missing_links(repo_root: Path) -> list[str]:
    missing = []
    for source in sorted(repo_root.rglob("*.md")):
        if ".git" in source.parts:
            continue
        text = source.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), 1):
            for match in LINK_PATTERN.finditer(line):
                target = local_target(match.group(1))
                if not target:
                    continue
                target_path = Path(target)
                if target_path.is_absolute() and target_path.is_relative_to(repo_root):
                    candidate = target_path
                elif target.startswith("/"):
                    candidate = repo_root / target.lstrip("/")
                else:
                    candidate = source.parent / target
                if not candidate.resolve().exists():
                    relative_source = source.relative_to(repo_root)
                    missing.append(f"{relative_source}:{line_number}: {target}")
    return missing


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "root",
        nargs="?",
        default=Path(__file__).resolve().parents[2],
        type=Path,
    )
    args = parser.parse_args()
    missing = missing_links(args.root.resolve())
    if missing:
        print("Missing local Markdown links:", file=sys.stderr)
        for item in missing:
            print(f"  {item}", file=sys.stderr)
        return 1
    print("Local Markdown links passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
