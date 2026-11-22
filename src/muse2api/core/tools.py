"""Emulated tool calling and workspace grounding for muse2api (TODO C3).

muse.ai has no native function calling. This module:
1. Formats OpenAPI / OpenAI tool definitions into clear system prompts for Muse.
2. Formats multi-turn tool history (assistant tool calls and tool responses).
3. Parses tool calls from Muse output (XML <tool_call>, markdown ```json, or bare JSON).
4. Automatically attaches local git and workspace grounding when relevant.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

log = logging.getLogger(__name__)


def _format_tool_lines(tool: dict[str, Any]) -> list[str]:
    """Render the prompt lines for a single tool definition."""
    fn = tool.get("function", tool)
    name = fn.get("name", "")
    desc = fn.get("description", "")
    params = fn.get("parameters", {})
    props = params.get("properties", {}) if isinstance(params, dict) else {}
    req = params.get("required", []) if isinstance(params, dict) else []

    lines = [f"- Tool: `{name}`"]
    if desc:
        lines.append(f"  Description: {desc}")
    if not props:
        return lines

    lines.append("  Parameters:")
    for p_name, p_spec in props.items():
        ptype = p_spec.get("type", "any") if isinstance(p_spec, dict) else "any"
        pdesc = p_spec.get("description", "") if isinstance(p_spec, dict) else ""
        req_tag = " (required)" if p_name in req else ""
        lines.append(f"    * {p_name} ({ptype}{req_tag}): {pdesc}")
    return lines


def format_tools_prompt(tools: list[dict[str, Any]]) -> str:
    """Format OpenAI tool definitions into prompt instructions for muse."""
    if not tools:
        return ""

    lines = [
        "[Host Environment & Workspace Identity]",
        "You are Hermes Agent operating directly on the user's local macOS host machine inside "
        "the local repository workspace (/Volumes/Drive D/Indian Firm/Ai-indian-trading-firm).",
        "NEVER claim that /Volumes/Drive D is not mounted, that you are in a web chat, or that "
        "you are in ~/workspace. You have direct local repository access through the agent harness.",
        "",
        "[Available Tools on Host Machine]",
        "You have access to the following local tools:",
        "",
    ]

    for t in tools:
        lines.extend(_format_tool_lines(t))
        lines.append("")

    lines.extend([
        "[Tool Calling Instructions]",
        "When you need to inspect files, check git status, view commits or diffs, run tests, "
        "or execute commands:",
        "1. You MUST call an available tool. Do NOT assume commands cannot be run or that the "
        "directory is not mounted.",
        "2. To invoke a tool, output a tool call block in EXACTLY this format:",
        "<tool_call>",
        '{"name": "<tool_name>", "arguments": {<arguments>}}',
        "</tool_call>",
        "3. You can also format as a JSON code block:",
        "```json",
        '{"tool_calls": [{"name": "<tool_name>", "arguments": {<arguments>}}]}',
        "```",
        "4. The local runtime will intercept your tool call, execute it on the user's host machine, "
        "and return the tool output to you.",
        "5. If no tool is needed, respond directly with your conversational reply.",
    ])
    return "\n".join(lines)


def _normalize_tool_name(name: str, available_tools: list[str] | None) -> str | None:
    """Map tool name aliases (e.g. bash -> terminal) or check validity."""
    if not name:
        return None
    if not available_tools:
        return name

    names_map = {t.lower(): t for t in available_tools}
    aliases = {
        "bash": "terminal",
        "sh": "terminal",
        "shell": "terminal",
        "exec": "terminal",
        "read": "read_file",
        "write": "write_file",
    }
    for candidate in (name, aliases.get(name.lower())):
        if candidate and candidate.lower() in names_map:
            return names_map[candidate.lower()]
    return None


def _build_call_entry(name: str, args: Any, available_tools: list[str] | None) -> dict[str, Any] | None:
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


def _tool_entries(data: Any) -> list[dict[str, Any]]:
    """Normalize a parsed JSON payload into a list of {name, arguments} entries."""
    if isinstance(data, dict):
        raw_calls = data.get("tool_calls")
        if isinstance(raw_calls, list):
            data = raw_calls
        elif "name" in data and ("arguments" in data or "parameters" in data):
            args = data.get("arguments", data.get("parameters", {}))
            return [{"name": data["name"], "arguments": args}]
        else:
            return []

    if not isinstance(data, list):
        return []
    return [
        {"name": tc["name"], "arguments": tc.get("arguments", {})}
        for tc in data
        if isinstance(tc, dict) and "name" in tc
    ]


def _calls_from_payload(data: Any, available_tools: list[str] | None) -> list[dict[str, Any]]:
    """Build normalized tool calls from a parsed JSON payload."""
    calls: list[dict[str, Any]] = []
    for tc in _tool_entries(data):
        entry = _build_call_entry(tc.get("name", ""), tc.get("arguments", {}), available_tools)
        if entry:
            calls.append(entry)
    return calls


def _extract_from_blocks(
    text: str, available_tools: list[str] | None
) -> tuple[list[dict[str, Any]] | None, str | None] | None:
    """Parse XML <tool_call> and markdown JSON blocks; return (calls, remaining) or None."""
    block_patterns = (
        re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL),
        re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL),
    )
    for pattern in block_patterns:
        matches = list(pattern.finditer(text))
        if not matches:
            continue
        calls: list[dict[str, Any]] = []
        for m in matches:
            try:
                calls.extend(_calls_from_payload(json.loads(m.group(1).strip()), available_tools))
            except Exception:
                pass
        if calls:
            remaining = pattern.sub("", text).strip()
            return calls, remaining or None
    return None


def extract_tool_calls(
    text: str,
    available_tools: list[str] | None = None,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Parse tool calls from model output.

    Supports:
    1. <tool_call>{"name": ..., "arguments": ...}</tool_call>
    2. ```json ... ``` with tool_calls or function schema
    3. Bare JSON with "tool_calls" or ("name" and "arguments")

    Returns:
        (tool_calls, remaining_text) if valid tool calls found,
        (None, original_text) otherwise.
    """
    if not text:
        return None, text

    found = _extract_from_blocks(text, available_tools)
    if found is not None:
        return found

    # 3. Bare JSON
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            calls = _calls_from_payload(json.loads(stripped), available_tools)
            if calls:
                return calls, None
        except Exception:
            pass

    return None, text


def _load_workspace_helper() -> tuple[Any, Any]:
    """Import workspace_helper, retrying from the repo root on first failure."""
    try:
        from workspace_helper import get_default_workspace, get_git_context

        return get_default_workspace, get_git_context
    except ImportError:
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parents[3]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        try:
            from workspace_helper import get_default_workspace, get_git_context
        except ImportError:
            return None, None
        return get_default_workspace, get_git_context


def _last_user_query(messages: list[dict[str, Any]]) -> str:
    """Return the most recent user message rendered as plain text."""
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return " ".join(part.get("text", "") for part in c if isinstance(part, dict))
    return ""


def get_workspace_grounding(messages: list[dict[str, Any]]) -> str:
    """Fetch relevant local git & workspace context for the user query."""
    get_default_workspace, get_git_context = _load_workspace_helper()
    if get_default_workspace is None:
        return ""

    user_query = _last_user_query(messages)
    if not user_query:
        return ""

    try:
        git_ctx = get_git_context(get_default_workspace(), user_query)
        return git_ctx if git_ctx else ""
    except Exception as e:
        log.debug("Workspace grounding fetch failed: %s", e)
        return ""
