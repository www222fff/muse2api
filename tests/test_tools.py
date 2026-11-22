from __future__ import annotations

import json

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
        }
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
                }
            ],
        },
        {"role": "tool", "name": "terminal", "content": "On branch main\nnothing to commit"},
    ]
    tools = [
        {
            "type": "function",
            "function": {"name": "terminal", "description": "Run shell command"},
        }
    ]
    flat = flatten_messages(messages, tools=tools)
    assert "Available Tools" in flat.text
    assert "<tool_call>" in flat.text
    assert "git status" in flat.text
    assert "[Tool Output (terminal)]" in flat.text
    assert "On branch main" in flat.text


def test_api_tool_calling_non_stream(client, auth, monkeypatch):
    async def mock_stream(account, req):
        yield '<tool_call>{"name": "terminal", "arguments": {"command": "git status"}}</tool_call>'

    monkeypatch.setattr(client.app.state.services.gateway.driver, "chat_stream", mock_stream)
    r = client.post("/v1/chat/completions", headers=auth, json={
        "messages": [{"role": "user", "content": "git status"}],
        "tools": [{"type": "function", "function": {"name": "terminal"}}],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["choices"][0]["finish_reason"] == "tool_calls"
    assert body["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "terminal"


def test_api_tool_calling_stream(client, auth, monkeypatch):
    async def mock_stream(account, req):
        yield '<tool_call>{"name": "terminal", "arguments": {"command": "git log"}}</tool_call>'

    monkeypatch.setattr(client.app.state.services.gateway.driver, "chat_stream", mock_stream)
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

