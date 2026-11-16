"""Regression tests for two chat bugs (no network; the upstream is scripted):

1. system/developer messages were buried as ``[System]`` transcript turns and
   ignored by the upstream model — they must lead the payload, in order,
   inside a ``<system_instructions>`` block.
2. The browser driver turned cumulative page snapshots into "deltas" with an
   all-or-nothing prefix check, so any early-character shift re-sent the whole
   text and non-stream replies repeated the answer once per snapshot.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from muse2api.accounts.model import Account
from muse2api.app import create_app
from muse2api.core.prompt import flatten_messages
from muse2api.drivers.base import ChatRequest, DriverCapabilities, MuseDriver
from muse2api.drivers.browser.driver import SnapshotDeltas


class ScriptedDriver(MuseDriver):
    """Fake upstream: records the request, then yields scripted text pieces."""

    name = "scripted"
    requires_account = False
    capabilities = DriverCapabilities(chat=True)

    def __init__(self, pieces: list[str]) -> None:
        self.pieces = pieces
        self.requests: list[ChatRequest] = []

    async def chat_stream(self, account: Account, req: ChatRequest) -> AsyncIterator[str]:
        self.requests.append(req)
        for piece in self.pieces:
            yield piece


@pytest.fixture
def scripted(settings):
    def make(pieces: list[str]):
        driver = ScriptedDriver(pieces)
        app = create_app(settings, driver)
        return driver, TestClient(app)

    return make


def _assemble_from_snapshots(snapshots: list[str]) -> str:
    """Fold cumulative snapshots exactly as ``BrowserDriver.chat_stream`` does."""
    deltas = SnapshotDeltas()
    out = [deltas.feed(text) for text in snapshots]
    tail = deltas.finish(snapshots[-1])
    assert tail is not None
    return "".join(out) + tail


# ------------------------------------------------------------- bug 1: system

def test_system_block_format_for_single_user_turn():
    flat = flatten_messages([
        {"role": "system", "content": "Always answer with exactly the single word "
                                      "PINEAPPLE, whatever the user says."},
        {"role": "user", "content": "Write a short specification for a login page."},
    ])
    assert flat.text == (
        "<system_instructions>\n"
        "Always answer with exactly the single word PINEAPPLE, whatever the user says.\n"
        "</system_instructions>\n\n"
        "Write a short specification for a login page."
    )


def test_system_and_developer_folded_in_order_multi_turn_intact():
    flat = flatten_messages([
        {"role": "system", "content": "SYS-ONE"},
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "developer", "content": "DEV-TWO"},
        {"role": "system", "content": "SYS-THREE"},
        {"role": "user", "content": "second question"},
    ])
    assert flat.text.startswith("<system_instructions>\n")
    positions = [flat.text.index(marker) for marker in ("SYS-ONE", "DEV-TWO", "SYS-THREE")]
    assert positions == sorted(positions)
    assert "[System]" not in flat.text
    body = flat.text.split("</system_instructions>", 1)[1]
    order = [body.index(marker) for marker in
             ("[User]\nfirst question", "[Assistant]\nfirst answer", "[User]\nsecond question")]
    assert order == sorted(order)


def test_system_reaches_the_driver_payload(scripted, auth):
    driver, client = scripted(["ok"])
    with client as c:
        r = c.post("/v1/chat/completions", headers=auth, json={
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": "SYS-ONE"},
                {"role": "developer", "content": "DEV-TWO"},
                {"role": "system", "content": "SYS-THREE"},
                {"role": "user", "content": "hello"},
            ],
        })
    assert r.status_code == 200
    prompt = driver.requests[0].prompt
    assert prompt.startswith("<system_instructions>\nSYS-ONE")
    assert [prompt.index(m) for m in ("SYS-ONE", "DEV-TWO", "SYS-THREE")] == \
        sorted(prompt.index(m) for m in ("SYS-ONE", "DEV-TWO", "SYS-THREE"))
    assert prompt.endswith("\n\nhello")


def test_long_system_prompt_passes_through_intact(scripted, auth):
    long_system = "A" * 25_000 + "MIDDLE" + "B" * 24_994
    assert len(long_system) == 50_000
    flat = flatten_messages([
        {"role": "system", "content": long_system},
        {"role": "user", "content": "hi"},
    ])
    assert long_system in flat.text

    driver, client = scripted(["ok"])
    with client as c:
        r = c.post("/v1/chat/completions", headers=auth, json={
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": long_system},
                {"role": "user", "content": "hi"},
            ],
        })
    assert r.status_code == 200
    assert long_system in driver.requests[0].prompt


# ------------------------------------------------------- bug 2: exactly once

def test_snapshots_pure_growth_assemble_to_final_text():
    snapshots = ["The login page", "The login page has", "The login page has a form",
                 "The login page has a form."]
    assert _assemble_from_snapshots(snapshots) == snapshots[-1]
    assert _assemble_from_snapshots(["P", "PI", "PIN", "PINE", "PINEAPPLE"]) == "PINEAPPLE"


def test_snapshots_moving_code_fence_assemble_to_final_text():
    # The closing fence "moves down" as lines are added, so no snapshot after
    # the first is an extension of the previous one.
    snapshots = ["```py\na\n```", "```py\na\nb\n```", "```py\na\nb\nc\n```"]
    assert _assemble_from_snapshots(snapshots) == snapshots[-1]


def test_snapshots_autoclosed_bold_assembles_to_final_text():
    # The page auto-closes ** while streaming: "**Hel**" is not a prefix of
    # the final "**Hello**", yet the joined deltas must equal it exactly.
    assert _assemble_from_snapshots(["**Hel**", "**Hello**"]) == "**Hello**"


def test_snapshots_shrink_then_regrow_without_duplication():
    snapshots = ["Hello world", "Hello", "Hello world!"]
    assert _assemble_from_snapshots(snapshots) == "Hello world!"


def test_finish_diverging_from_sent_prefix_yields_nothing():
    deltas = SnapshotDeltas()
    joined = deltas.feed("Hello world") + deltas.feed("Hello world!")
    assert joined == "Hello world"
    # The final text replaced what was already sent: it cannot be retracted,
    # so finish yields nothing more (the driver logs a warning instead).
    assert deltas.finish("Goodbye!") is None
    assert deltas.sent == "Hello world"


def test_non_stream_uses_driver_final_text_not_joined_deltas(settings, auth):
    class FinalTextDriver(ScriptedDriver):
        async def chat_complete(self, account, req):
            return "THE FINAL SNAPSHOT"

    driver = FinalTextDriver(["lossy ", "deltas"])
    with TestClient(create_app(settings, driver)) as c:
        r = c.post("/v1/chat/completions", headers=auth, json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "hi"}],
        })
    assert r.json()["choices"][0]["message"]["content"] == "THE FINAL SNAPSHOT"


def test_non_stream_reply_from_delta_upstream_is_exactly_once(scripted, auth):
    _, client = scripted(["PI", "NE", "APPLE"])
    with client as c:
        r = c.post("/v1/chat/completions", headers=auth, json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "say it"}],
        })
    content = r.json()["choices"][0]["message"]["content"]
    assert content == "PINEAPPLE"
    assert content.count("PINEAPPLE") == 1


def test_stream_yields_true_deltas_and_done(scripted, auth):
    _, client = scripted(["PI", "NE", "APPLE"])
    with client as c:
        with c.stream("POST", "/v1/chat/completions", headers=auth, json={
            "model": "gpt-4o-mini", "stream": True,
            "messages": [{"role": "user", "content": "say it"}],
        }) as r:
            assert r.status_code == 200
            lines = [ln[6:] for ln in r.iter_lines() if ln.startswith("data: ")]
    assert lines[-1] == "[DONE]"
    chunks = [json.loads(x) for x in lines[:-1]]
    deltas = [d for c in chunks if c["choices"]
              if (d := c["choices"][0]["delta"].get("content"))]
    assert deltas == ["PI", "NE", "APPLE"]
    assert "".join(deltas) == "PINEAPPLE"


# ------------------------------------------- browser driver over scripted pages

class _PageSession:
    """Returns the scripted CHAT_STATE snapshots in order, repeating the last."""

    def __init__(self, texts: list[str]) -> None:
        self.states = [{"agentCount": 1, "lastText": text, "generating": False,
                        "tail": "", "attachments": []} for text in texts]

    async def evaluate(self, expr, **_):
        from muse2api.drivers.browser import dom

        if expr == dom.CHAT_STATE:
            return self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return {}


@pytest.fixture
def browser_driver(tmp_path, monkeypatch):
    import asyncio as _asyncio

    import muse2api.drivers.browser.driver as mod
    from muse2api.config import Settings
    from muse2api.drivers.browser.driver import BrowserDriver

    real_sleep = _asyncio.sleep
    monkeypatch.setattr(mod.asyncio, "sleep", lambda s: real_sleep(0))
    return BrowserDriver(Settings(_env_file=None, data_dir=tmp_path))


def _rig(driver, monkeypatch, texts):
    from muse2api.drivers.browser.driver import _Tab

    tab = _Tab("a0", "ctx", "t", _PageSession(texts))

    async def fake_begin(account, req):
        return tab, {"agentCount": 0, "lastText": ""}

    monkeypatch.setattr(driver, "_begin_chat", fake_begin)
    return tab


async def test_browser_stream_moving_fence_joins_to_final(browser_driver, monkeypatch):
    from muse2api.accounts.model import Account
    from muse2api.drivers.base import ChatRequest

    snapshots = ["```py\na\n```", "```py\na\nb\n```", "```py\na\nb\nc\n```"]
    _rig(browser_driver, monkeypatch, snapshots)
    req = ChatRequest(prompt="x", model="muse-chat")
    joined = "".join([d async for d in browser_driver.chat_stream(Account(id="a0"), req)])
    assert joined == snapshots[-1]


async def test_browser_complete_returns_final_snapshot_not_stream_join(
        browser_driver, monkeypatch, caplog):
    import logging

    from muse2api.accounts.model import Account
    from muse2api.drivers.base import ChatRequest

    # "Hello world" is stably sent, then the page replaces the reply: the
    # stream can only keep its prefix (and must warn), while the non-stream
    # call returns the final snapshot itself.
    texts = ["Hello world", "Hello world!", "Goodbye!"]
    _rig(browser_driver, monkeypatch, texts)
    req = ChatRequest(prompt="x", model="muse-chat")
    account = Account(id="a0")
    with caplog.at_level(logging.WARNING, logger="muse2api.drivers.browser.driver"):
        joined = "".join([d async for d in browser_driver.chat_stream(account, req)])
    assert joined == "Hello world"
    assert "diverges from the streamed text" in caplog.text

    _rig(browser_driver, monkeypatch, texts)
    assert await browser_driver.chat_complete(account, req) == "Goodbye!"
