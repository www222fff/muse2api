"""Live check: chat replies must preserve markdown.

Three cases per model, each run non-streaming and streaming:

1. A document with four exact headings — every heading must appear exactly
   once, *with* its ``## `` prefix.
2. A structured reply — a bulleted list of exactly 3 items, a numbered list
   of 3 items, one fenced python block containing a line that starts with
   ``## ``, and a 2x2 table. PASS requires: zero zero-width characters, no
   ``- -`` and no ``-<U+200B>``, each item text exactly once, an even number
   of ``` fences, and a table separator row.

The structural checks run on the streamed reply as well as the non-stream
one (the two are independent generations, so they are never compared with
each other). A third, deterministic case per model — a system message
demanding exactly the word PINEAPPLE — asserts both replies strip() to
exactly "PINEAPPLE". Both texts are printed on FAIL.

Usage:
    python scripts/verify_markdown.py --key <API_KEY>
    (or set MUSE2API_KEY; --base overrides http://127.0.0.1:18610)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request

HEADINGS = ("## Background / Goal", "## Scope (in / out)",
            "## Functional requirements", "## Acceptance criteria")
ZERO_WIDTH = "\u200b\u200c\u200d\ufeff"
BULLETS = ("red apple", "green pear", "blue plum")
STEPS = ("first step", "second step", "third step")
PROMPT_HEADINGS = (
    "Write a short specification document. Use exactly these four headings, "
    "in this order, and write one or two sentences under each heading:\n"
    + "\n".join(HEADINGS)
)
PROMPT_STRUCTURE = (
    "Reply with exactly the following, nothing else:\n"
    "1. A bulleted list of exactly these 3 items: red apple, green pear, blue plum.\n"
    "2. A numbered list of exactly these 3 items: first step, second step, third step.\n"
    "3. One fenced python code block containing exactly this line: ## not a heading\n"
    "4. A markdown table with 2 columns (Name, Colour) and 2 rows: "
    "apple/red, pear/green."
)
PINEAPPLE_MESSAGES = [
    {"role": "system",
     "content": "Reply with exactly the single word PINEAPPLE and nothing else"},
    {"role": "user", "content": "go"},
]
_TABLE_SEPARATOR = re.compile(r"\|\s*:?-{3,}:?\s*\|")


def _request(base: str, key: str, payload: dict) -> urllib.request.Request:
    return urllib.request.Request(
        base.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(payload).encode(), method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})


def chat(base: str, key: str, model: str, messages: list[dict]) -> str:
    body = {"model": model, "messages": messages}
    with urllib.request.urlopen(_request(base, key, body), timeout=600) as resp:
        payload = json.loads(resp.read())
    return payload["choices"][0]["message"]["content"]


def chat_streamed(base: str, key: str, model: str, messages: list[dict]) -> str:
    body = {"model": model, "stream": True, "messages": messages}
    parts: list[str] = []
    with urllib.request.urlopen(_request(base, key, body), timeout=600) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            chunk = json.loads(line[6:])
            for choice in chunk.get("choices", []):
                parts.append(choice.get("delta", {}).get("content", ""))
    return "".join(parts)


def _zero_width_count(text: str) -> int:
    return sum(text.count(char) for char in ZERO_WIDTH)


def check_pineapple(content: str) -> list[str]:
    problems = []
    if content.strip() != "PINEAPPLE":
        problems.append(f"reply is not exactly 'PINEAPPLE': {content.strip()!r}")
    if content.count("PINEAPPLE") != 1:
        problems.append(f"PINEAPPLE appears {content.count('PINEAPPLE')} times")
    return problems


def check_headings(content: str) -> list[str]:
    problems = []
    counts = {heading: content.count(heading) for heading in HEADINGS}
    if any(count != 1 for count in counts.values()):
        problems.append(f"heading counts {counts}")
    if _zero_width_count(content):
        problems.append(f"{_zero_width_count(content)} zero-width chars")
    return problems


def check_structure(content: str) -> list[str]:
    problems = []
    if _zero_width_count(content):
        problems.append(f"{_zero_width_count(content)} zero-width chars")
    if "- -" in content:
        problems.append("contains '- -' (duplicated list marker)")
    if "-\u200b" in content:
        problems.append("contains '-<U+200B>' (renderer bullet artifact)")
    for item in (*BULLETS, *STEPS):
        if content.count(item) != 1:
            problems.append(f"item {item!r} appears {content.count(item)} times")
    fences = content.count("```")
    if fences == 0 or fences % 2:
        problems.append(f"{fences} code fences (need an even number >= 2)")
    if "## not a heading" not in content:
        problems.append("fenced block's '## ' line missing")
    if not _TABLE_SEPARATOR.search(content):
        problems.append("table separator row missing")
    return problems


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
        for label, messages, check in (
            ("headings", [{"role": "user", "content": PROMPT_HEADINGS}], check_headings),
            ("structure", [{"role": "user", "content": PROMPT_STRUCTURE}], check_structure),
            ("pineapple", PINEAPPLE_MESSAGES, check_pineapple),
        ):
            content = chat(args.base, args.key, model, messages)
            streamed = chat_streamed(args.base, args.key, model, messages)
            # The two replies are independent generations: each is checked on
            # its own structure, never compared with the other.
            problems = ([f"non-stream: {p}" for p in check(content)]
                        + [f"stream: {p}" for p in check(streamed)])
            ok = not problems
            failures += 0 if ok else 1
            print(f"[{'PASS' if ok else 'FAIL'}] model={model} case={label}"
                  + ("" if ok else f" problems={problems}"))
            if not ok:
                print("non-stream reply was:\n" + content
                      + "\nstreamed reply was:\n" + streamed + "\n---")
    print("ALL PASS" if failures == 0 else f"{failures} case(s) FAILED")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
