from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from muse2api.core.prompt import flatten_messages
from muse2api.core.tools import extract_tool_calls, format_tools_prompt


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
