"""Emulated tool calling and workspace grounding for muse2api (milestone C3).

muse.ai has no native function calling. This module:
1. Formats OpenAPI / OpenAI tool definitions into clear system prompts for Muse.
2. Formats multi-turn tool history (assistant tool calls and tool responses).
3. Parses tool calls from Muse output (XML <tool_call>, markdown ```json, or bare JSON).
4. Automatically attaches local git and workspace grounding when relevant.
5. Synthesizes tool calls if Muse inadvertently outputs a web-chat refusal for an executable task.
6. Sanitizes web-chat disclaimers from responses.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import TYPE_CHECKING, Any, TypeAlias, cast

if TYPE_CHECKING:
    from collections.abc import Callable

log = logging.getLogger(__name__)

_DICT_STR_ANY: TypeAlias = dict[str, Any]
_LIST_ANY: TypeAlias = list[Any]

_TOOL_ALIASES = {
    "bash": "terminal",
    "sh": "terminal",
    "shell": "terminal",
    "exec": "terminal",
    "cmd": "terminal",
    "run": "terminal",
    "read": "read_file",
    "cat": "read_file",
    "write": "write_file",
}

_XML_OPEN = "<tool_call>"
_XML_CLOSE = "</tool_call>"
_FENCE = "```"

def _format_tool_lines(tool: dict[str, Any]) -> list[str]:
    """Render the prompt lines for a single tool definition."""
    fn = cast(_DICT_STR_ANY, tool.get("function", tool))
    params = cast(_DICT_STR_ANY, fn.get("parameters", {}))
    props = cast(_DICT_STR_ANY, params.get("properties", {}))
    req_raw: Any = params.get("required", [])
    req_list: list[Any] = []
    if isinstance(req_raw, list):
        req_list = cast(_LIST_ANY, req_raw)

    lines = [f"- Tool: `{fn.get('name', '')}`"]
    desc = fn.get("description", "")
    if desc:
        lines.append(f"  Description: {desc}")
    if not props:
        return lines

    lines.append("  Parameters:")
    for p_name, p_spec in props.items():
        spec = cast(_DICT_STR_ANY, p_spec) if isinstance(p_spec, dict) else {}
        ptype = spec.get("type", "any")
        pdesc = spec.get("description", "")
        req_tag = " (required)" if p_name in req_list else ""
        lines.append(f"    * {p_name} ({ptype}{req_tag}): {pdesc}")
    return lines


def format_tools_prompt(tools: list[dict[str, Any]]) -> str:
    """Format OpenAI tool definitions into prompt instructions for muse."""
    if not tools:
        return ""

    lines = [
        "[Available Tools]",
        "",
        "Tools available to invoke:",
        "",
    ]

    for t in tools:
        lines.extend(_format_tool_lines(t))
        lines.append("")

    lines.extend([
        "[Tool Calling Instructions]",
        "When a tool is needed to answer the user:",
        "1. Invoke an available tool rather than guessing its result.",
        "2. To invoke a tool, output a tool call block in EXACTLY this format:",
        "<tool_call>",
        '{"name": "<tool_name>", "arguments": {<arguments>}}',
        "</tool_call>",
        "3. You can also format as a JSON code block:",
        "```json",
        '{"tool_calls": [{"name": "<tool_name>", "arguments": {<arguments>}}]}',
        "```",
        ("4. The client runs the tool and returns its output in a `[Tool Output]` "
         "block; base your answer on that output."),
        "5. When all needed information is already in context, answer directly.",
    ])
    return "\n".join(lines)


def _normalize_tool_name(name: str, available_tools: list[str] | None) -> str | None:
    """Map tool name aliases (e.g. bash -> terminal) or check validity."""
    if not name:
        return None
    if not available_tools:
        return name

    names_map = {t.lower(): t for t in available_tools}
    low = name.lower()
    if low in names_map:
        return names_map[low]
    alias = _TOOL_ALIASES.get(low)
    if alias is not None and alias.lower() in names_map:
        return names_map[alias.lower()]
    return None


def _safe_json_loads(text: str) -> Any | None:
    """Parse JSON without raising; returns None on malformed input."""
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _build_call_entry(
    name: str, args: Any, available_tools: list[str] | None,
) -> dict[str, Any] | None:
    """Build an OpenAI-style tool-call entry, or None when the tool is unknown."""
    norm_name = _normalize_tool_name(name, available_tools)
    if not norm_name:
        return None

    if isinstance(args, dict):
        args_str = json.dumps(args, ensure_ascii=False)
    elif isinstance(args, str):
        args_str = args
    else:
        args_str = json.dumps(args or {})

    return {
        "id": f"call_{uuid.uuid4().hex[:16]}",
        "type": "function",
        "function": {
            "name": norm_name,
            "arguments": args_str,
        },
    }


def _single_tool_entry(payload: dict[str, Any]) -> list[dict[str, Any]] | None:
    """A bare {name, arguments} payload, or None when it carries tool_calls."""
    raw_calls = payload.get("tool_calls")
    if isinstance(raw_calls, list) or "name" not in payload:
        return None
    args: Any = payload.get("arguments", payload.get("parameters", {}))
    return [{"name": payload["name"], "arguments": args}]


def _entry_from_item(tc: Any) -> dict[str, Any] | None:
    """Normalize one tool-call item; None when it is not usable."""
    if not isinstance(tc, dict) or "name" not in tc:
        return None
    t = cast(_DICT_STR_ANY, tc)
    return {
        "name": t["name"],
        "arguments": t.get("arguments", t.get("parameters", {})),
    }


def _tool_list_items(data: Any) -> list[Any]:
    """Coerce a payload into a list of tool-call items."""
    if isinstance(data, list):
        return cast(_LIST_ANY, data)
    if not isinstance(data, dict):
        return []
    payload = cast(_DICT_STR_ANY, data)
    raw_calls = payload.get("tool_calls")
    return cast(_LIST_ANY, raw_calls) if isinstance(raw_calls, list) else []


def _tool_entries(data: Any) -> list[dict[str, Any]]:
    """Normalize a parsed JSON payload into a list of {name, arguments} entries."""
    if isinstance(data, dict):
        single = _single_tool_entry(cast(_DICT_STR_ANY, data))
        if single is not None:
            return single
    return [
        entry
        for entry in (_entry_from_item(tc) for tc in _tool_list_items(data))
        if entry is not None
    ]


def _calls_from_payload(
    data: Any, available_tools: list[str] | None,
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for tc in _tool_entries(data):
        c = _build_call_entry(tc.get("name", ""), tc.get("arguments", {}), available_tools)
        if c:
            calls.append(c)
    return calls


def _xml_blocks(text: str) -> list[tuple[int, int]]:
    """Non-overlapping spans of ``<tool_call>...</tool_call>`` blocks.

    Deterministic linear scan (``str.find``) instead of a lazy-DOTALL regex,
    which is O(n^2) on input with many unclosed tags (backtracking).
    """
    spans: list[tuple[int, int]] = []
    pos = 0
    while True:
        start = text.find(_XML_OPEN, pos)
        if start < 0:
            break
        end = text.find(_XML_CLOSE, start + len(_XML_OPEN))
        if end < 0:
            break
        spans.append((start, end + len(_XML_CLOSE)))
        pos = spans[-1][1]
    return spans


def _json_code_blocks(text: str) -> list[tuple[int, int]]:
    """Non-overlapping spans of ```json … ``` blocks whose body is JSON.

    Deterministic linear scan (``str.find``) — no lazy/overlapping quantifiers.
    """
    spans: list[tuple[int, int]] = []
    pos = 0
    while True:
        start = text.find(_FENCE, pos)
        if start < 0:
            break
        body_start = start + len(_FENCE)
        if text.startswith("json", body_start):
            tag_end = body_start + len("json")
            if tag_end >= len(text) or text[tag_end].isspace():
                body_start = tag_end
        end = text.find(_FENCE, body_start)
        if end < 0:
            break
        if text[body_start:end].strip().startswith("{"):
            spans.append((start, end + len(_FENCE)))
        pos = end + len(_FENCE)
    return spans


def _xml_content(text: str, start: int, end: int) -> str:
    return text[start + len(_XML_OPEN):end - len(_XML_CLOSE)].strip()


def _fence_content(text: str, start: int, end: int) -> str:
    inner = text[start + len(_FENCE):end - len(_FENCE)].strip()
    if inner.startswith("json"):
        inner = inner[len("json"):].lstrip()
    return inner


def _extract_blocks(
    locate: Callable[[str], list[tuple[int, int]]],
    content_at: Callable[[str, int, int], str],
    text: str,
    available_tools: list[str] | None,
) -> tuple[list[dict[str, Any]], str] | None:
    """Parse every located block; return (calls, remaining) or None."""
    spans = locate(text)
    if not spans:
        return None
    calls: list[dict[str, Any]] = []
    remaining_parts: list[str] = []
    prev = 0
    for start, end in spans:
        payload = _safe_json_loads(content_at(text, start, end))
        if payload is not None:
            calls.extend(_calls_from_payload(payload, available_tools))
        remaining_parts.append(text[prev:start])
        prev = end
    remaining_parts.append(text[prev:])
    if not calls:
        return None
    return calls, "".join(remaining_parts).strip()


def _extract_from_blocks(
    text: str, available_tools: list[str] | None,
) -> tuple[list[dict[str, Any]] | None, str | None] | None:
    """Parse XML <tool_call> and markdown JSON blocks; return (calls, remaining) or None."""
    for locate, content_at in (
        (_xml_blocks, _xml_content),
        (_json_code_blocks, _fence_content),
    ):
        found = _extract_blocks(locate, content_at, text, available_tools)
        if found is not None:
            calls, remaining = found
            return calls, remaining or None
    return None


def _bare_json_calls(
    text: str, available_tools: list[str] | None,
) -> list[dict[str, Any]] | None:
    """Tool calls from a top-level JSON object, or None when not one."""
    stripped = text.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return None
    payload = _safe_json_loads(stripped)
    if payload is None:
        return None
    calls = _calls_from_payload(payload, available_tools)
    return calls or None


def extract_tool_calls(
    text: str,
    available_tools: list[str] | None = None,
    user_query: str = "",
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Parse tool calls from model output."""
    if not text:
        return None, text

    found = _extract_from_blocks(text, available_tools)
    if found is not None:
        return found

    calls = _bare_json_calls(text, available_tools)
    if calls:
        return calls, None

    return None, text
