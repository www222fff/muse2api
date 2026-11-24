"""Gateway: runs driver calls on pooled accounts with automatic failover."""

from __future__ import annotations

import logging
import types
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from functools import wraps
from typing import Any, TypeVar, cast

from ..accounts.model import Account
from ..accounts.pool import AccountPool, Lease
from ..drivers.base import ChatRequest, ImageRequest, MediaResult, MuseDriver, VideoRequest
from ..errors import UpstreamError
from .request_log import note_account

log = logging.getLogger(__name__)
T = TypeVar("T")

_ANONYMOUS = Account(id="anonymous", label="driver without accounts")


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


class Gateway:
    def __init__(self, pool: AccountPool, driver: MuseDriver, *, max_failover: int = 2) -> None:
        self.pool = pool
        self.driver = driver
        self.max_failover = max_failover

    @_asynccontextmanager
    async def _lease(self, exclude: list[str], hint: str | None,
                     kind: str | None = None) -> AsyncIterator[Lease]:
        if not self.driver.requires_account and not self.pool.all():
            note_account(_ANONYMOUS.id)
            yield Lease(_ANONYMOUS)
            return
        async with self.pool.lease(exclude=exclude, hint=hint, kind=kind) as lease:
            note_account(lease.account.id)
            yield lease

    def _should_retry(self, exc: UpstreamError, attempt: int, tried: list[str],
                      kind: str | None = None) -> bool:
        # With no other account to switch to, a retry would only replace the real
        # error with "no usable account in pool".
        return (exc.retryable and attempt < self.max_failover
                and self.pool.has_alternative(tried, kind))

    async def chat_stream(self, req: ChatRequest) -> AsyncIterator[str]:
        tried: list[str] = []
        for attempt in range(self.max_failover + 1):
            async with self._lease(tried, req.conversation_hint) as lease:
                emitted = False
                try:
                    async for delta in self.driver.chat_stream(lease.account, req):
                        emitted = True
                        yield delta
                except UpstreamError as exc:
                    lease.fail(exc)
                    tried.append(lease.account.id)
                    # Once text reached the client we cannot transparently switch accounts.
                    if emitted or not self._should_retry(exc, attempt, tried):
                        raise
                    log.warning("chat failed on %s (%s), failing over", lease.account.id, exc)
                    continue
                return

    async def chat_complete(self, req: ChatRequest) -> str:
        """Non-streaming chat: the driver's final reply text, with failover."""
        return await self._run(lambda acc: self.driver.chat_complete(acc, req),
                               hint=req.conversation_hint)

    async def _run(self, fn: Callable[[Account], Awaitable[T]], hint: str | None = None,
                   kind: str | None = None) -> T:
        tried: list[str] = []
        for attempt in range(self.max_failover + 1):
            async with self._lease(tried, hint, kind) as lease:
                try:
                    return await fn(lease.account)
                except UpstreamError as exc:
                    lease.fail(exc)
                    tried.append(lease.account.id)
                    if not self._should_retry(exc, attempt, tried, kind):
                        raise
                    log.warning("call failed on %s (%s), failing over", lease.account.id, exc)
        message = "unreachable"
        raise AssertionError(message)

    async def generate_image(self, req: ImageRequest) -> list[MediaResult]:
        return await self._run(lambda acc: self.driver.generate_image(acc, req))

    async def generate_video(self, req: VideoRequest) -> MediaResult:
        return await self._run(lambda acc: self.driver.generate_video(acc, req), kind="video")
