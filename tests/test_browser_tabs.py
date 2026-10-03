"""Tab pool of the browser driver, with Chromium replaced by fake tabs."""

from __future__ import annotations

import asyncio
import itertools

import pytest

from muse2api.accounts import Account
from muse2api.config import Settings
from muse2api.drivers.browser.driver import BrowserDriver, _Tab


class _FakeSession:
    closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def driver(tmp_path, monkeypatch) -> BrowserDriver:
    drv = BrowserDriver(Settings(_env_file=None, data_dir=tmp_path, account_max_concurrency=2))
    ids = itertools.count()

    async def fake_open(account: Account) -> _Tab:
        return _Tab(account.id, "ctx", f"t{next(ids)}", _FakeSession())

    monkeypatch.setattr(drv, "_open_tab", fake_open)
    return drv


ACC = Account(id="a0", cookies={"hatch_sess": "x"})


async def test_parallel_checkouts_get_separate_tabs(driver):
    t1 = await driver._checkout(ACC)
    t2 = await driver._checkout(ACC)
    assert t1 is not t2 and t1.busy and t2.busy
    assert (await driver.health())["tabs"] == 2


async def test_checkout_waits_at_the_limit_until_checkin(driver):
    t1 = await driver._checkout(ACC)
    await driver._checkout(ACC)
    third = asyncio.create_task(driver._checkout(ACC))
    await asyncio.sleep(0.01)
    assert not third.done()
    await driver._checkin(t1)
    assert await asyncio.wait_for(third, 1) is t1


async def test_media_opens_a_new_tab_rather_than_wiping_a_chat(driver):
    chat = await driver._checkout(ACC)
    chat.turns = [("user", "hi")]
    await driver._checkin(chat)
    media = await driver._checkout(ACC, prefer=lambda t: not t.has_state)
    assert media is not chat


async def test_at_the_limit_a_chat_tab_is_reused(driver):
    a = await driver._checkout(ACC)
    b = await driver._checkout(ACC)
    a.turns = [("user", "hi")]
    await driver._checkin(a)
    got = await driver._checkout(ACC, prefer=lambda t: not t.has_state)
    assert got is a  # nothing else free and no room for another tab
    await driver._checkin(b)


async def test_chat_prefers_the_tab_holding_its_conversation(driver):
    a = await driver._checkout(ACC)
    b = await driver._checkout(ACC)
    b.hint, b.turns = "user-1", [("user", "hi")]
    await driver._checkin(a)
    await driver._checkin(b)
    got = await driver._checkout(ACC, prefer=lambda t: t.has_state and t.hint == "user-1")
    assert got is b


async def test_stale_tab_closes_on_checkin(driver):
    tab = await driver._checkout(ACC)
    tab.stale = True
    await driver._checkin(tab)
    assert tab.session.closed
    assert (await driver.health())["tabs"] == 0


async def test_failed_open_releases_its_slot(driver, monkeypatch):
    ok_open = driver._open_tab

    async def broken(account):
        raise RuntimeError("chromium went away")

    monkeypatch.setattr(driver, "_open_tab", broken)
    with pytest.raises(RuntimeError):
        await driver._checkout(ACC)
    monkeypatch.setattr(driver, "_open_tab", ok_open)
    a = await driver._checkout(ACC)
    b = await driver._checkout(ACC)  # both slots still usable
    assert a is not b


async def test_close_then_checkin_closes_once(driver, monkeypatch):
    tab = await driver._checkout(ACC)
    calls = []
    original = driver._dispose_tab

    async def counting(t):
        calls.append(t)
        await original(t)

    monkeypatch.setattr(driver, "_dispose_tab", counting)
    tab.stale = True
    await driver._close_tab(tab)  # e.g. a CDP error path
    await driver._checkin(tab)    # the finally block still checks it in
    assert calls == [tab]
    assert (await driver.health())["tabs"] == 0


async def test_cancelled_checkin_still_frees_the_tab(driver):
    tab = await driver._checkout(ACC)
    await driver._tab_cond.acquire()  # lock contended while the request is cancelled
    task = asyncio.create_task(driver._checkin(tab))
    await asyncio.sleep(0)
    task.cancel()
    driver._tab_cond.release()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not tab.busy
    assert await asyncio.wait_for(driver._checkout(ACC), 1) is tab


# ---- waiting for media, with page states scripted ----

class _ScriptedSession(_FakeSession):
    """Returns the scripted CHAT_STATE snapshots in order, repeating the last one."""

    def __init__(self, states: list[dict]) -> None:
        self.states = states

    async def evaluate(self, expr, **_):
        from muse2api.drivers.browser import dom

        if expr == dom.CHAT_STATE:
            return self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return {}


@pytest.fixture
def fast(monkeypatch):
    import muse2api.drivers.browser.driver as mod

    real_sleep = asyncio.sleep
    monkeypatch.setattr(mod.asyncio, "sleep", lambda s: real_sleep(0))
    monkeypatch.setattr(BrowserDriver, "_MEDIA_GRACE", {"image": 0.0, "video": 0.0})


def _media_tab(states):
    return _Tab("a0", "ctx", "t", _ScriptedSession(states))


IMG = {"tid": "hatch-chat-attachment-presentation-1", "kind": "image", "src": "blob:x"}
DONE = {"agentCount": 1, "lastText": "Done — the sheet passed QC.", "generating": False,
        "attachments": []}


async def test_text_only_reply_asks_for_the_file_once(driver, fast, monkeypatch):
    sent = []

    async def send(tab, text):
        sent.append(text)
        tab.session.states[:] = [{**DONE, "agentCount": 2, "attachments": [IMG]}]

    monkeypatch.setattr(driver, "_send", send)
    tab = _media_tab([{**DONE, "agentCount": 0}, DONE, DONE])
    att = await driver._wait_media(tab, {"agentCount": 0, "attachments": []}, "image", 30, None, None)
    assert att == IMG
    assert len(sent) == 1 and "attach the final image" in sent[0]


async def test_still_text_only_after_asking_is_refused(driver, fast, monkeypatch):
    from muse2api.errors import UpstreamRefused

    sent = []

    async def send(tab, text):
        sent.append(text)
        tab.session.states[:] = [{**DONE, "agentCount": 2, "lastText": "It's in my workspace."}]

    monkeypatch.setattr(driver, "_send", send)
    tab = _media_tab([DONE])
    with pytest.raises(UpstreamRefused):
        await driver._wait_media(tab, {"agentCount": 0, "attachments": []}, "image", 30, None, None)
    assert len(sent) == 1


async def test_timeout_is_soft_while_still_generating(driver, fast):
    busy = {"agentCount": 0, "lastText": "", "generating": True, "attachments": []}
    tab = _media_tab([busy])
    task = asyncio.create_task(
        driver._wait_media(tab, {"agentCount": 0, "attachments": []}, "image", 0.05, None, None))
    await asyncio.sleep(0.1)  # past the timeout, but the stop button is still shown
    assert not task.done()
    tab.session.states[:] = [{**busy, "generating": False, "agentCount": 1, "attachments": [IMG]}]
    assert await asyncio.wait_for(task, 1) == IMG


async def test_quota_hint_only_counts_in_the_finished_reply(driver):
    from muse2api.errors import UpstreamQuotaError

    sidebar = {"tail": "Earlier: Fixed token limit in the sheet script 2:21 pm",
               "lastText": "Generating your image", "generating": True}
    assert (await driver._state(_media_tab([sidebar])))["tail"] == sidebar["tail"]
    quota = {"tail": "You've reached your usage limit. Try again later.",
             "lastText": "You've reached your usage limit. Try again later.", "generating": False}
    with pytest.raises(UpstreamQuotaError, match="usage limit"):
        await driver._state(_media_tab([quota]))
