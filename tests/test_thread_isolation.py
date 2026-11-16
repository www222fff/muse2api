"""Thread isolation between requests, with a scripted page.

Regression rig for the live verify failures: a request must never be
answered from the previous request's thread — not with its stale last
bubble (the non-stream PINEAPPLE case returning the login spec), and not
with a fresh reply generated inside the old conversation (the headings
case answering "is this the login page again?"). Also pins that
chat_stream and chat_complete send the identical prompt.
"""

from __future__ import annotations

import itertools
import json
import re

import pytest

from muse2api.accounts import Account
from muse2api.config import Settings
from muse2api.drivers.base import ChatRequest
from muse2api.drivers.browser.driver import BrowserDriver, _Tab
from muse2api.errors import UpstreamError

ACC = Account(id="a0", cookies={"hatch_sess": "x"})
FILL_RE = re.compile(r"setter\.call\(ta, (\".*?\")\);", re.DOTALL)


class _ScriptedSession:
    """A fake page: soft navigation always 'works', states come from a queue."""

    closed = False

    def __init__(self) -> None:
        self.href = "about:blank"  # a fresh tab
        self.after_send_href = "https://muse.ai/thread/aaa"
        self.path = "/thread/new"
        self.states: list[dict] = []
        self._last_state: dict = {"agentCount": 0, "lastText": "", "lastHtml": "",
                                  "generating": False, "approval": False,
                                  "attachments": [], "tail": ""}
        self.fills: list[str] = []

    async def close(self) -> None:
        self.closed = True

    async def send(self, method: str, params: dict | None = None) -> dict:
        return {}

    def queue(self, *states: dict) -> None:
        self.states.extend(states)

    def _state(self, **over) -> dict:
        return {**self._last_state, **over}

    async def evaluate(self, script: str, timeout: float | None = None):
        if script == "location.href":
            return self.href
        if "router.push" in script:
            return True
        if "page.path ===" in script:  # FRESH_THREAD
            return True
        if "agentCount" in script:  # CHAT_STATE
            if self.states:
                self._last_state = self.states.pop(0)
            return self._last_state
        if "no-input" in script:  # fill_input: sending moves to the new thread
            match = FILL_RE.search(script)
            text = json.loads(match.group(1)) if match else ""
            self.fills.append(text)
            self.href = self.after_send_href
            return {"ok": True, "len": len(text)}
        if "return 'missing'" in script:  # CLICK_SEND
            return "clicked"
        if script.startswith("!("):  # INPUT_EMPTY
            return True
        if "path: location.pathname" in script:  # PAGE_STATE
            return {"ready": True, "hasInput": True, "path": self.path, "head": ""}
        if "denied" in script:  # DENY_APPROVALS
            return []
        return None


def _reply(text: str) -> dict:
    return {"agentCount": 1, "lastText": text, "lastHtml": "", "generating": False,
            "approval": False, "attachments": [], "tail": ""}


@pytest.fixture
def rig(tmp_path, monkeypatch):
    session = _ScriptedSession()
    drv = BrowserDriver(Settings(_env_file=None, data_dir=tmp_path,
                                 account_max_concurrency=2, warm_threads=0))
    ids = itertools.count()

    async def fake_open(account: Account) -> _Tab:
        return _Tab(account.id, "ctx", f"t{next(ids)}", session)

    monkeypatch.setattr(drv, "_open_tab", fake_open)
    return drv, session


def _req(prompt: str) -> ChatRequest:
    return ChatRequest(prompt=prompt, model="gpt-4o-mini",
                       timeout=10, first_token_timeout=5)


async def test_second_request_never_gets_the_first_requests_thread(rig):
    drv, session = rig
    # Request 1: a fresh thread answers normally.
    session.queue({"agentCount": 0, "lastText": ""}, *[_reply("FIRST ANSWER")] * 6)
    assert await drv.chat_complete(ACC, _req("write the login spec")) == "FIRST ANSWER"
    assert session.fills == ["write the login spec"]

    # Request 2: soft navigation claims success, but the page still shows the
    # previous thread at base time and a "reply" arrives inside it. The driver
    # must refuse (fresh-thread isolation) instead of sending into the old
    # conversation or returning anything from it.
    session.queue({"agentCount": 1, "lastText": "FIRST ANSWER"},
                  {"agentCount": 1, "lastText": "FIRST ANSWER"},
                  {"agentCount": 2, "lastText": "CONTAMINATED REPLY"})
    with pytest.raises(UpstreamError):
        await drv.chat_complete(ACC, _req("reply with exactly PINEAPPLE"))
    assert session.fills == ["write the login spec"]  # never sent anywhere


async def test_poll_reply_rejects_a_bubble_from_the_previous_thread(rig):
    drv, session = rig
    tab = await drv._checkout(ACC)
    session.href = "https://muse.ai/thread/old1"  # the page went back
    session.queue(*[_reply("OLD REPLY")] * 8)
    base = {"agentCount": 0, "lastText": "",
            "previousUrl": "https://muse.ai/thread/old1"}
    with pytest.raises(UpstreamError):
        async for _ in drv._poll_reply(tab, base, _req("go")):
            pass


async def test_poll_reply_accepts_a_reply_on_a_new_thread(rig):
    drv, session = rig
    tab = await drv._checkout(ACC)
    session.href = "https://muse.ai/thread/new9"  # a different, new thread
    session.queue(*[_reply("NEW REPLY")] * 8)
    base = {"agentCount": 0, "lastText": "",
            "previousUrl": "https://muse.ai/thread/old1"}
    seen = [text async for text in drv._poll_reply(tab, base, _req("go"))]
    assert seen[-1] == "NEW REPLY"


async def test_stream_and_complete_send_the_identical_prompt(rig):
    drv, session = rig
    prompt = "<system_instructions>\nReply with exactly PINEAPPLE\n</system_instructions>\n\ngo"
    session.queue({"agentCount": 0, "lastText": ""}, *[_reply("PINEAPPLE")] * 8)
    assert await drv.chat_complete(ACC, _req(prompt)) == "PINEAPPLE"

    session.states.clear()
    session.after_send_href = "https://muse.ai/thread/bbb"
    session.queue({"agentCount": 0, "lastText": ""}, *[_reply("PINEAPPLE")] * 8)
    streamed = "".join([delta async for delta in drv.chat_stream(ACC, _req(prompt))])
    assert streamed == "PINEAPPLE"
    assert session.fills == [prompt, prompt]
