"""Account pool: scheduling, per-account concurrency limits, health state and failover.

Usage::

    async with pool.lease(exclude={...}, hint=conversation_key) as lease:
        ... use lease.account ...
        lease.fail(exc)   # optional; success is recorded automatically otherwise
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
import types
from collections import defaultdict
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterable
from contextlib import AbstractAsyncContextManager
from functools import wraps
from typing import Any, Protocol, TypeVar, cast

from ..errors import NoAccountAvailable, UpstreamAuthError, UpstreamQuotaError, UpstreamTimeout
from .model import Account, AccountStatus
from .store import AccountStore

log = logging.getLogger(__name__)

WAKE_BLOCK_KIND = "video"
WAKE_BLOCK_SECONDS = 600
"""How long an account whose VM wake was not confirmed is skipped for video: a video
on a VM that is not RUNNING stalls for minutes."""

def _kind_blocked_until(acc: Account, kind: str) -> float:
    blocked = acc.meta.get("kind_blocked_until")
    if isinstance(blocked, dict):
        return float(cast(dict[str, Any], blocked).get(kind, 0.0))
    return 0.0


def note_wake(acc: Account, session_meta: dict[str, Any]) -> None:
    """Apply the VM wake outcome of one session renewal (``session_meta`` is that
    renewal's ``SessionInfo.meta``) through the per-kind block in
    ``meta["kind_blocked_until"]``: an unconfirmed wake blocks video for
    ``WAKE_BLOCK_SECONDS``; a confirmed one lifts that block again, but never a
    longer block set for another reason."""
    ok = session_meta.get("vm_wake_ok")
    if ok is None:  # no wake attempted this time
        return
    now = time.time()
    raw_blocks = acc.meta.get("kind_blocked_until")
    if isinstance(raw_blocks, dict):
        blocks = {
            kind: until
            for kind, until in cast(dict[str, Any], raw_blocks).items()
            if until > now
        }
    else:
        blocks = {}
    ours = acc.meta.pop("wake_blocked_until", None)
    if not ok:
        until = now + WAKE_BLOCK_SECONDS
        if blocks.get(WAKE_BLOCK_KIND, 0.0) < until:
            blocks[WAKE_BLOCK_KIND] = until
            acc.meta["wake_blocked_until"] = until
            log.warning("account %s vm wake not confirmed (state %s); no video on it for %ds",
                        acc.id, session_meta.get("vm_state_after"), WAKE_BLOCK_SECONDS)
    elif ours is not None and blocks.get(WAKE_BLOCK_KIND) == ours:
        del blocks[WAKE_BLOCK_KIND]
    acc.meta["kind_blocked_until"] = blocks


# ---------------------------------------------------------------- strategies
class Strategy(Protocol):
    def choose(self, candidates: list[Account], hint: str | None) -> Account: ...


class RoundRobinStrategy:
    def __init__(self) -> None:
        self._counter = itertools.count()

    def choose(self, candidates: list[Account], hint: str | None = None) -> Account:
        _ = hint
        ordered = sorted(candidates, key=lambda a: a.id)
        return ordered[next(self._counter) % len(ordered)]


class LRUStrategy:
    def choose(self, candidates: list[Account], hint: str | None = None) -> Account:
        _ = hint
        return min(candidates, key=lambda a: a.last_used_at)


class AffinityStrategy:
    """Sticks a hint (e.g. conversation id / user id) to the account that served it last,
    so a driver can reuse warm browser tabs or upstream threads. Falls back to LRU."""

    def __init__(self, max_entries: int = 4096) -> None:
        self._map: dict[str, str] = {}
        self._max = max_entries
        self._fallback = LRUStrategy()

    def choose(self, candidates: list[Account], hint: str | None) -> Account:
        if hint and (acc_id := self._map.get(hint)):
            for acc in candidates:
                if acc.id == acc_id:
                    return acc
        acc = self._fallback.choose(candidates, hint)
        if hint:
            if len(self._map) >= self._max:
                self._map.pop(next(iter(self._map)))
            self._map[hint] = acc.id
        return acc


STRATEGIES: dict[str, type] = {
    "round_robin": RoundRobinStrategy,
    "lru": LRUStrategy,
    "affinity": AffinityStrategy,
}


# ---------------------------------------------------------------- lease
class Lease:
    def __init__(self, account: Account) -> None:
        self.account = account
        self.error: BaseException | None = None

    def fail(self, exc: BaseException) -> None:
        self.error = exc


# ---------------------------------------------------------------- pool
_T = TypeVar("_T")


class _AsyncContextManager(AbstractAsyncContextManager[_T]):
    """Compact async-context-manager decorator (stdlib-compatible semantics)."""

    def __init__(self, func: Callable[..., AsyncIterator[_T]], args: tuple[Any, ...],
                 kwargs: dict[str, Any]) -> None:
        self._func = func
        self._args = args
        self._kwargs = kwargs
        self._agen: AsyncGenerator[_T, Any] | None = None

    async def __aenter__(self) -> _T:
        agen = self._func(*self._args, **self._kwargs)
        self._agen = cast(AsyncGenerator[_T, Any], agen)
        return await self._agen.__anext__()

    async def __aexit__(self, typ: type[BaseException] | None,
                        value: BaseException | None,
                        traceback: types.TracebackType | None) -> bool:
        if self._agen is None:
            return False
        try:
            if typ is not None:
                await self._agen.athrow(cast(BaseException, value))
            else:
                await self._agen.__anext__()
        except StopAsyncIteration:
            return False
        except BaseException as exc:
            if typ is not None and issubclass(typ, type(exc)):
                return False
            raise
        return False


def _asynccontextmanager(
    func: Callable[..., AsyncIterator[_T]],
) -> Callable[..., _AsyncContextManager[_T]]:
    @wraps(func)
    def helper(*args: Any, **kwargs: Any) -> _AsyncContextManager[_T]:
        return _AsyncContextManager(func, args, kwargs)

    return helper


class AccountPool:
    def __init__(
        self,
        store: AccountStore,
        *,
        strategy: str = "lru",
        max_concurrency: int = 1,
        cooldown: float = 120.0,
        acquire_timeout: float = 60.0,
    ) -> None:
        self.store = store
        self.strategy: Strategy = STRATEGIES[strategy]()
        self.max_concurrency = max(1, max_concurrency)
        self.cooldown = cooldown
        self.acquire_timeout = acquire_timeout
        self._accounts: dict[str, Account] = {}
        self._inflight: dict[str, int] = defaultdict(int)
        self._cond = asyncio.Condition()

    # ---- lifecycle / CRUD ----
    async def load(self) -> None:
        self._accounts = {a.id: a for a in await self.store.load()}
        log.info("loaded %d account(s)", len(self._accounts))

    async def persist(self) -> None:
        await self.store.save(list(self._accounts.values()))

    def all(self) -> list[Account]:
        return list(self._accounts.values())

    def get(self, account_id: str) -> Account | None:
        return self._accounts.get(account_id)

    async def upsert(self, account: Account) -> Account:
        async with self._cond:
            self._accounts[account.id] = account
            self._cond.notify_all()
        await self.persist()
        return account

    async def remove(self, account_id: str) -> bool:
        async with self._cond:
            removed = self._accounts.pop(account_id, None) is not None
        if removed:
            await self.persist()
        return removed

    def stats(self) -> dict[str, int]:
        now = time.time()
        return {
            "total": len(self._accounts),
            "available": sum(1 for a in self._accounts.values() if a.is_available(now)),
            "invalid": sum(1 for a in self._accounts.values() if a.status == AccountStatus.INVALID),
            "inflight": sum(self._inflight.values()),
        }

    # ---- scheduling ----
    def _kind_blocked(self, kind: str | None, now: float) -> set[str]:
        """Ids of accounts still blocked for ``kind`` (see ``note_wake``). Empty when
        every usable account is blocked: a block must never make the kind unusable
        altogether, so then everyone gets tried again."""
        if kind is None:
            return set()
        usable = [a for a in self._accounts.values()
                  if a.enabled and a.status != AccountStatus.INVALID]
        blocked = {a.id for a in usable if _kind_blocked_until(a, kind) > now}
        return set() if len(blocked) == len(usable) else blocked

    def _candidates(
        self, exclude: Iterable[str], kind: str | None = None,
    ) -> tuple[list[Account], bool, float | None]:
        """Returns (free candidates, whether a usable account is merely busy, and the
        earliest time a cooling account comes back, if any)."""
        now = time.time()
        excluded = set(exclude) | self._kind_blocked(kind, now)
        free: list[Account] = []
        busy = False
        back_at: float | None = None
        for acc in self._accounts.values():
            if acc.id in excluded or not acc.enabled or acc.status == AccountStatus.INVALID:
                continue
            if not acc.is_available(now):  # cooling down
                back_at = acc.cooldown_until if back_at is None else min(back_at, acc.cooldown_until)
                continue
            if self._inflight[acc.id] >= self.max_concurrency:
                busy = True
                continue
            free.append(acc)
        return free, busy, back_at

    def has_alternative(self, exclude: Iterable[str], kind: str | None = None) -> bool:
        """Whether an enabled, valid account exists outside ``exclude`` (busy or cooling counts)
        that is not blocked for ``kind``."""
        excluded = set(exclude) | self._kind_blocked(kind, time.time())
        return any(a.id not in excluded and a.enabled and a.status != AccountStatus.INVALID
                   for a in self._accounts.values())

    def _reserve(self, free: list[Account], hint: str | None) -> Account:
        acc = self.strategy.choose(free, hint)
        self._inflight[acc.id] += 1
        acc.last_used_at = time.time()
        return acc

    def _fail_fast(self, deadline: float, *, busy: bool, back_at: float | None) -> None:
        remaining = deadline - time.monotonic()
        back_in = None if back_at is None else back_at - time.time()
        if not busy and back_in is None:
            message = "no usable account in pool"
            raise NoAccountAvailable(message)
        if remaining <= 0 or (back_in is not None and not busy and back_in > remaining):
            # Nothing frees up before the deadline; say why instead of waiting in vain.
            reason = "busy" if busy else "cooling down"
            message = f"all accounts are {reason}, try again later"
            raise NoAccountAvailable(message)

    def _next_wait(self, deadline: float, back_at: float | None) -> float:
        remaining = deadline - time.monotonic()
        back_in = None if back_at is None else back_at - time.time()
        if back_in is None:
            return remaining
        return min(remaining, max(back_in, 0.05))

    async def acquire(
        self, *, exclude: Iterable[str] = (), hint: str | None = None, kind: str | None = None,
    ) -> Account:
        exclude = tuple(exclude)
        deadline = time.monotonic() + self.acquire_timeout
        async with self._cond:
            while True:
                free, busy, back_at = self._candidates(exclude, kind)
                if free:
                    return self._reserve(free, hint)
                self._fail_fast(deadline, busy=busy, back_at=back_at)
                # Wake on release, or when the soonest cooldown ends (nothing notifies that).
                wait = self._next_wait(deadline, back_at)
                try:
                    await asyncio.wait_for(self._cond.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    continue

    async def release(self, account: Account, error: BaseException | None = None) -> None:
        async with self._cond:
            self._inflight[account.id] = max(0, self._inflight[account.id] - 1)
            self._apply_result(account, error)
            self._cond.notify_all()
        await self.persist()

    def _apply_result(self, acc: Account, error: BaseException | None) -> None:
        if error is None:
            acc.success_count += 1
            acc.last_error = None
            if acc.status == AccountStatus.COOLING:
                acc.status = AccountStatus.ACTIVE
            return
        acc.fail_count += 1
        acc.last_error = str(error)[:500]
        if isinstance(error, UpstreamAuthError):
            acc.status = AccountStatus.INVALID
            log.warning("account %s marked invalid: %s", acc.id, error)
        elif isinstance(error, UpstreamQuotaError):
            acc.status = AccountStatus.COOLING
            acc.cooldown_until = time.time() + max(self.cooldown, 3600)
            log.warning("account %s out of quota, cooling down", acc.id)
        elif isinstance(error, UpstreamTimeout):
            # muse.ai being slow says nothing about the account; cooling it down
            # would only fail the other requests running on it in parallel.
            pass
        elif getattr(error, "retryable", False):
            acc.status = AccountStatus.COOLING
            acc.cooldown_until = time.time() + self.cooldown

    @_asynccontextmanager
    async def lease(
        self, *, exclude: Iterable[str] = (), hint: str | None = None, kind: str | None = None,
    ) -> AsyncIterator[Lease]:
        acc = await self.acquire(exclude=exclude, hint=hint, kind=kind)
        lease = Lease(acc)
        try:
            yield lease
        except (asyncio.CancelledError, GeneratorExit):
            # Client went away; not the account's fault.
            raise
        except BaseException as exc:
            lease.error = lease.error or exc
            raise
        finally:
            await self.release(acc, lease.error)
