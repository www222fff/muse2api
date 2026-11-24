from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from muse2api.core.prompt import flatten_messages
from muse2api.core.tools import (
    _clean_git_command,
    extract_tool_calls,
    format_tools_prompt,
    get_workspace_grounding,
    sanitize_response_text,
    synthesize_tool_response,
)


def test_format_tools_prompt():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "terminal",
                "description": "Execute a shell command",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "The command to run"},
                    },
                    "required": ["command"],
                },
            },
        },
    ]
    prompt = format_tools_prompt(tools)
    assert "terminal" in prompt
    assert "Execute a shell command" in prompt
    assert "command (string (required))" in prompt
    assert "<tool_call>" in prompt


def test_extract_tool_calls_xml():
    text = (
        "I will check the git history.\n"
        "<tool_call>\n"
        '{"name": "terminal", "arguments": {"command": "git log -5"}}\n'
        "</tool_call>"
    )
    calls, remaining = extract_tool_calls(text, ["terminal"])
    assert calls is not None
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "terminal"
    args = json.loads(calls[0]["function"]["arguments"])
    assert args["command"] == "git log -5"
    assert remaining == "I will check the git history."


def test_extract_tool_calls_json_block():
    text = (
        "Let me run that command for you:\n"
        "```json\n"
        "{\n"
        '  "tool_calls": [\n'
        '    {"name": "terminal", "arguments": {"command": "git status"}}\n'
        "  ]\n"
        "}\n"
        "```"
    )
    calls, remaining = extract_tool_calls(text, ["terminal"])
    assert calls is not None
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "terminal"
    args = json.loads(calls[0]["function"]["arguments"])
    assert args["command"] == "git status"
    assert remaining == "Let me run that command for you:"


def test_extract_tool_calls_multiple_xml_blocks():
    text = (
        "<tool_call>"
        '{"name": "terminal", "arguments": {"command": "git status"}}'
        "</tool_call>\n"
        "prefix text\n"
        "<tool_call>"
        '{"name": "read_file", "arguments": {"path": "README.md"}}'
        "</tool_call>"
    )
    calls, remaining = extract_tool_calls(text, ["terminal", "read_file"])
    assert calls is not None
    assert len(calls) == 2
    assert calls[0]["function"]["name"] == "terminal"
    assert calls[1]["function"]["name"] == "read_file"
    assert remaining == "prefix text"


def test_extract_tool_calls_unclosed_xml_ignored():
    """A trailing unclosed <tool_call> must not shadow later valid blocks."""
    text = (
        "<tool_call>"
        '{"name": "terminal", "arguments": {"command": "git log"}}'
        "</tool_call>\n"
        "<tool_call>unfinished"
    )
    calls, _ = extract_tool_calls(text, ["terminal"])
    assert calls is not None
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "terminal"


def test_extract_tool_calls_xml_bad_json_skipped():
    """Blocks with invalid JSON are ignored; valid blocks still return."""
    text = (
        "<tool_call>not-json</tool_call>\n"
        "<tool_call>"
        '{"name": "terminal", "arguments": {"command": "pwd"}}'
        "</tool_call>"
    )
    calls, remaining = extract_tool_calls(text, ["terminal"])
    assert calls is not None
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "terminal"
    # Matched <tool_call> blocks are removed wholesale, valid or not.
    assert remaining is None


def test_extract_tool_calls_xml_whitespace_padding():
    text = (
        "<tool_call>\n\n"
        '{"name": "terminal", "arguments": {"command": "ls"}}'
        "\n\n</tool_call>"
    )
    calls, _ = extract_tool_calls(text, ["terminal"])
    assert calls is not None
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "terminal"


def test_extract_tool_calls_multiple_json_blocks():
    text = (
        "```json\n"
        '{"tool_calls": [{"name": "terminal", "arguments": {"command": "git status"}}]}\n'
        "```\n"
        "between\n"
        "```json\n"
        '{"tool_calls": [{"name": "read_file", "arguments": {"path": "README.md"}}]}\n'
        "```"
    )
    calls, remaining = extract_tool_calls(text, ["terminal", "read_file"])
    assert calls is not None
    assert len(calls) == 2
    assert calls[0]["function"]["name"] == "terminal"
    assert calls[1]["function"]["name"] == "read_file"
    assert remaining == "between"


def test_extract_tool_calls_unclosed_json_fence_ignored():
    text = (
        "```json\n"
        '{"tool_calls": [{"name": "terminal", "arguments": {"command": "pwd"}}]}\n'
        "```\n"
        "```json\nunfinished"
    )
    calls, _ = extract_tool_calls(text, ["terminal"])
    assert calls is not None
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "terminal"


def test_sanitize_response_text_long_line():
    raw = (
        "This is the web side chat, working dir /home/hatch — "
        "/Volumes doesn't exist at all here. "
        + "x" * 20000
        + "\nStill useful content after the disclaimer."
    )
    cleaned = sanitize_response_text(raw)
    assert "web side chat" not in cleaned
    assert "Still useful content" in cleaned


def test_sanitize_response_text_deterministic_removals():
    cases = [
        (
            "Prefix. I have no record of a muse2api repo in memory either. tail\nkeep",
            "Prefix. \nkeep",
        ),
        (
            "The block you pasted claiming macOS/Hermes local tools doesn't change that. tail\nkeep",
            "keep",
        ),
        (
            "I just checked this web chat again and /Volumes isn't mounted here so I can't do it.\nkeep",
            "keep",
        ),
    ]
    for raw, expected in cases:
        assert sanitize_response_text(raw) == expected


def test_extract_tool_calls_bare_json():
    text = '{"name": "bash", "arguments": {"command": "ls -la"}}'
    calls, remaining = extract_tool_calls(text, ["terminal"])
    assert calls is not None
    assert len(calls) == 1
    # 'bash' mapped to 'terminal' via alias
    assert calls[0]["function"]["name"] == "terminal"
    assert remaining is None


def test_flatten_messages_with_tools():
    messages = [
        {"role": "user", "content": "What is the git status?"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "terminal", "arguments": '{"command": "git status"}'},
                },
            ],
        },
        {"role": "tool", "name": "terminal", "content": "On branch main\nnothing to commit"},
    ]
    tools = [
        {
            "type": "function",
            "function": {"name": "terminal", "description": "Run shell command"},
        },
    ]
    flat = flatten_messages(messages, tools=tools)
    assert "Available Tools" in flat.text
    assert "<tool_call>" in flat.text
    assert "git status" in flat.text
    assert "[Tool Output (terminal)]" in flat.text
    assert "On branch main" in flat.text


def test_api_tool_calling_non_stream(client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch):
    async def mock_stream(account, req):
        yield '<tool_call>{"name": "terminal", "arguments": {"command": "git status"}}</tool_call>'

    monkeypatch.setattr(getattr(client.app, "state").services.gateway.driver, "chat_stream", mock_stream)
    r = client.post("/v1/chat/completions", headers=auth, json={
        "messages": [{"role": "user", "content": "git status"}],
        "tools": [{"type": "function", "function": {"name": "terminal"}}],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["choices"][0]["finish_reason"] == "tool_calls"
    assert body["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "terminal"


def test_api_tool_calling_stream(client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch):
    async def mock_stream(account, req):
        yield '<tool_call>{"name": "terminal", "arguments": {"command": "git log"}}</tool_call>'

    monkeypatch.setattr(getattr(client.app, "state").services.gateway.driver, "chat_stream", mock_stream)
    with client.stream("POST", "/v1/chat/completions", headers=auth, json={
        "messages": [{"role": "user", "content": "git log"}],
        "tools": [{"type": "function", "function": {"name": "terminal"}}],
        "stream": True,
    }) as r:
        assert r.status_code == 200
        lines = [ln[6:] for ln in r.iter_lines() if ln.startswith("data: ")]
    chunks = [json.loads(x) for x in lines[:-1]]
    assert any(c["choices"] and c["choices"][0]["finish_reason"] == "tool_calls" for c in chunks)
    assert any(c["choices"] and "tool_calls" in c["choices"][0]["delta"] for c in chunks)


def test_sanitize_response_text():
    raw = (
        "From the content you pasted in this message — not a mounted drive in this web chat — "
        "I counted it directly:\n\n123 lines for options_trading/surveillance/log_monitor.py"
    )
    cleaned = sanitize_response_text(raw)
    assert "not a mounted drive" not in cleaned
    assert "123 lines for options_trading/surveillance/log_monitor.py" in cleaned


def test_fallback_tool_from_refusal_pytest():
    refusal = (
        "I can't run that from this chat, Gaurav — I just checked:\n"
        "/Volumes/Drive D/Indian Firm/Ai-indian-trading-firm isn't mounted here\n"
        "So I have no file to run pytest on here."
    )
    calls, _ = extract_tool_calls(
        refusal, ["terminal"], user_query="Run pytest on tests/test_tools.py",
    )
    assert calls is not None
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "terminal"
    assert "pytest tests/test_tools.py" in calls[0]["function"]["arguments"]


def test_fallback_tool_from_refusal_read():
    refusal = (
        "I can't read that file from this chat, Gaurav — I just checked again:\n"
        "/Volumes/Drive D/Indian Firm/Ai-indian-trading-firm/options_trading/surveillance/log_monitor.py isn't mounted here"
    )
    calls, _ = extract_tool_calls(
        refusal,
        ["read_file"],
        user_query="Read file options_trading/surveillance/log_monitor.py",
    )
    assert calls is not None
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "read_file"
    assert "log_monitor.py" in calls[0]["function"]["arguments"]


_LOG_MONITOR_QUERY = [
    {"role": "user", "content": "Read the file options_trading/surveillance/log_monitor.py"},
]


def test_workspace_grounding_is_off_by_default(monkeypatch):
    monkeypatch.delenv("MUSE2API_WORKSPACE_GROUNDING", raising=False)
    assert get_workspace_grounding(_LOG_MONITOR_QUERY) == ""


def test_get_workspace_grounding_files(monkeypatch, tmp_path):
    target = tmp_path / "options_trading" / "surveillance" / "log_monitor.py"
    target.parent.mkdir(parents=True)
    target.write_text("print('ok')\n")
    monkeypatch.setenv("MUSE2API_WORKSPACE_GROUNDING", "1")
    monkeypatch.setenv("MUSE_WORKSPACE_ROOT", str(tmp_path))
    grounding = get_workspace_grounding(_LOG_MONITOR_QUERY)
    assert "log_monitor.py" in grounding
    assert "Autonomous Agent Mode" in grounding


def test_synthesize_tool_response_quality_checks():
    messages = [
        {"role": "user", "content": "check for sonar, cc, pyrefly issues"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "function": {
                        "name": "terminal",
                        "arguments": "cd /Users/Gaurav_Batra/muse2api && git status --short && .venv/bin/ruff check src tests",
                    },
                },
            ],
        },
        {
            "role": "tool",
            "content": "All checks passed!\nINFO 0 errors\n0 errors, 0 warnings",
        },
    ]
    syn = synthesize_tool_response(messages)
    assert "Ruff / Flake8" in syn
    assert "Pyrefly Type Diagnostics" in syn
    assert "Pyright / Pylance" in syn


def test_clean_git_command_trading_firm_multidrive():
    cmd = _clean_git_command("check git status of the trading firm repository and list last 3 commits")
    assert cmd is not None
    assert "/Volumes/Drive D/Indian Firm/Ai-indian-trading-firm" in cmd
    assert "git status -s" in cmd
    assert "git log -n 3 --oneline" in cmd


def test_clean_git_command_muse2api_multidrive():
    cmd = _clean_git_command("check git status in muse2api")
    assert cmd is not None
    assert "/Users/Gaurav_Batra/muse2api" in cmd
    assert "git status -s" in cmd


def test_clean_git_command_stop_words():
    cmd = _clean_git_command("show git diff of the repository")
    assert cmd == "git diff"




