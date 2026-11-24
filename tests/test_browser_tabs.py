"""Tab pool of the browser driver, with Chromium replaced by fake tabs."""

from __future__ import annotations

import asyncio
import itertools

import pytest

from muse2api.accounts import Account
from muse2api.config import Settings
from muse2api.drivers.base import ChatRequest
from muse2api.drivers.browser.driver import BrowserDriver, _Tab


class _FakeSession:
    closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def driver(tmp_path, monkeypatch) -> BrowserDriver:
    drv = BrowserDriver(Settings(_env_file=None, data_dir=tmp_path, account_max_concurrency=2))  # type: ignore[call-arg]
    ids = itertools.count()

    async def fake_open(account: Account) -> _Tab:
        return _Tab(account.id, "ctx", f"t{next(ids)}", _FakeSession())  # type: ignore[arg-type]

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
    return _Tab("a0", "ctx", "t", _ScriptedSession(states))  # type: ignore[arg-type]


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
    att = await driver._wait_media(
        tab, {"agentCount": 0, "attachments": []}, "image", 30,
        on_progress=None, cancel=None,
    )
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
        await driver._wait_media(
            tab, {"agentCount": 0, "attachments": []}, "image", 30,
            on_progress=None, cancel=None,
        )
    assert len(sent) == 1


async def test_timeout_is_soft_while_still_generating(driver, fast):
    busy = {"agentCount": 0, "lastText": "", "generating": True, "attachments": []}
    tab = _media_tab([busy])
    task = asyncio.create_task(
        driver._wait_media(
            tab, {"agentCount": 0, "attachments": []}, "image", 0.05,
            on_progress=None, cancel=None,
        ))
    await asyncio.sleep(0.1)  # past the timeout, but the stop button is still shown
    assert not task.done()
    tab.session.states[:] = [{**busy, "generating": False, "agentCount": 1, "attachments": [IMG]}]  # type: ignore[attr-defined]
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


async def _run_begin_chat(driver, monkeypatch, tab, req, on_thread=True):
    """Drive _begin_chat with CDP-touching steps stubbed; returns (sent, new_thread)."""
    sent = []
    new_thread = []

    async def fake_new(t):
        new_thread.append(t)

    async def fake_state(t):
        return {}

    async def fake_attach(t, images):
        pass

    async def fake_send(t, prompt):
        sent.append(prompt)

    async def fake_on(t):
        return on_thread

    async def spy_checkout(account, prefer=None):
        spy_checkout.prefer = prefer  # type: ignore[attr-defined]
        return tab

    monkeypatch.setattr(driver, "_checkout", spy_checkout)
    monkeypatch.setattr(driver, "_new_thread", fake_new)
    monkeypatch.setattr(driver, "_state", fake_state)
    monkeypatch.setattr(driver, "_attach", fake_attach)
    monkeypatch.setattr(driver, "_send", fake_send)
    monkeypatch.setattr(driver, "_on_thread", fake_on)
    await driver._begin_chat(ACC, req)
    return sent, new_thread, spy_checkout.prefer  # type: ignore[attr-defined]


async def test_anonymous_chat_prefers_a_stateless_tab(driver, monkeypatch):
    fresh = _Tab(ACC.id, "ctx", "t0", _FakeSession())  # type: ignore[arg-type]
    sent, _, prefer = await _run_begin_chat(
        driver, monkeypatch, fresh,
        ChatRequest(prompt="hi", model="muse-chat", turns=[("user", "hi")]),
    )
    used = _Tab(ACC.id, "ctx", "t1", _FakeSession())  # type: ignore[arg-type]
    used.hint, used.turns = "", [("user", "hi")]
    assert prefer(used) is False
    assert prefer(fresh) is True
    assert sent == ["hi"]  # new thread, full prompt


async def test_anonymous_chat_never_continues_an_unowned_thread(driver, monkeypatch):
    # History lines up with the tab's thread, but with no `user` there is no
    # way to know it is the same client, so it must not continue it.
    tab = _Tab(ACC.id, "ctx", "t0", _FakeSession())  # type: ignore[arg-type]
    tab.hint, tab.turns = "", [("user", "hi")]
    tab.thread_url = "https://muse.ai/chat/abc"
    sent, new_thread, _ = await _run_begin_chat(
        driver, monkeypatch, tab,
        ChatRequest(prompt="FULL", model="muse-chat",
                    turns=[("user", "hi"), ("assistant", "hello"), ("user", "more")]),
    )
    assert new_thread  # opened a new thread instead of continuing
    assert sent == ["FULL"]  # full prompt, not the "more" delta


async def test_named_chat_still_continues_its_thread(driver, monkeypatch):
    tab = _Tab(ACC.id, "ctx", "t0", _FakeSession())  # type: ignore[arg-type]
    tab.hint, tab.turns = "alice", [("user", "hi")]
    tab.thread_url = "https://muse.ai/chat/abc"
    sent, new_thread, _ = await _run_begin_chat(
        driver, monkeypatch, tab,
        ChatRequest(prompt="FULL", model="muse-chat", conversation_hint="alice",
                    turns=[("user", "hi"), ("assistant", "hello"), ("user", "more")]),
    )
    assert not new_thread
    assert sent == ["more"]  # only the delta, on the same thread


async def _finish_chat(driver, fast, monkeypatch, req):
    """Run chat_stream to a finished reply on a fake thread; returns (tab, account)."""
    account = Account(id="a1", cookies={"hatch_sess": "x"})
    tab = _Tab(account.id, "ctx", "t0", _FakeSession())  # type: ignore[arg-type]
    tab.busy = True

    async def begin(acc, r):
        return tab, {"agentCount": 0, "lastText": ""}

    async def state(t):
        return {"agentCount": 1, "lastText": "hello", "generating": False}

    async def href(t):
        return "https://muse.ai/thread/abc"

    monkeypatch.setattr(driver, "_begin_chat", begin)
    monkeypatch.setattr(driver, "_state", state)
    monkeypatch.setattr(driver, "_href", href)
    assert [d async for d in driver.chat_stream(account, req)] == ["hello"]
    return tab, account


async def test_anonymous_reply_is_not_kept(driver, fast, monkeypatch):
    tab, account = await _finish_chat(driver, fast, monkeypatch,
                                      ChatRequest(prompt="hi", model="muse-chat", turns=[("user", "hi")]))
    assert not tab.has_state and not tab.busy
    assert "hot_page" not in account.meta


async def test_named_reply_is_kept_for_the_next_turn(driver, fast, monkeypatch):
    tab, account = await _finish_chat(
        driver, fast, monkeypatch,
        ChatRequest(prompt="hi", model="muse-chat", conversation_hint="k:alice", turns=[("user", "hi")]))
    assert tab.hint == "k:alice" and tab.thread_url == "https://muse.ai/thread/abc"
    assert account.meta["hot_page"]["hint"] == "k:alice"


async def test_anonymous_chat_does_not_restore_the_saved_thread(driver, monkeypatch):
    tab = _Tab(ACC.id, "ctx", "t0", _FakeSession())  # type: ignore[arg-type]
    account = Account(id="a2", cookies={"hatch_sess": "x"},
                      meta={"hot_page": {"hint": "k:alice", "turns": [["user", "hi"]],
                                         "url": "https://muse.ai/thread/abc", "image_count": 0}})
    monkeypatch.setattr(driver, "_checkout", lambda acc, prefer=None: _ready(tab))
    for name in ("_new_thread", "_attach", "_send"):
        monkeypatch.setattr(driver, name, _noop)
    monkeypatch.setattr(driver, "_state", lambda t: _ready({}))
    await driver._begin_chat(account, ChatRequest(prompt="hi", model="muse-chat", turns=[("user", "hi")]))
    assert not tab.has_state  # alice's saved thread stays out of an anonymous tab
    assert account.meta["hot_page"]["hint"] == "k:alice"


async def _ready(value):
    return value


async def _noop(*args, **kwargs):
    return None


# ---- opening a blank thread ----

class _NavSession(_FakeSession):
    """A muse.ai page whose router either lands on an empty /thread/new or is missing."""

    def __init__(self, router: bool) -> None:
        self.router = router
        self.sent: list[str] = []

    async def evaluate(self, expr, **_):
        from muse2api.drivers.browser import dom

        if expr == dom.SOFT_NEW_THREAD:
            return self.router
        if expr == dom.FRESH_THREAD:
            return True
        if expr == dom.PAGE_STATE:
            return {"ready": True, "path": "/thread/new"}
        return {}

    async def send(self, method, params=None, **_):
        self.sent.append(method)
        return {}


async def test_new_thread_uses_the_page_router_without_reloading(driver, fast):
    tab = _Tab("a0", "ctx", "t", _NavSession(router=True))  # type: ignore[arg-type]
    tab.turns, tab.thread_url = [("user", "hi")], "https://muse.ai/thread/x"
    await driver._new_thread(tab)
    assert tab.session.sent == []  # type: ignore[attr-defined]
    assert not tab.has_state


async def test_new_thread_falls_back_to_a_full_navigation(driver, fast):
    tab = _Tab("a0", "ctx", "t", _NavSession(router=False))  # type: ignore[arg-type]
    await driver._new_thread(tab)
    assert tab.session.sent == ["Page.navigate"]  # type: ignore[attr-defined]


# ---- warm threads ----

async def test_chat_goes_into_a_warm_thread_without_opening_one(driver, monkeypatch):
    tab = _Tab(ACC.id, "ctx", "t0", _FakeSession())  # type: ignore[arg-type]
    tab.warm = True
    sent, new_thread, _ = await _run_begin_chat(
        driver, monkeypatch, tab,
        ChatRequest(prompt="FULL", model="muse-chat", turns=[("user", "hi")]),
    )
    assert not new_thread
    assert sent == ["FULL"]
    assert not tab.warm  # used up


async def test_checkout_hands_out_a_warm_tab_first(driver):
    cold, warm = await driver._checkout(ACC), await driver._checkout(ACC)
    warm.warm = True
    await driver._checkin(cold)
    await driver._checkin(warm)
    assert await driver._checkout(ACC, prefer=lambda t: not t.has_state) is warm


async def test_media_leaves_a_warm_tab_alone(driver, monkeypatch):
    warm = await driver._checkout(ACC)
    warm.warm = True
    await driver._checkin(warm)
    taken = []

    async def prepare(tab, prompt, images):
        taken.append(tab)
        raise RuntimeError("stop")

    monkeypatch.setattr(driver, "_prepare", prepare)
    with pytest.raises(RuntimeError):
        await driver._generate(ACC, "a cat", [], "image", 1, on_progress=None, cancel=None)
    assert taken and taken[0] is not warm and warm.warm


async def test_warming_keeps_a_slot_free_for_real_requests(tmp_path, monkeypatch):
    drv = BrowserDriver(Settings(_env_file=None, data_dir=tmp_path, account_max_concurrency=2,  # type: ignore[call-arg]
                                 warm_threads=1))
    ids = itertools.count()

    async def fake_open(account):
        return _Tab(account.id, "ctx", f"t{next(ids)}", _FakeSession())  # type: ignore[arg-type]

    monkeypatch.setattr(drv, "_open_tab", fake_open)
    busy = await drv._checkout(ACC)
    assert await drv._checkout_for_warming(ACC) is None  # one slot left: keep it for a request
    await drv._checkin(busy)
    tab = await drv._checkout_for_warming(ACC)
    assert tab is not None and tab.busy
    tab.warm = True
    await drv._checkin(tab)
    assert await drv._checkout_for_warming(ACC) is None  # enough warm threads


class _CookieSession(_FakeSession):
    def __init__(self) -> None:
        self.cookies: dict[str, str] = {}

    async def send(self, method, params=None, **_):
        if method == "Network.setCookie":
            self.cookies[params["name"]] = params["value"]  # type: ignore[optional-subscript]
        return {}


async def test_renewal_rotates_cookies_in_place_and_keeps_tabs(driver, monkeypatch):
    from muse2api.drivers.base import SessionInfo
    from muse2api.drivers.browser import driver as mod

    async def renew(cookies, **_):
        return SessionInfo(ok=True, cookies={"hatch_sess": "rotated"})

    monkeypatch.setattr(mod.muse, "renew_session", renew)
    tab = await driver._checkout(ACC)
    tab.session = _CookieSession()
    tab.warm = True
    await driver._checkin(tab)
    await driver.renew_session(ACC)
    assert tab.session.cookies["hatch_sess"] == "rotated"
    assert not tab.closed and not tab.stale and tab.warm


async def test_new_named_conversation_takes_the_warm_tab_not_a_new_one(driver):
    warm = await driver._checkout(ACC)
    warm.warm = True
    await driver._checkin(warm)
    taken = []

    async def stop(t):
        taken.append(t)
        raise RuntimeError("stop")

    driver._state = stop
    with pytest.raises(RuntimeError):
        await driver._begin_chat(ACC, ChatRequest(prompt="hi", model="muse-chat",
                                                  conversation_hint="k:bob", turns=[("user", "hi")]))
    assert taken == [warm]


async def test_missing_warm_threads_are_taken_together(tmp_path, monkeypatch):
    drv = BrowserDriver(Settings(_env_file=None, data_dir=tmp_path, account_max_concurrency=6,  # type: ignore[call-arg]
                                 warm_threads=3))
    ids = itertools.count()

    async def fake_open(account):
        return _Tab(account.id, "ctx", f"t{next(ids)}", _FakeSession())  # type: ignore[arg-type]

    monkeypatch.setattr(drv, "_open_tab", fake_open)
    batch = []
    while (tab := await drv._checkout_for_warming(ACC, len(batch))) is not None:
        batch.append(tab)
    assert len(batch) == 3 and len({t.target_id for t in batch}) == 3


async def test_a_failed_warm_round_is_retried(tmp_path, monkeypatch, fast):
    drv = BrowserDriver(Settings(_env_file=None, data_dir=tmp_path, account_max_concurrency=4,  # type: ignore[call-arg]
                                 warm_threads=1))
    ids = itertools.count()

    async def fake_open(account):
        return _Tab(account.id, "ctx", f"t{next(ids)}", _FakeSession())  # type: ignore[arg-type]

    attempts = []

    async def warm_up(tab):
        attempts.append(tab)
        if len(attempts) == 1:
            raise RuntimeError("page did not become ready")
        tab.warm = True

    monkeypatch.setattr(drv, "_open_tab", fake_open)
    monkeypatch.setattr(drv, "_warm_up", warm_up)
    await drv._fill_warm(ACC)
    assert len(attempts) == 2
    assert sum(t.warm for t in drv._tabs[ACC.id]) == 1
