"""Facts about the muse.ai web app shared by all drivers.

Everything here was observed from the public web client and may change without
notice. Keep upstream-specific constants in this module so that a site update
is a one-file fix.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, NamedTuple, cast

import httpx

from ..drivers.base import SessionInfo
from ..errors import UpstreamAuthError, UpstreamError

SITE = "https://muse.ai"
NEW_THREAD_URL = f"{SITE}/thread/new"
SESSION_API = f"{SITE}/api/session"
VM_WAKE_API = f"{SITE}/api/hatch/vm/wake"

# Cookies that together make up a logged-in session.
# Cookies an account must have. As of 2026-10 muse.ai no longer sets hatch_gw /
# hatch_vml for every logged-in session, so they are optional: forwarded when
# present, but not required.
SESSION_COOKIES = ("hatch_sess", "hatch_native_auth_device")
OPTIONAL_SESSION_COOKIES = ("hatch_gw", "hatch_vml")

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)


# A wake request is only acknowledged with HTTP 200; the VM is up once the session
# endpoint reports it RUNNING. Poll that for at most this many seconds.
WAKE_CONFIRM_SECONDS = 20.0
WAKE_POLL_SECONDS = 2.0


def wake_confirm_seconds() -> float:
    """``MUSE2API_WAKE_CONFIRM_S`` (read here: config.py has no knob for it)."""
    try:
        return max(0.0, float(os.environ.get("MUSE2API_WAKE_CONFIRM_S", WAKE_CONFIRM_SECONDS)))
    except ValueError:
        return WAKE_CONFIRM_SECONDS


def cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if v)


def missing_session_cookies(cookies: dict[str, str]) -> list[str]:
    return [name for name in SESSION_COOKIES if not cookies.get(name)]


def _merge_cookies(resp: httpx.Response, jar: dict[str, str], expires: dict[str, int]) -> None:
    for c in resp.cookies.jar:
        if c.value:
            jar[c.name] = c.value
        if c.expires:
            expires[c.name] = c.expires


def _json_body(resp: httpx.Response) -> dict[str, Any]:
    if not resp.headers.get("content-type", "").startswith("application/json"):
        return {}
    try:
        body = resp.json()
    except ValueError:
        return {}
    if isinstance(body, dict):
        return cast(dict[str, Any], body)
    return {}


class WakeTiming(NamedTuple):
    """Poll timing knobs for VM wake confirmation (injectable for tests)."""
    timeout: float
    confirm_s: float
    poll_s: float = WAKE_POLL_SECONDS
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep
    clock: Callable[[], float] = time.monotonic


async def renew_session(
    cookies: dict[str, str],
    *,
    wake_vm: bool = True,
    timeout: float = 15.0,
    confirm_s: float | None = None,
    poll_s: float = WAKE_POLL_SECONDS,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> SessionInfo:
    """Hit the session endpoint, which rotates short-lived cookies via Set-Cookie,
    and optionally wake the account's cloud workspace so the first request is fast.

    After the wake request, whatever it answered, the session endpoint is polled
    until it reports the VM ``RUNNING`` or ``confirm_s`` (default
    ``MUSE2API_WAKE_CONFIRM_S``, 20s) runs out. ``meta["vm_state_after"]`` is the
    last state seen while polling (None when no poll succeeded) and
    ``meta["vm_wake_ok"]`` is True for RUNNING, False for any other state seen, and
    None when no state was seen. ``meta["vm_wake_http"]`` records the wake's own
    status code (or exception name) for diagnosis."""
    headers = {
        "User-Agent": USER_AGENT,
        "Origin": SITE,
        "Referer": NEW_THREAD_URL,
        "Accept": "application/json",
    }
    jar = dict(cookies)
    expires: dict[str, int] = {}
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        resp = await client.get(SESSION_API, headers={"Cookie": cookie_header(jar)})
        if resp.status_code in (401, 403):
            message = f"session rejected by upstream (HTTP {resp.status_code})"
            raise UpstreamAuthError(message)
        if resp.status_code >= 400:
            message = f"session endpoint returned HTTP {resp.status_code}"
            raise UpstreamError(message)

        _merge_cookies(resp, jar, expires)
        body = _json_body(resp)
        # Persisted with the account, so keep only short strings.
        meta: dict[str, Any] = {
            key: (str(body[src])[:128] if body.get(src) is not None else None)
            for key, src in (("session_status", "status"), ("vm_id", "vm_id"),
                             ("vm_state", "vm_state"))
        }

        if wake_vm and meta["vm_id"] and meta["vm_state"] != "DISABLED":
            timing = WakeTiming(
                timeout=timeout,
                confirm_s=wake_confirm_seconds() if confirm_s is None else confirm_s,
                poll_s=poll_s,
                sleep=sleep,
                clock=clock,
            )
            meta.update(await _wake_vm(client, jar, expires, meta["vm_id"], timing))

    return SessionInfo(ok=meta["session_status"] == "assigned", cookies=jar,
                       cookie_expires=expires, meta=meta)


async def _wake_vm(
    client: httpx.AsyncClient, jar: dict[str, str], expires: dict[str, int],
    vm_id: str, timing: WakeTiming,
) -> dict[str, Any]:
    """POST a wake request, then poll the session endpoint for RUNNING."""
    meta: dict[str, Any] = {}
    # The wake's own answer says little: muse.ai answers 403 to a wake for a
    # VM that is already RUNNING. Judge by the state the session reports.
    try:
        w = await client.post(
            VM_WAKE_API,
            headers={"Cookie": cookie_header(jar)},
            json={"vm_id": vm_id, "retry_count": 0,
                  "connect_attempt_id": str(uuid.uuid4())},
        )
        meta["vm_wake_http"] = str(w.status_code)
    except httpx.HTTPError as exc:
        meta["vm_wake_http"] = type(exc).__name__
    meta["vm_state_after"] = await _confirm_running(client, jar, expires, timing)
    # None when no poll answered: unknown is not evidence the VM is down.
    meta["vm_wake_ok"] = (None if meta["vm_state_after"] is None
                          else meta["vm_state_after"] == "RUNNING")
    return meta


async def _confirm_running(
    client: httpx.AsyncClient, jar: dict[str, str], expires: dict[str, int],
    timing: WakeTiming,
) -> str | None:
    """Poll the session endpoint until the VM is ``RUNNING`` or ``confirm_s`` passes.
    Returns the last ``vm_state`` seen. Errors only cost that poll."""
    deadline = timing.clock() + timing.confirm_s
    state: str | None = None
    while True:
        remaining = deadline - timing.clock()
        try:
            resp = await client.get(SESSION_API, headers={"Cookie": cookie_header(jar)},
                                    timeout=max(1.0, min(timing.timeout, remaining)))
        except httpx.HTTPError:
            resp = None
        if resp is not None and resp.status_code < 400:
            _merge_cookies(resp, jar, expires)
            seen = _json_body(resp).get("vm_state")
            if seen is not None:
                state = str(seen)[:128]
                if state == "RUNNING":
                    return state
        if timing.clock() + timing.poll_s > deadline:
            return state
        await timing.sleep(timing.poll_s)
