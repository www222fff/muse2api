"""Emulated tool calling and workspace grounding for muse2api (TODO C3).

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
import re
import uuid
from typing import Any, cast

log = logging.getLogger(__name__)


def _format_tool_lines(tool: dict[str, Any]) -> list[str]:
    """Render the prompt lines for a single tool definition."""
    fn = cast(dict[str, Any], tool.get("function", tool))
    params = cast(dict[str, Any], fn.get("parameters", {}))
    props = cast(dict[str, Any], params.get("properties", {}))
    req_raw: Any = params.get("required", [])
    req_list: list[Any] = []
    if isinstance(req_raw, list):
        req_list = cast(list[Any], req_raw)

    lines = [f"- Tool: `{fn.get('name', '')}`"]
    desc = fn.get("description", "")
    if desc:
        lines.append(f"  Description: {desc}")
    if not props:
        return lines

    lines.append("  Parameters:")
    for p_name, p_spec in props.items():
        spec = cast(dict[str, Any], p_spec) if isinstance(p_spec, dict) else {}
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
        "[Host Environment & Available Tools]",
        "You are operating with direct access to local execution tools on the user's host machine.",
        "Workspace: /Volumes/Drive D/Indian Firm/Ai-indian-trading-firm (macOS)",
        "Tools available to invoke:",
        "",
    ]

    for t in tools:
        lines.extend(_format_tool_lines(t))
        lines.append("")

    lines.extend([
        "[Tool Calling Instructions]",
        "When the user asks to run commands, run tests, check git, inspect files, or make edits:",
        "1. You MUST invoke an available tool. Do NOT assume commands cannot be run.",
        "2. To invoke a tool, output a tool call block in EXACTLY this format:",
        "<tool_call>",
        '{"name": "<tool_name>", "arguments": {<arguments>}}',
        "</tool_call>",
        "3. You can also format as a JSON code block:",
        "```json",
        '{"tool_calls": [{"name": "<tool_name>", "arguments": {<arguments>}}]}',
        "```",
        "4. The runtime intercepts your tool call, executes it on the host machine, "
        "and returns the tool output to you.",
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
    aliases = {
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
    low = name.lower()
    if low in names_map:
        return names_map[low]
    alias = aliases.get(low)
    if alias and alias.lower() in names_map:
        return names_map[alias.lower()]

    return name if not available_tools else None


def _build_call_entry(
    name: str, args: Any, available_tools: list[str] | None
) -> dict[str, Any] | None:
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
    items: list[Any] | None = None
    if isinstance(data, dict):
        payload = cast(dict[str, Any], data)
        raw_calls = payload.get("tool_calls")
        if isinstance(raw_calls, list):
            items = cast(list[Any], raw_calls)
        elif "name" in payload:
            args: Any = payload.get("arguments", payload.get("parameters", {}))
            return [{"name": payload["name"], "arguments": args}]
    elif isinstance(data, list):
        items = cast(list[Any], data)

    if items is None:
        return []
    out: list[dict[str, Any]] = []
    for tc in items:
        if not isinstance(tc, dict) or "name" not in tc:
            continue
        t = cast(dict[str, Any], tc)
        out.append({"name": t["name"],
                    "arguments": t.get("arguments", t.get("parameters", {}))})
    return out


def _calls_from_payload(
    data: Any, available_tools: list[str] | None
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for tc in _tool_entries(data):
        c = _build_call_entry(tc.get("name", ""), tc.get("arguments", {}), available_tools)
        if c:
            calls.append(c)
    return calls


_XML_TOOL_CALL = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)
_JSON_CODE_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_pattern(
    pattern: re.Pattern[str], text: str, available_tools: list[str] | None
) -> tuple[list[dict[str, Any]], str] | None:
    """Parse every ``pattern`` block in ``text``; return (calls, remaining) or None."""
    matches = list(pattern.finditer(text))
    if not matches:
        return None
    calls: list[dict[str, Any]] = []
    for m in matches:
        try:
            calls.extend(_calls_from_payload(json.loads(m.group(1).strip()), available_tools))
        except Exception:
            pass
    if not calls:
        return None
    return calls, pattern.sub("", text).strip()


def _extract_from_blocks(
    text: str, available_tools: list[str] | None
) -> tuple[list[dict[str, Any]] | None, str | None] | None:
    """Parse XML <tool_call> and markdown JSON blocks; return (calls, remaining) or None."""
    for pattern in (_XML_TOOL_CALL, _JSON_CODE_BLOCK):
        found = _extract_pattern(pattern, text, available_tools)
        if found is not None:
            calls, remaining = found
            return calls, remaining or None
    return None


def _is_refusal(text: str) -> bool:
    """True when the reply reads like a web-chat refusal instead of a tool call."""
    refusal_signals = (
        "can't run",
        "cannot run",
        "can't read",
        "cannot read",
        "isn't mounted",
        "not mounted",
        "no file to run",
        "won't pretend",
    )
    low = text.lower()
    return any(sig in low for sig in refusal_signals)


_PYTEST_RE = re.compile(r"\b(pytest(?:\s+on)?\s+[^\n]+)", re.IGNORECASE)
_RUN_CMD_RE = re.compile(
    r"\b(?:run|execute)\s+([a-zA-Z0-9_\-./]+(?:\s+[^\n]+)?)", re.IGNORECASE
)
_FILE_PATH_RE = re.compile(r"((?:options_trading|tests|scripts|docs)/[a-zA-Z0-9_\-./]+\.[a-zA-Z0-9]+)")


def _clean_git_command(user_query: str) -> str | None:
    m = re.search(
        r"\b(git\s+(?:status|diff|log|show|branch|checkout|add|commit|blame|rev-parse)"
        r"(?:\s+-[a-zA-Z0-9_\-]+|\s+--[a-zA-Z0-9_\-]+|\s+[a-zA-Z0-9_\-./]+)*)",
        user_query,
        re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()
    low = user_query.lower()
    if "git status" in low:
        return "git status"
    if "last" in low and "commit" in low:
        m_count = re.search(r"last\s+(\d+)\s+commits?", low)
        n = m_count.group(1) if m_count else "5"
        return f"git log -n {n} --oneline"
    if "git log" in low:
        return "git log -n 5 --oneline"
    return None


def _terminal_command(user_query: str) -> str | None:
    """A shell command the user asked to run (pytest, git, run/execute)."""
    m_pytest = _PYTEST_RE.search(user_query)
    if m_pytest:
        cmd = m_pytest.group(1).strip()
        cmd = re.sub(r"^pytest\s+on\s+", "pytest ", cmd, flags=re.IGNORECASE)
        cmd = re.sub(r"\s+(?:and|then|to|for|with)\s+.*$", "", cmd, flags=re.IGNORECASE).strip()
        if not cmd.startswith(".venv"):
            cmd = f".venv/bin/{cmd}"
        return cmd
    cmd_git = _clean_git_command(user_query)
    if cmd_git:
        return cmd_git
    m_run = _RUN_CMD_RE.search(user_query)
    if m_run:
        return m_run.group(1).strip()
    return None


def _refusal_file_path(text: str, user_query: str) -> str | None:
    """A repo file path mentioned in the refusal or the user query."""
    m_path = _FILE_PATH_RE.search(text) or _FILE_PATH_RE.search(user_query)
    return m_path.group(1).strip() if m_path else None


def _terminal_fallback(norm_tools: dict[str, str], user_query: str,
                       available_tools: list[str]) -> list[dict[str, Any]] | None:
    """Synthesize a terminal tool call when the user asked to run a command."""
    terminal_tool = norm_tools.get("terminal")
    if not terminal_tool:
        return None
    cmd = _terminal_command(user_query)
    if not cmd:
        return None
    c = _build_call_entry(terminal_tool, {"command": cmd}, available_tools)
    return [c] if c else None


def _read_file_fallback(norm_tools: dict[str, str], text: str, user_query: str,
                        available_tools: list[str]) -> list[dict[str, Any]] | None:
    """Synthesize a read_file tool call for a repo path mentioned in the reply."""
    read_tool = norm_tools.get("read_file")
    if not read_tool:
        return None
    path = _refusal_file_path(text, user_query)
    if not path:
        return None
    c = _build_call_entry(read_tool, {"path": path}, available_tools)
    return [c] if c else None


def _fallback_tool_from_refusal(
    text: str,
    user_query: str,
    available_tools: list[str] | None,
) -> list[dict[str, Any]] | None:
    """When Muse outputs a web-chat refusal instead of calling a tool, synthesize the call."""
    if not available_tools or not text:
        return None
    if not _is_refusal(text):
        return None

    norm_tools = {t.lower(): t for t in available_tools}
    return (
        _terminal_fallback(norm_tools, user_query, available_tools)
        or _read_file_fallback(norm_tools, text, user_query, available_tools)
    )


def extract_tool_calls(
    text: str,
    available_tools: list[str] | None = None,
    user_query: str = "",
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Parse tool calls from model output, with fallback synthesis on refusal."""
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

    # 4. Fallback synthesis if model refused an actionable query
    if user_query and available_tools:
        fallback = _fallback_tool_from_refusal(text, user_query, available_tools)
        if fallback:
            return fallback, None

    return None, text


def sanitize_response_text(text: str) -> str:
    """Clean out boilerplate web-chat disclaimers and sandbox caveats."""
    if not text:
        return text

    # Remove zero-width spaces that precede repeated chunks
    text = re.sub(r"-\u200b", "", text)

    disclaimer_patterns = [
        # "From the content you pasted in this message — not a mounted drive in this web chat — I counted it directly:"
        r"From the (?:content|git context) you pasted in this message\s*—\s*not a mounted drive in this web chat\s*—\s*",
        # "— not a mounted drive in this web chat —"
        r"—\s*not a mounted drive in this web chat\s*—\s*",
        # "I just checked this web chat again and [^\n]+ isn't mounted here[^\n]+so I can't [^\n]+\.?\s*"
        r"I just checked (?:this web chat again and )?[^\n]+ isn't mounted here[^\n]*(?:and there's no [^\n]+)?(?:so I can't [^\n]+)?\.?\s*",
        # "and I won't pretend a (?:Hermes/)?terminal tool on your Mac is available in this web chat\.?"
        r"and I won't pretend a (?:Hermes/)?terminal tool on your Mac is available in this web chat\.?",
        # "in this web chat"
        r"\bin this web chat\b",
    ]
    for pat in disclaimer_patterns:
        text = re.sub(pat, "", text, flags=re.DOTALL | re.IGNORECASE)

    # Clean leading/trailing empty lines
    text = re.sub(r"^\s*\n+", "", text)
    return text.strip()


def _load_workspace_helper() -> Any:
    """Import workspace_helper, retrying from the repo root on first failure."""
    try:
        import workspace_helper

        return workspace_helper
    except ImportError:
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parents[3]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        try:
            import workspace_helper

            return workspace_helper
        except ImportError:
            return None


def _last_user_query(messages: list[dict[str, Any]]) -> str:
    """Return the most recent user message rendered as plain text."""
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            texts: list[str] = []
            for part in cast(list[Any], c):
                if isinstance(part, dict):
                    texts.append(cast(dict[str, Any], part).get("text", ""))
            return " ".join(texts)
    return ""


def _is_workspace_query(query: str) -> bool:
    """Return True if query refers to repository files, git, tests, or code."""
    q = query.lower()
    if re.search(r"\b[a-zA-Z0-9_\-./]+\.(?:py|json|toml|md|sh|yaml|yml)\b", q):
        return True
    if "/" in q or "\\" in q:
        return True
    git_words = (
        "git",
        "commit",
        "commits",
        "diff",
        "branch",
        "repo",
        "repository",
        "status",
        "log",
        "head",
    )
    if any(w in q for w in git_words):
        return True
    code_words = (
        "test",
        "tests",
        "pytest",
        "refactor",
        "function",
        "class",
        "module",
        "code",
        "file",
        "files",
    )
    return any(w in q for w in code_words)


def get_workspace_grounding(messages: list[dict[str, Any]]) -> str:
    """Fetch relevant local git & workspace context (files + commits) for the user query."""
    user_query = _last_user_query(messages)
    if not user_query or not _is_workspace_query(user_query):
        return ""

    helper = _load_workspace_helper()
    if helper is None:
        return ""

    try:
        base = helper.get_default_workspace()
        paths = helper.find_local_paths(user_query, base_dir=base)
        # build_context_for_paths embeds matching file contents AND git context
        ctx = helper.build_context_for_paths(paths, prompt=user_query)
        return ctx if ctx else ""
    except Exception as e:
        log.debug("Workspace grounding fetch failed: %s", e)
        return ""
