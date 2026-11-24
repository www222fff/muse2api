"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import types
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from functools import wraps
from typing import Any, TypeVar, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import __version__
from .accounts import AccountStatus
from .accounts.keepalive import keepalive_loop
from .api.middleware import RequestLogMiddleware
from .api.routes import build_router
from .config import Settings, get_settings
from .drivers.base import MuseDriver
from .errors import Muse2APIError
from .services.container import Services

log = logging.getLogger("muse2api")

# How often pending key "last used" times are written and old request rows pruned.
_FLUSH_INTERVAL = 60.0
_PRUNE_INTERVAL = 3600.0


async def _housekeeping(services: Services) -> None:
    last_prune = time.monotonic()
    while True:
        await asyncio.sleep(_FLUSH_INTERVAL)
        await services.keys.flush()
        if time.monotonic() - last_prune >= _PRUNE_INTERVAL:
            last_prune = time.monotonic()
            await services.requests.prune(services.settings.request_log_retention_days)
            removed = services.media.prune(services.settings.media_retention_days * 86400)
            if removed:
                log.info("media cleanup: removed %d expired file(s)", removed)


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


def create_app(settings: Settings | None = None, driver: MuseDriver | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.resolve_api_key()
    services = Services.build(settings, driver)

    @_asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await services.pool.load()
        await services.tasks.load()
        await services.keys.load()
        await services.requests.open()
        await services.requests.prune(settings.request_log_retention_days)
        await services.requests.backfill_tasks(services.tasks.list(limit=services.tasks.max_kept))
        await services.driver.startup()
        for acc in services.pool.all():
            if acc.enabled and acc.status != AccountStatus.INVALID:
                services.driver.prewarm(acc)
        housekeeping = asyncio.create_task(_housekeeping(services))
        keepalive = None
        if settings.keepalive_enabled and services.driver.capabilities.renew_session:
            keepalive = asyncio.create_task(
                keepalive_loop(services.pool, services.driver, settings.keepalive_interval),
            )
        log.info("muse2api %s ready (driver=%s)", __version__, services.driver.name)
        try:
            yield
        finally:
            for bg in (keepalive, housekeeping):
                if bg:
                    bg.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await bg
            await services.tasks.shutdown()
            await services.driver.shutdown()
            await services.keys.flush()
            await services.requests.close()

    app = FastAPI(title="muse2api", version=__version__, lifespan=lifespan)
    app.state.services = services

    @app.exception_handler(Muse2APIError)
    async def _muse_error(_: Request, exc: Muse2APIError) -> JSONResponse:
        return JSONResponse(exc.to_body(), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        msg = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        return JSONResponse(
            {"error": {"message": msg, "type": "invalid_request_error", "code": "invalid_request"}},
            status_code=400,
        )

    app.include_router(build_router())
    app.add_middleware(RequestLogMiddleware, request_log=services.requests)
    # The cookie-import extension calls /admin/accounts from chrome-extension://.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"chrome-extension://.*",
        allow_methods=["*"],
        allow_headers=["*"],
    )
    return app
