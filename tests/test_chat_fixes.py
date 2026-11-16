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
from muse2api.drivers.browser.driver import snapshot_delta


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
    emitted, out = "", []
    for text in snapshots:
        delta = snapshot_delta(emitted, text)
        emitted = text
        if delta:
            out.append(delta)
    return "".join(out)


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

def test_cumulative_snapshots_assemble_to_final_text_once():
    snapshots = ["The login page", "The login page has", "The login page has a form",
                 "The login page has a form."]
    assert _assemble_from_snapshots(snapshots) == snapshots[-1]
    assert _assemble_from_snapshots(["P", "PI", "PIN", "PINE", "PINEAPPLE"]) == "PINEAPPLE"


def test_reflowed_snapshot_never_resends_the_full_text():
    # innerText re-wraps as the bubble grows: an early line break moves, so the
    # new snapshot is not an extension of the previous one. The old code yielded
    # the entire snapshot again; the delta must be only the divergent tail.
    delta = snapshot_delta("Hello\nworld", "Hello world again")
    assert delta != "Hello world again"
    assert len(delta) < len("Hello world again")


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
