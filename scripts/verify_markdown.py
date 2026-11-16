"""Live check: chat replies must preserve markdown headings.

Sends a request for a document with four exact headings through the running
gateway and, for each model, prints how many of the headings appear in the
reply *with* their ``## `` prefix. Every heading must appear exactly once.

Usage:
    python scripts/verify_markdown.py --key <API_KEY>
    (or set MUSE2API_KEY; --base overrides http://127.0.0.1:18610)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

HEADINGS = ("## Background / Goal", "## Scope (in / out)",
            "## Functional requirements", "## Acceptance criteria")
ZERO_WIDTH = "\u200b\u200c\u200d\ufeff"
PROMPT = (
    "Write a short specification document. Use exactly these four headings, "
    "in this order, and write one or two sentences under each heading:\n"
    + "\n".join(HEADINGS)
)


def chat(base: str, key: str, model: str) -> str:
    body = json.dumps({"model": model,
                       "messages": [{"role": "user", "content": PROMPT}]}).encode()
    req = urllib.request.Request(
        base.rstrip("/") + "/v1/chat/completions", data=body, method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as resp:
        payload = json.loads(resp.read())
    return payload["choices"][0]["message"]["content"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:18610")
    parser.add_argument("--key", default=os.environ.get("MUSE2API_KEY", ""))
    args = parser.parse_args()
    if not args.key:
        print("No API key: pass --key or set MUSE2API_KEY.")
        return 2

    failures = 0
    for model in ("gpt-4o-mini", "claude-sonnet-4", "gpt-5"):
        content = chat(args.base, args.key, model)
        counts = {heading: content.count(heading) for heading in HEADINGS}
        zw = sum(content.count(char) for char in ZERO_WIDTH)
        ok = all(count == 1 for count in counts.values()) and zw == 0
        failures += 0 if ok else 1
        print(f"[{'PASS' if ok else 'FAIL'}] model={model} headings={counts} "
              f"zero_width_chars={zw}")
        if not ok:
            print("reply was:\n" + content + "\n---")
    print("ALL PASS" if failures == 0 else f"{failures} model(s) FAILED")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
