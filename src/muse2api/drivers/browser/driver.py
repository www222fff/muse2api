"""Browser driver: operates the muse.ai web client in headless Chromium over CDP.

Design notes
------------
* One Chromium process; each account gets its own *browser context* (isolated
  cookie jar), so switching accounts never requires clearing cookies.
* One tab per account, reused across requests. Per-account serialisation is
  guaranteed by the account pool (``MUSE2API_ACCOUNT_MAX_CONCURRENCY=1``).
* A follow-up from the same user stays on that tab when the new ``messages``
  continue the conversation already on the page. Anything else opens a fresh
  thread, so two users never share a page.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from ...accounts.model import Account
from ...config import Settings
from ...core.prompt import followup_text
from ...errors import (
    Muse2APIError,
    UpstreamAuthError,
    UpstreamError,
    UpstreamQuotaError,
    UpstreamRefused,
    UpstreamTimeout,
)
from ...upstream import muse
from ..base import (
    ChatRequest,
    DriverCapabilities,
    ImageRequest,
    InputImage,
    MediaResult,
    MuseDriver,
    SessionInfo,
    VideoRequest,
)
from . import dom
from .cdp import CDPError, CDPSession
from .chromium import ChromiumProcess, find_chromium

log = logging.getLogger(__name__)

POLL_INTERVAL = 0.15
STABLE_POLLS_DONE = 4
STABLE_POLLS_FORCE = 12
# Past this many assistant bubbles the page gets slow and a new thread is cheaper.
HOT_BUBBLE_LIMIT = 16
# A new thread often shows "Connecting..." for 10-30s and then answers normally.
STALL_GIVE_UP = 45.0
QUOTA_CACHE_SECONDS = 1800.0


def quota_cache_seconds() -> float:
    """``MUSE2API_QUOTA_CACHE_S`` (read here: config.py has no knob for it)."""
    try:
        return max(0.0, float(os.environ.get("MUSE2API_QUOTA_CACHE_S", QUOTA_CACHE_SECONDS)))
    except ValueError:
        return QUOTA_CACHE_SECONDS


class AccountBusy(Muse2APIError):
    """Every tab of the account is serving a request; a quota read never takes one over."""

    status_code = 409
    error_type = "server_error"
    code = "account_busy"


def is_thread_url(url: str) -> bool:
    """A real muse conversation, not the empty "new thread" page."""
    path = urlsplit(url).path.rstrip("/")
    return path.startswith("/thread/") and path != "/thread/new"


@dataclass
class _Tab:
    account_id: str
    context_id: str
    target_id: str
    session: CDPSession
    hint: str | None = None
    turns: list[tuple[str, str]] = field(default_factory=list)
    image_count: int = 0
    thread_url: str = ""
    quota_note: str = ""
    """Last ignored quota-hint snippet, so a page polled every 0.6s logs it once."""
    warm: bool = False
    """The page shows a started thread holding only the warm-up exchange."""
    busy: bool = False
    stale: bool = False
    """Cookies were renewed; close this tab once it is idle."""
    closed: bool = False

    @property
    def has_state(self) -> bool:
        return bool(self.turns or self.thread_url)


class BrowserDriver(MuseDriver):
    name = "browser"
    capabilities = DriverCapabilities(
        chat=True, chat_images=True, image=True, image_edit=True, video=True, renew_session=True,
        quota=True,
    )

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._chromium: ChromiumProcess | None = None
        self._browser: CDPSession | None = None
        # One browser context (cookie jar) per account, holding up to
        # account_max_concurrency tabs; each request checks one tab out.
        self._contexts: dict[str, str] = {}
        self._tabs: dict[str, list[_Tab]] = {}
        self._opening: dict[str, int] = {}
        self._tab_cond = asyncio.Condition()
        self._ctx_lock = asyncio.Lock()
        # Last quota snapshot per account (see ``quota``) and the lock serialising reads.
        self._quota_cache: dict[str, dict[str, Any]] = {}
        self._quota_locks: dict[str, asyncio.Lock] = {}
        self._warming: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------ lifecycle
    async def startup(self) -> None:
        exe = find_chromium(self.settings.chromium_path)
        self._chromium = ChromiumProcess(
            exe, self.settings.cdp_port, self.settings.profile_dir, self.settings.headless,
            proxy=self.settings.browser_proxy,
        )
        ws_url = await self._chromium.start()
        self._browser = await CDPSession.connect(ws_url)
        log.info("browser driver ready")

    async def shutdown(self) -> None:
        for task in self._warming.values():
            task.cancel()
        for tabs in self._tabs.values():
            for tab in tabs:
                await self._close_tab(tab)
        if self._browser:
            await self._browser.close()
        if self._chromium:
            await self._chromium.stop()

    async def health(self) -> dict[str, Any]:
        ok = self._browser is not None and not self._browser.closed
        tabs = [t for ts in self._tabs.values() for t in ts]
        return {"driver": self.name, "ok": ok, "tabs": len(tabs),
                "busy_tabs": sum(t.busy for t in tabs)}

    # ------------------------------------------------------------ tabs
    def _claim_or_plan(self, account: Account, tabs: list[_Tab], limit: int,
                       prefer: Callable[[_Tab], bool] | None) -> tuple[_Tab | None, bool]:
        """Return (tab to use, or None; whether to open a fresh tab)."""
        idle = sorted((t for t in tabs if not t.busy), key=lambda t: not t.warm)
        can_open = len(tabs) + self._opening.get(account.id, 0) < limit
        match = next((t for t in idle if prefer is None or prefer(t)), None)
        if match or (idle and not can_open):
            tab = match or next((t for t in idle if not t.has_state), idle[0])
            tab.busy = True
            return tab, False
        if can_open:
            self._opening[account.id] = self._opening.get(account.id, 0) + 1
            return None, True
        return None, False

    async def _checkout(self, account: Account,
                        prefer: Callable[[_Tab], bool] | None = None) -> _Tab:
        """Reserve an idle tab of ``account``, opening a new one while under the
        per-account limit. An idle tab matching ``prefer`` wins; without one, a new
        tab beats reusing (and so wiping) a tab that holds another conversation.
        Pair every call with ``_checkin``."""
        limit = max(1, self.settings.account_max_concurrency)
        dead: list[_Tab] = []
        tab: _Tab | None = None
        async with self._tab_cond:
            while True:
                tabs = self._tabs.setdefault(account.id, [])
                for t in [t for t in tabs
                          if t.closed or (not t.busy and (t.stale or t.session.closed))]:
                    tabs.remove(t)
                    dead.append(t)
                tab, to_open = self._claim_or_plan(account, tabs, limit, prefer)
                if tab is not None or to_open:
                    break
                # The pool caps requests per account, so this only waits out a tab
                # that is still being closed.
                await self._tab_cond.wait()
        for t in dead:
            await self._close_tab(t)
        if tab:
            return tab
        try:
            tab = await self._open_tab(account)
        finally:
            async with self._tab_cond:
                self._opening[account.id] -= 1
                if tab:
                    tab.busy = True
                    self._tabs.setdefault(account.id, []).append(tab)
                self._tab_cond.notify_all()
        if tab is None:
            raise UpstreamError(f"failed to open a tab for account {account.id}")
        log.info("opened tab %d for account %s", len(self._tabs[account.id]), account.id)
        return tab

    async def _checkin(self, tab: _Tab) -> None:
        # Free the tab before taking the lock: a request cancelled while waiting for
        # the lock (client gone) must not leave its tab marked busy for good.
        tab.busy = False
        await asyncio.shield(self._notify_tabs())
        if tab.stale:
            await self._close_tab(tab)

    async def _notify_tabs(self) -> None:
        async with self._tab_cond:
            self._tab_cond.notify_all()

    async def _ensure_browser(self) -> CDPSession:
        if self._browser is not None and not self._browser.closed:
            return self._browser
        log.info("reconnecting browser / CDP session...")
        assert self._chromium
        ws_url = await self._chromium.start()
        self._browser = await CDPSession.connect(ws_url)
        self._contexts.clear()
        return self._browser

    async def _context(self, account: Account) -> str:
        async with self._ctx_lock:
            browser = await self._ensure_browser()
            if ctx := self._contexts.get(account.id):
                return ctx
            try:
                res = await browser.send("Target.createBrowserContext", {"disposeOnDetach": False})
            except Exception:
                browser = await self._ensure_browser()
                res = await browser.send("Target.createBrowserContext", {"disposeOnDetach": False})
            self._contexts[account.id] = res["browserContextId"]
            return res["browserContextId"]

    async def _open_tab(self, account: Account) -> _Tab:
        if missing := muse.missing_session_cookies(account.cookies):
            raise UpstreamAuthError(f"account is missing session cookies: {', '.join(missing)}")
        browser = await self._ensure_browser()
        assert self._chromium
        context_id = await self._context(account)
        # A window per tab: Chromium throttles background tabs of a shared window,
        # which made parallel requests on one account run one after another.
        try:
            target = await browser.send(
                "Target.createTarget",
                {"url": "about:blank", "browserContextId": context_id, "newWindow": True},
            )
        except Exception:
            browser = await self._ensure_browser()
            context_id = await self._context(account)
            target = await browser.send(
                "Target.createTarget",
                {"url": "about:blank", "browserContextId": context_id, "newWindow": True},
            )
        target_id = target["targetId"]
        session = await CDPSession.connect(
            f"ws://127.0.0.1:{self._chromium.port}/devtools/page/{target_id}"
        )
        for domain in ("Page", "Runtime", "Network"):
            await session.send(f"{domain}.enable")
        await session.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
        await session.send("Page.setWebLifecycleState", {"state": "active"})
        await self._set_cookies(session, account)
        return _Tab(account.id, context_id, target_id, session)

    async def _close_tab(self, tab: _Tab) -> None:
        """Idempotent; marks the tab closed first so a cancelled close never leaves it
        counted as open or busy."""
        if tab.closed:
            return
        tab.closed, tab.busy = True, False
        await asyncio.shield(self._dispose_tab(tab))

    async def _dispose_tab(self, tab: _Tab) -> None:
        async with self._tab_cond:
            tabs = self._tabs.get(tab.account_id, [])
            if tab in tabs:
                tabs.remove(tab)
            last = not tabs and not self._opening.get(tab.account_id)
            context_id = self._contexts.pop(tab.account_id, None) if last else None
            self._tab_cond.notify_all()
        await tab.session.close()
        if self._browser and not self._browser.closed:
            with contextlib.suppress(Exception):
                await self._browser.send("Target.closeTarget", {"targetId": tab.target_id})
            if context_id:
                with contextlib.suppress(Exception):
                    await self._browser.send(
                        "Target.disposeBrowserContext", {"browserContextId": context_id}
                    )

    @staticmethod
    async def _set_cookies(session: CDPSession, account: Account) -> None:
        floor = time.time() + 3600
        for name, value in account.cookies.items():
            if not value:
                continue
            exp = account.cookie_expires.get(name) or 0
            params = {
                "name": name,
                "value": value,
                "domain": ".muse.ai",
                "path": "/",
                "secure": True,
                # Never hand Chromium a past expiry; it would drop the cookie silently.
                "expires": exp if exp > floor else time.time() + 7 * 86400,
            }
            if name.startswith("__Host-"):
                # Chromium rejects __Host- cookies that carry a Domain attribute.
                params.pop("domain")
                params["url"] = "https://muse.ai/"
            await session.send("Network.setCookie", params)

    @staticmethod
    def _forget(tab: _Tab) -> None:
        tab.warm = False
        tab.hint = None
        tab.turns = []
        tab.image_count = 0
        tab.thread_url = ""

    def _load_hot(self, tab: _Tab, account: Account) -> None:
        """Restore the last conversation after a restart or a new browser tab."""
        if tab.turns or tab.thread_url:
            return
        hot = account.meta.get("hot_page")
        if not isinstance(hot, dict):
            return
        turns = []
        for item in hot.get("turns") or []:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                turns.append((str(item[0]), str(item[1])))
        tab.turns = turns
        tab.hint = hot.get("hint") or None
        tab.thread_url = str(hot.get("url") or "")
        try:
            tab.image_count = int(hot.get("image_count") or 0)
        except (TypeError, ValueError):
            tab.image_count = 0

    def _save_hot(self, tab: _Tab, account: Account, req: ChatRequest, url: str) -> None:
        if url:
            tab.thread_url = url
        tab.hint = req.conversation_hint
        tab.turns = req.turns
        tab.image_count = len(req.images)
        account.meta["hot_page"] = {
            "hint": req.conversation_hint or "",
            "turns": [list(turn) for turn in req.turns],
            "url": tab.thread_url,
            "image_count": tab.image_count,
        }

    async def _href(self, tab: _Tab) -> str:
        try:
            return (await tab.session.evaluate("location.href")) or ""
        except CDPError:
            return ""

    async def _on_thread(self, tab: _Tab) -> bool:
        """The open page is the saved conversation and can take another message."""
        href = await self._href(tab)
        if not is_thread_url(href):
            return False
        if tab.thread_url and urlsplit(href).path.rstrip("/") != urlsplit(tab.thread_url).path.rstrip("/"):
            return False
        try:
            page = await tab.session.evaluate(dom.PAGE_STATE) or {}
            if not page.get("hasInput"):
                return False
            if "Connecting..." in (page.get("head") or ""):
                return False
            state = await self._state(tab)
        except CDPError:
            return False
        if state.get("generating"):
            return False
        if any(hint in state.get("tail", "") for hint in dom.STALL_HINTS):
            return False
        return (state.get("agentCount") or 0) < HOT_BUBBLE_LIMIT

    _SOFT_NAV_TIMEOUT = 8.0

    async def _soft_new_thread(self, tab: _Tab) -> bool:
        """Switch an already loaded muse.ai page to a blank thread without reloading it.
        False means the caller should fall back to a full navigation."""
        with contextlib.suppress(CDPError, TimeoutError):
            if not await tab.session.evaluate(dom.SOFT_NEW_THREAD, timeout=5.0):
                return False
            deadline = time.monotonic() + self._SOFT_NAV_TIMEOUT
            while time.monotonic() < deadline:
                await asyncio.sleep(0.25)
                if await tab.session.evaluate(dom.FRESH_THREAD, timeout=5.0):
                    return True
        return False

    async def _new_thread(self, tab: _Tab) -> None:
        self._forget(tab)
        if await self._soft_new_thread(tab):
            return
        await tab.session.send("Page.navigate", {"url": muse.NEW_THREAD_URL})
        deadline = time.monotonic() + self.settings.page_ready_timeout
        state: dict = {}
        while time.monotonic() < deadline:
            await asyncio.sleep(0.25)
            # A busy page can leave Runtime.evaluate unanswered while it loads; poll again.
            with contextlib.suppress(CDPError, TimeoutError):
                state = await tab.session.evaluate(dom.PAGE_STATE, timeout=5.0) or {}
                if state.get("ready"):
                    return
        head = (state.get("head") or "").lower()
        if any(h in head for h in dom.LOGIN_HINTS):
            raise UpstreamAuthError("muse.ai redirected to login; cookies are no longer valid")
        raise UpstreamTimeout("muse.ai page did not become ready")

    # ------------------------------------------------------------ input
    async def _attach(self, tab: _Tab, images: list[InputImage]) -> None:
        for idx, img in enumerate(images):
            ext = img.mime.split("/")[-1].replace("jpeg", "jpg")
            res = await tab.session.evaluate(
                dom.attach_file(base64.b64encode(img.data).decode(), img.mime, f"input_{idx}.{ext}")
            )
            if not (res or {}).get("ok"):
                raise UpstreamError(f"failed to attach image: {(res or {}).get('err')}")
        if images:
            await asyncio.sleep(1.0)

    async def _send(self, tab: _Tab, prompt: str) -> None:
        res = await tab.session.evaluate(dom.fill_input(prompt))
        if not (res or {}).get("ok"):
            raise UpstreamError(f"cannot fill chat input: {(res or {}).get('err')}")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if await tab.session.evaluate(dom.CLICK_SEND) == "clicked":
                return
            await asyncio.sleep(0.1)
        for kind in ("keyDown", "keyUp"):
            await tab.session.send(
                "Input.dispatchKeyEvent",
                {"type": kind, "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13,
                 **({"text": "\r"} if kind == "keyDown" else {})},
            )
        await asyncio.sleep(0.5)
        if not await tab.session.evaluate(dom.INPUT_EMPTY):
            raise UpstreamError("send button not found and Enter did not submit")

    async def _state(self, tab: _Tab) -> dict:
        state = await tab.session.evaluate(dom.CHAT_STATE) or {}
        tail = state.get("tail", "")
        low = tail.lower()
        if hit := next((h for h in dom.QUOTA_HINTS if h in low), None):
            i = low.find(hit)
            snippet = " ".join(tail[max(0, i - 120):i + 120].split())
            # The page tail also holds the prompt and the thread-history sidebar, and
            # matching it cooled down accounts whose images muse.ai went on to deliver.
            # Only a finished agent reply that says so counts; anything else is logged.
            reply = state.get("lastText", "")
            if not state.get("generating") and hit in reply.lower():
                log.warning("quota hint %r in reply on %s: %s", hit,
                            tab.thread_url or "new thread", snippet)
                raise UpstreamQuotaError(f"muse.ai reports the account is out of quota: …{snippet}…")
            if snippet != tab.quota_note:
                tab.quota_note = snippet
                log.info("ignoring quota hint %r outside the reply on %s: %s", hit,
                         tab.thread_url or "new thread", snippet)
        return state

    async def _prepare(self, tab: _Tab, prompt: str, images: list[InputImage]) -> dict:
        await self._new_thread(tab)
        base = await self._state(tab)
        await self._attach(tab, images)
        await self._send(tab, prompt)
        return base

    async def _deny_approvals(self, tab: _Tab) -> None:
        denied = await tab.session.evaluate(dom.DENY_APPROVALS) or []
        for what in denied:
            log.info("denied muse.ai %s", what.lower())

    # ------------------------------------------------------------ chat
    async def _live_continuation(self, tab: _Tab) -> bool:
        """This tab already shows the stored conversation, even if its URL is still /thread/new."""
        last_user = next((text for role, text in reversed(tab.turns) if role == "user"), "")
        if not last_user:
            return False
        try:
            page = await tab.session.evaluate(dom.PAGE_STATE) or {}
            if not page.get("hasInput"):
                return False
            state = await self._state(tab)
            present = await tab.session.evaluate(
                f"!!(document.body && document.body.innerText.includes({dom.q(last_user[:80])}))"
            )
        except CDPError:
            return False
        if state.get("generating"):
            return False
        if any(hint in state.get("tail", "") for hint in dom.STALL_HINTS):
            return False
        if (state.get("agentCount") or 0) >= HOT_BUBBLE_LIMIT:
            return False
        return bool(present)

    async def _open_saved(self, tab: _Tab) -> None:
        tab.warm = False
        await tab.session.send("Page.navigate", {"url": tab.thread_url})
        deadline = time.monotonic() + self.settings.page_ready_timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(0.25)
            if await self._on_thread(tab):
                return
        raise UpstreamTimeout("saved conversation did not become ready")

    async def _pick_chat_tab(self, account: Account, req: ChatRequest) -> _Tab:
        """Select a tab for the request: named conversations reuse their tab."""
        hint = req.conversation_hint or ""
        if hint:
            # A named conversation reuses the tab already holding it; a new one takes
            # a warm thread rather than opening a cold tab.
            def owned(t: _Tab) -> bool:
                return t.has_state and (t.hint or "") == hint

            has_own = any(owned(t) for t in self._tabs.get(account.id, []))
            return await self._checkout(
                account, prefer=owned if has_own else (lambda t: owned(t) or t.warm))
        # No identity (no `user` field): never latch onto a tab another
        # anonymous request was using, or two clients without `user`
        # would share one thread. Prefer a fresh/stateless tab instead.
        return await self._checkout(account, prefer=lambda t: not t.has_state)

    async def _open_fresh_thread(self, tab: _Tab, account: Account, hint: str) -> None:
        """Drop any stale hot-page hint and start a new thread on ``tab``."""
        saved = account.meta.get("hot_page")
        if isinstance(saved, dict) and (saved.get("hint") or "") == hint:
            account.meta.pop("hot_page", None)
        if tab.warm:
            log.info("using a warm thread")
            self._forget(tab)
        else:
            log.info("opening a new thread")
            await self._new_thread(tab)

    async def _continue_existing_thread(self, tab: _Tab, follow: str,
                                        images: list[InputImage]) -> tuple[str, list[InputImage]] | None:
        """Continue the tab's thread when usable; else return None to open fresh."""
        if await self._on_thread(tab) or await self._live_continuation(tab):
            log.info("reusing hot page %s", tab.thread_url or "open page")
            return follow, images[tab.image_count:]
        if tab.thread_url:
            log.info("reopening %s", tab.thread_url)
            await self._open_saved(tab)
            return follow, images[tab.image_count:]
        return None

    async def _resolve_thread(self, tab: _Tab, account: Account, req: ChatRequest,
                              hint: str) -> tuple[str, list[InputImage]]:
        """Decide whether to continue the tab's thread or open a fresh one."""
        # The saved conversation (restored after a restart) may only go into a
        # tab while no other tab of the account already holds a conversation.
        if hint and not any(t.has_state for t in self._tabs.get(account.id, []) if t is not tab):
            self._load_hot(tab, account)
        # An anonymous request never continues a thread, even if the tab
        # it got (e.g. the at-limit fallback, or a restored hot page)
        # happens to hold one: without an identity there is no way to
        # know it is the same client. It opens a new thread instead and
        # sends the full prompt, which keeps the model in context.
        same_owner = bool(hint) and (tab.hint or "") == hint
        follow = followup_text(tab.turns, req.turns) if tab.turns and same_owner else None
        if follow and len(req.images) >= tab.image_count and (
            tab.thread_url or await self._live_continuation(tab)
        ):
            continued = await self._continue_existing_thread(tab, follow, req.images)
            if continued is not None:
                return continued
        await self._open_fresh_thread(tab, account, hint)
        return req.prompt, req.images

    async def _begin_chat(self, account: Account, req: ChatRequest):
        """Send ``req`` on the saved conversation when it continues that page."""
        hint = req.conversation_hint or ""
        tab = await self._pick_chat_tab(account, req)
        try:
            prompt, images = await self._resolve_thread(tab, account, req, hint)
            base = await self._state(tab)
            await self._attach(tab, images)
            await self._send(tab, prompt)
            return tab, base
        except CDPError as exc:
            await self._close_tab(tab)
            await self._checkin(tab)
            raise UpstreamError(f"browser error: {exc}") from exc
        except BaseException:
            self._forget(tab)
            await self._checkin(tab)
            raise

    def _enforce_stream_deadlines(self, req: ChatRequest, elapsed: float,
                                  got_first: bool) -> None:
        """Raise when the stream exceeds the overall or first-token deadlines."""
        if elapsed > req.timeout:
            raise UpstreamTimeout("assistant reply timed out")
        if not got_first and elapsed > req.first_token_timeout:
            raise UpstreamTimeout("no first token from assistant")

    @staticmethod
    def _raise_if_stalled(elapsed: float, st: dict) -> None:
        """Raise when the upstream workspace is stuck connecting."""
        if elapsed > STALL_GIVE_UP and any(h in st.get("tail", "") for h in dom.STALL_HINTS):
            raise UpstreamTimeout("upstream workspace is stuck connecting")

    @staticmethod
    def _stream_settled(st: dict, stable: int) -> bool:
        """True when the reply has been stable long enough to finalise."""
        return (not st.get("generating") and stable >= STABLE_POLLS_DONE) or stable >= STABLE_POLLS_FORCE

    async def _finish_stream(self, tab: _Tab, account: Account, req: ChatRequest,
                             finished: bool) -> None:
        """Persist/release the tab after the stream ends."""
        # Only a named conversation is kept: an anonymous one can never be
        # continued, so holding it would just keep the tab from counting as fresh.
        if finished and req.conversation_hint:
            url = ""
            for _ in range(10):
                url = await self._href(tab)
                if is_thread_url(url):
                    break
                await asyncio.sleep(0.2)
            self._save_hot(tab, account, req, url if is_thread_url(url) else "")
            if tab.thread_url:
                log.info("saved hot page %s", tab.thread_url)
            else:
                log.info("reply finished but the thread url was not ready yet")
        else:
            self._forget(tab)
        await self._checkin(tab)
        self.prewarm(account)

    @dataclass
    class _StreamState:
        """Mutable polling state shared across stream iterations."""
        base_count: int
        base_text: str
        started: float
        emitted: str = ""
        last: str | None = None
        stable: int = 0
        got_first: bool = False

    async def _poll_stream(self, tab: _Tab, req: ChatRequest,
                           state: _StreamState) -> tuple[_StreamState, bool, str | None]:
        """Poll the tab once; returns (state, finished, delta)."""
        if req.cancel and req.cancel.is_set():
            return state, True, None
        elapsed = time.monotonic() - state.started
        self._enforce_stream_deadlines(req, elapsed, state.got_first)
        await asyncio.sleep(POLL_INTERVAL)
        st = await self._state(tab)
        if st.get("approval"):
            await self._deny_approvals(tab)
            return state, False, None
        text = st.get("lastText", "")
        is_new = st.get("agentCount", 0) > state.base_count or (text and text != state.base_text)
        if not is_new or not text:
            self._raise_if_stalled(elapsed, st)
            return state, False, None
        if text != state.last:
            previous = state.emitted
            state.emitted = text
            state.last = text
            state.stable = 0
            state.got_first = True
            delta = text[len(previous):] if text.startswith(previous) else text
            return state, False, delta
        state.stable += 1
        state.got_first = True
        if self._stream_settled(st, state.stable):
            return state, True, None
        return state, False, None

    async def chat_stream(self, account: Account, req: ChatRequest) -> AsyncIterator[str]:
        tab, base = await self._begin_chat(account, req)
        state = self._StreamState(
            base_count=base.get("agentCount", 0),
            base_text=base.get("lastText", ""),
            started=time.monotonic(),
        )
        finished = False
        try:
            while True:
                state, finished, delta = await self._poll_stream(tab, req, state)
                if finished:
                    return
                if delta:
                    yield delta
        except CDPError as exc:
            await self._close_tab(tab)
            raise UpstreamError(f"browser error: {exc}") from exc
        finally:
            await self._finish_stream(tab, account, req, finished)

    # ------------------------------------------------------------ warm threads
    _WARM_REPLY_TIMEOUT = 90.0
    _WARM_RETRY_DELAY = 60.0
    _WARM_MAX_FAILED_ROUNDS = 3

    def prewarm(self, account: Account) -> None:
        """Top up ``account``'s warm threads in the background (``MUSE2API_WARM_THREADS``)."""
        if self.settings.warm_threads <= 0 or self._browser is None:
            return
        task = self._warming.get(account.id)
        if task and not task.done():
            return
        self._warming[account.id] = asyncio.create_task(self._fill_warm(account))

    async def _fill_warm(self, account: Account) -> None:
        """Warm the missing threads in parallel, round after round, until the account has
        enough. A failed round (a cold page load often times out) is retried after a
        pause, up to ``_WARM_MAX_FAILED_ROUNDS`` in a row."""
        failed = 0
        while True:
            batch: list[_Tab] = []
            while (tab := await self._checkout_for_warming(account, len(batch))) is not None:
                batch.append(tab)
            if not batch:
                return
            results = await asyncio.gather(*(self._warm_one(account, t) for t in batch))
            if all(results):
                failed = 0
                continue
            failed += 1
            if failed >= self._WARM_MAX_FAILED_ROUNDS:
                return
            await asyncio.sleep(self._WARM_RETRY_DELAY)

    async def _warm_one(self, account: Account, tab: _Tab) -> bool:
        try:
            await self._warm_up(tab)
        except Exception as exc:
            self._forget(tab)
            if isinstance(exc, CDPError):
                await self._close_tab(tab)
            log.warning("warming a thread for %s failed: %s", account.id, exc)
            return False
        finally:
            await self._checkin(tab)
        log.info("warm thread ready for account %s", account.id)
        return True

    async def _checkout_for_warming(self, account: Account, pending: int = 0) -> _Tab | None:
        """A tab to warm, but only while the account has warm threads missing (counting
        the ``pending`` ones being warmed) and a slot is still left for a real request."""
        limit = max(1, self.settings.account_max_concurrency)
        async with self._tab_cond:
            tabs = [t for t in self._tabs.get(account.id, []) if not t.closed]
            if sum(t.warm for t in tabs) + pending >= self.settings.warm_threads:
                return None
            idle = [t for t in tabs if not (t.busy or t.stale or t.session.closed)]
            free = len(idle) + limit - len(tabs) - self._opening.get(account.id, 0)
            if free < 2:
                return None
        return await self._checkout(account, prefer=lambda t: not t.has_state and not t.warm)

    async def _warm_up(self, tab: _Tab) -> None:
        await self._new_thread(tab)
        base = (await self._state(tab)).get("agentCount", 0)
        await self._send(tab, self.settings.warm_prompt)
        deadline = time.monotonic() + self._WARM_REPLY_TIMEOUT
        last, stable = None, 0
        while time.monotonic() < deadline:
            await asyncio.sleep(POLL_INTERVAL)
            st = await self._state(tab)
            if st.get("approval"):
                await self._deny_approvals(tab)
                continue
            text = st.get("lastText", "")
            if st.get("agentCount", 0) <= base or not text:
                continue
            if text != last:
                last, stable = text, 0
            elif not st.get("generating") and (stable := stable + 1) >= STABLE_POLLS_DONE:
                tab.warm = True
                return
        raise UpstreamTimeout("no reply to the warm-up message")

    # ------------------------------------------------------------ media
    @staticmethod
    def _media_prompt(prompt: str, kind: str, size: str | None, duration: int | None = None) -> str:
        hints = []
        if size:
            hints.append(f"aspect ratio {size}")
        if duration:
            hints.append(f"{duration} seconds long")
        verb = "Generate an image" if kind == "image" else "Generate a video"
        suffix = f" ({', '.join(hints)})" if hints else ""
        return f"{verb}{suffix}: {prompt}"

    # muse.ai posts a completion sentence ("Here's your video…") a few seconds
    # before the media attachment's blob src is ready, so a reply that is "text
    # only" right now may still be finalising. Keep waiting this long for the
    # attachment to appear/load before treating the reply as a refusal.
    _MEDIA_GRACE = {"video": 90.0, "image": 8.0}
    # The timeout is soft while muse.ai is visibly still working (stop button
    # shown or attachment loading): giving up then threw away images that landed
    # a minute later, and the failover started the whole generation over.
    _OVERRUN_FACTOR = 2.5

    async def _text_only_action(self, tab: _Tab, kind: str, text: str,
                                grace: float, denied: bool, asked: bool) -> tuple[str, str | None]:
        """Handle a text-only reply: ask once, then probe for a link, else refuse."""
        if not asked:
            # The agent often finishes with the file only in its workspace:
            # after a denied upload, after post-processing that it shows as
            # an image gallery (raw, QC crops and final mixed together), or
            # when it recognises a repeat request and points at the earlier
            # file. Asking once gets the final file attached in chat.
            log.info("%s not attached (%s); asking for it in chat: %.120s", kind,
                     "denied approval" if denied else "text-only reply", text)
            await self._send(tab, f"Please attach the final {kind} file here in "
                                  "the chat" + (" instead of uploading it." if denied
                                                else ", as a single attachment."))
            return "ask", None
        probe = await tab.session.evaluate(dom.LAST_BUBBLE_MEDIA) or {}
        url = self._pick_media_url(probe.get("links", []), kind)
        if url:
            log.info("%s delivered as link in text bubble: %s", kind, url)
            return "link", url
        log.info("%s: no media after %.0fs grace; links=%s html=%.300s",
                 kind, grace, probe.get("links", []), probe.get("html", ""))
        return "refuse", text

    async def _settle_text_only(self, tab: _Tab, st: dict, kind: str,
                                ctx: dict) -> tuple[str, dict | None]:
        """Wait out text-only replies; ask once, then probe links or refuse."""
        text = st.get("lastText", "")
        if not (st.get("agentCount", 0) > ctx["base_count"] and text
                and not st.get("generating")):
            ctx["text_done_at"] = None
            return "continue", None
        now = time.monotonic()
        if ctx["text_done_at"] is None:
            ctx["text_done_at"] = now
            return "continue", None
        if now - ctx["text_done_at"] < ctx["grace"]:
            return "continue", None
        action, payload = await self._text_only_action(
            tab, kind, text, ctx["grace"], ctx["denied"], ctx["asked"])
        if action == "ask":
            ctx["asked"] = True
            ctx["text_done_at"] = None
            ctx["base_count"] = st.get("agentCount", 0)
            return "continue", None
        if action == "link":
            return "link", {"src": payload, "kind": kind}
        raise UpstreamRefused(f"upstream replied with text only: {text[:200]}")

    async def _step_media(self, tab: _Tab, st: dict, kind: str,
                          ctx: dict) -> tuple[str, dict | None]:
        """Advance one media-wait iteration; returns (action, payload)."""
        new_atts = st.get("attachments", [])[ctx["base_atts"]:]
        fresh = [a for a in new_atts if a.get("src") and a.get("kind") == kind]
        if fresh:
            return "fresh", fresh[-1]
        ctx["busy"] = bool(st.get("generating")) or any(
            kind in (a.get("tid") or "") for a in new_atts)
        # An attachment node for this kind is on the page but its (blob) src
        # has not loaded yet -> media is still finalising, keep waiting.
        if any(kind in (a.get("tid") or "") for a in new_atts):
            ctx["text_done_at"] = None
            return "continue", None
        return await self._settle_text_only(tab, st, kind, ctx)

    async def _wait_media(self, tab: _Tab, base: dict, kind: str, timeout: float,
                          on_progress, cancel: asyncio.Event | None) -> dict:
        ctx = {
            "base_atts": len(base.get("attachments", [])),
            "base_count": base.get("agentCount", 0),
            "grace": self._MEDIA_GRACE.get(kind, 8.0),
            "denied": False,
            "asked": False,
            "text_done_at": None,
            "busy": False,
        }
        started = time.monotonic()
        while True:
            elapsed = time.monotonic() - started
            if elapsed >= timeout and not (ctx["busy"] and elapsed < timeout * self._OVERRUN_FACTOR):
                break
            if cancel and cancel.is_set():
                raise asyncio.CancelledError
            await asyncio.sleep(0.6)
            st = await self._state(tab)
            if st.get("approval"):
                await self._deny_approvals(tab)
                ctx["denied"], ctx["text_done_at"] = True, None
                continue
            if on_progress:
                on_progress(min(95, int(elapsed / timeout * 100)))
            outcome, payload = await self._step_media(tab, st, kind, ctx)
            if outcome in ("fresh", "link"):
                return payload  # type: ignore[return-value]  # payload non-None for these outcomes
        if ctx["busy"]:
            log.warning("%s still generating after %.0fs; giving up", kind, elapsed)
        raise UpstreamTimeout(f"{kind} generation timed out")

    _VIDEO_EXT = (".mp4", ".webm", ".mov", ".m4v")
    _IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif")
    _MEDIA_HOSTS = ("videodelivery.net", "cloudflarestream.com", "cloudflarestorage.com",
                    "amazonaws.com", "storage.googleapis.com", "blob.core.windows.net",
                    "musecdn", "cdn.muse", "muse-cdn")
    _MEDIA_PATHS = ("/media/", "/download", "/dl/", "/video", "/attachment", "/files/")

    @classmethod
    def _pick_media_url(cls, links: list[str], kind: str) -> str | None:
        """Best media URL from a reply bubble, or None if nothing looks like one."""
        exts = cls._VIDEO_EXT if kind == "video" else cls._IMAGE_EXT

        def score(u: str) -> int:
            low = u.lower()
            path = low.split("?", 1)[0].split("#", 1)[0]
            if u.startswith("blob:") or path.endswith(exts):
                return 3
            if any(h in low for h in cls._MEDIA_HOSTS):
                return 2
            if any(seg in low for seg in cls._MEDIA_PATHS):
                return 1
            return 0

        cands = [u for u in links if u and not u.startswith(("mailto:", "javascript:"))]
        cands.sort(key=score, reverse=True)
        return cands[0] if cands and score(cands[0]) > 0 else None

    async def _download(self, tab: _Tab, att: dict, kind: str) -> MediaResult:
        res = await tab.session.evaluate(dom.fetch_as_base64(att["src"]), await_promise=True,
                                         timeout=300)
        if not (res or {}).get("ok"):
            raise UpstreamError(f"failed to download generated {kind}: {(res or {}).get('err')}")
        mime = res.get("mime") or ("video/mp4" if kind == "video" else "image/png")
        media_kind = cast(Literal["image", "video"], kind)
        return MediaResult(data=base64.b64decode(res["b64"]), mime=mime, kind=media_kind,
                           width=att.get("w") or None, height=att.get("h") or None)

    async def _generate(self, account: Account, prompt: str, images: list[InputImage],
                        kind: str, timeout: float, on_progress, cancel) -> MediaResult:
        # Media always starts a fresh thread, so keep tabs holding a chat (or a warm
        # thread) for chats.
        tab = await self._checkout(account, prefer=lambda t: not t.has_state and not t.warm)
        try:
            base = await self._prepare(tab, prompt, images)
            att = await self._wait_media(tab, base, kind, timeout, on_progress, cancel)
            return await self._download(tab, att, kind)
        except CDPError as exc:
            await self._close_tab(tab)
            raise UpstreamError(f"browser error: {exc}") from exc
        finally:
            await self._checkin(tab)

    async def generate_image(self, account: Account, req: ImageRequest) -> list[MediaResult]:
        prompt = self._media_prompt(req.prompt, "image", req.size)
        results = []
        for _ in range(max(1, req.n)):
            r = await self._generate(account, prompt, req.reference_images, "image",
                                     req.timeout, req.on_progress, req.cancel)
            r.revised_prompt = req.prompt
            results.append(r)
        return results

    async def generate_video(self, account: Account, req: VideoRequest) -> MediaResult:
        prompt = self._media_prompt(req.prompt, "video", req.size, req.duration)
        images = [req.first_frame] if req.first_frame else []
        return await self._generate(account, prompt, images, "video", req.timeout,
                                    req.on_progress, req.cancel)

    # ------------------------------------------------------------ session
    async def renew_session(self, account: Account) -> SessionInfo:
        info = await muse.renew_session(account.cookies)
        # Cookies live in the account's browser context, so writing the rotated ones
        # through any open tab updates every tab and keeps hot and warm pages alive.
        async with self._tab_cond:
            live = [t for t in self._tabs.get(account.id, [])
                    if not (t.closed or t.stale or t.session.closed)]
        if live:
            rotated = account.model_copy(update={
                "cookies": {**account.cookies, **info.cookies},
                "cookie_expires": {**account.cookie_expires, **info.cookie_expires},
            })
            with contextlib.suppress(CDPError, TimeoutError):
                await self._set_cookies(live[0].session, rotated)
                return info
        # Otherwise retire the account's tabs so new ones load the rotated cookies; a
        # tab in the middle of a request is closed when it is checked back in.
        async with self._tab_cond:
            tabs = self._tabs.get(account.id, [])
            for tab in tabs:
                tab.stale = True
            idle = [t for t in tabs if not t.busy]
        for tab in idle:
            await self._close_tab(tab)
        return info

    # ------------------------------------------------------------ quota
    _QUOTA_CLICK_TIMEOUT = 8.0
    _QUOTA_READ_TIMEOUT = 12.0
    _ESCAPE_PAUSE = 0.1
    """Lets the UI finish closing before the next key or click."""

    def forget_quota(self, account_id: str) -> None:
        """Drop the cached snapshot so the next ``quota`` call reads the page again."""
        self._quota_cache.pop(account_id, None)

    def _cached_quota(self, account_id: str) -> dict[str, Any] | None:
        hit = self._quota_cache.get(account_id)
        if hit and time.time() - hit["fetched_at"] < quota_cache_seconds():
            return dict(hit)
        return None

    async def quota(self, account: Account) -> dict[str, Any]:
        """Read Settings -> Usage on an idle tab of ``account``.

        Returns ``plan``, ``weekly_used_pct``, ``resets_at``, ``extra_left``, ``found``,
        ``source`` ("browser") and ``fetched_at``. Cached per account for
        ``MUSE2API_QUOTA_CACHE_S`` (30 min) so the dashboard cannot hammer muse.ai;
        ``forget_quota`` bypasses it. Raises ``AccountBusy`` rather than touch a tab
        that is serving a request."""
        if hit := self._cached_quota(account.id):
            return hit
        async with self._quota_locks.setdefault(account.id, asyncio.Lock()):
            if hit := self._cached_quota(account.id):
                return hit
            tab = await self._checkout_idle(account)
            try:
                parsed = await self._read_quota(tab)
            except CDPError as exc:
                await self._close_tab(tab)
                raise UpstreamError(f"browser error: {exc}") from exc
            finally:
                await self._checkin(tab)
            snapshot = {**parsed, "source": "browser", "fetched_at": time.time()}
            self._quota_cache[account.id] = snapshot
            return dict(snapshot)

    async def _checkout_idle(self, account: Account) -> _Tab:
        """An idle tab of ``account`` without waiting; a fresh tab only when the account
        has none open or opening. Raises ``AccountBusy`` when every tab is in use."""
        async with self._tab_cond:
            tabs = [t for t in self._tabs.get(account.id, []) if not t.closed]
            idle = [t for t in tabs if not (t.busy or t.stale or t.session.closed)]
            if idle:
                # Prefer a tab without a conversation; the dialog does not navigate anyway.
                tab = next((t for t in idle if not t.has_state), idle[0])
                tab.busy = True
                return tab
            if any(t.busy for t in tabs) or self._opening.get(account.id):
                raise AccountBusy(f"account '{account.id}' is serving a request; "
                                  "try the quota again when it is idle")
        return await self._checkout(account, prefer=lambda t: not t.has_state)

    async def _click_js(self, tab: _Tab, expr: str) -> bool:
        """Click the centre of the element ``expr`` locates, waiting for it to appear."""
        deadline = time.monotonic() + self._QUOTA_CLICK_TIMEOUT
        while True:
            pt = await tab.session.evaluate(expr)
            if isinstance(pt, dict) and "x" in pt and "y" in pt:
                for kind in ("mousePressed", "mouseReleased"):
                    await tab.session.send("Input.dispatchMouseEvent", {
                        "type": kind, "x": pt["x"], "y": pt["y"], "button": "left",
                        "clickCount": 1})
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.15)

    async def _escape(self, tab: _Tab) -> None:
        for _ in range(2):
            for kind in ("keyDown", "keyUp"):
                await tab.session.send("Input.dispatchKeyEvent", {
                    "type": kind, "key": "Escape", "code": "Escape",
                    "windowsVirtualKeyCode": 27, "nativeVirtualKeyCode": 27})
            await asyncio.sleep(self._ESCAPE_PAUSE)

    async def _read_quota(self, tab: _Tab) -> dict[str, Any]:
        page = await tab.session.evaluate(dom.PAGE_STATE) or {}
        if not page.get("hasInput"):  # a fresh tab is still on about:blank
            await self._new_thread(tab)
        await self._escape(tab)  # a menu left open would swallow the first click
        try:
            if not await self._click_js(tab, dom.QUOTA_SETTINGS_ENTRY):
                return {**dom.parse_usage_text(""), "error": "settings entry not found"}
            if not await self._click_js(tab, dom.QUOTA_MENU_ITEM):
                return {**dom.parse_usage_text(""), "error": "settings menu item not found"}
            deadline = time.monotonic() + self._QUOTA_READ_TIMEOUT
            while True:
                parsed = dom.parse_usage_text(await tab.session.evaluate(dom.QUOTA_DIALOG_TEXT))
                if parsed["found"] or time.monotonic() >= deadline:
                    return parsed
                await asyncio.sleep(0.2)
        finally:
            # Never hand a tab back with the dialog open: close it, and retire the
            # tab when that did not work.
            try:
                await self._escape(tab)
                if await tab.session.evaluate(dom.QUOTA_DIALOG_TEXT):
                    tab.stale = True
            except Exception:
                tab.stale = True
            if tab.stale:
                log.warning("settings dialog stayed open on a tab of %s; retiring it",
                            tab.account_id)
