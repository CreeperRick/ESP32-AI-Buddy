#!/usr/bin/env python3
"""Send a text prompt to a running AI Buddy server (no Cardputer required)."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--token", default="")
    parser.add_argument("text", nargs="*", help="Prompt to send")
    args = parser.parse_args()
    prompt = " ".join(args.text) or "Hello, who are you?"
    req = urllib.request.Request(
        args.url.rstrip("/") + "/api/chat",
        data=json.dumps({"text": prompt}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if args.token:
        req.add_header("Authorization", f"Bearer {args.token}")
    try:
        with urllib.request.urlopen(req, timeout=180) as response:
            print(json.loads(response.read().decode())["reply"])
    except urllib.error.URLError as exc:
        print(f"Request failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
