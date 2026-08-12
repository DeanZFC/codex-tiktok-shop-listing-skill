#!/usr/bin/env python3
"""Open sales-ranked product URLs in Microsoft Edge without shell interpolation."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse


def validate_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise argparse.ArgumentTypeError(f"Invalid HTTP(S) URL: {value}")
    return value


def windows_edge() -> str:
    candidates = [
        shutil.which("msedge.exe"),
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
        / "Microsoft"
        / "Edge"
        / "Application"
        / "msedge.exe",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
        / "Microsoft"
        / "Edge"
        / "Application"
        / "msedge.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    raise OSError("Microsoft Edge executable was not found")


def edge_commands(urls: list[str]) -> list[list[str]]:
    if sys.platform == "darwin":
        return [["open", "-a", "Microsoft Edge", url] for url in urls]
    if os.name == "nt":
        edge = windows_edge()
        return [[edge, url] for url in urls]
    edge = shutil.which("microsoft-edge") or shutil.which("microsoft-edge-stable")
    if not edge:
        raise OSError("Microsoft Edge executable was not found")
    return [[edge, url] for url in urls]


def main() -> int:
    parser = argparse.ArgumentParser(description="Open ranked product URLs in Microsoft Edge")
    parser.add_argument("urls", nargs="+", type=validate_url)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        commands = edge_commands(args.urls)
        if not args.dry_run:
            for command in commands:
                if sys.platform == "darwin":
                    result = subprocess.run(command, capture_output=True, encoding="utf-8", check=False)
                    if result.returncode != 0:
                        raise OSError(result.stderr.strip() or "Microsoft Edge could not be launched")
                else:
                    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(
            json.dumps(
                {
                    "browser": "edge",
                    "launch_attempted": not args.dry_run,
                    "ranked_urls": [
                        {"rank": index, "url": url} for index, url in enumerate(args.urls, start=1)
                    ],
                    "commands": commands if args.dry_run else None,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except OSError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
