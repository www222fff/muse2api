"""Admin API: account pool management, status, tasks, client keys and the request log.

The ``/dashboard`` page is built on these endpoints, and so is the browser
extension for one-click cookie import (served by ``GET /admin/extension.zip``).
"""

from __future__ import annotations

import io
import time
import zipfile
from pathlib import Path
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ... import __version__
from ...accounts.keepalive import renew_account
from ...accounts.model import Account, AccountStatus
from ...auth.keys import ApiKey
from ...errors import InvalidRequest, Muse2APIError, NotFound
from ...services.container import Services
from ...services.request_log import RequestQuery
from ...upstream.muse import missing_session_cookies
from ..deps import get_services, require_admin_key
from ..schemas import AccountCreate, AccountsImportRequest, AccountUpdate, KeyCreate, KeyUpdate

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin_key)])

EXTENSION_DIR = Path(__file__).resolve().parents[2] / "web" / "extension"
"""Unpacked Chrome extension that imports muse.ai cookies into this gateway."""


def _parse_cookie_header(value: str) -> dict[str, str]:
    """Parse a raw ``Cookie`` header string into a name -> value dict."""
    cookies: dict[str, str] = {}
    for raw_part in value.split(";"):
        part = raw_part.strip()
        if not part or "=" not in part:
            continue
        name, _, val = part.partition("=")
        name = name.strip()
        if name:
            cookies[name] = val.strip()
    return cookies


def _parse_cookie_list(value: list[Any]) -> tuple[dict[str, str], dict[str, int]]:
    """Parse a cookie-export list into ``(cookies, cookie_expires)``."""
    cookies: dict[str, str] = {}
    expires: dict[str, int] = {}
    for i, entry in enumerate(value):
        if not isinstance(entry, dict):
            message = f"cookies[{i}] must be an object with 'name' and 'value'"
            raise InvalidRequest(message)
        e = cast(dict[str, Any], entry)
        if not e.get("name"):
            message = f"cookies[{i}] must be an object with 'name' and 'value'"
            raise InvalidRequest(message)
        name = str(e["name"])
        cookies[name] = str(e.get("value", ""))
        exp = e.get("expirationDate")
        if exp is not None:
            try:
                expires[name] = int(float(exp))
            except (TypeError, ValueError):
                message = f"cookies[{i}].expirationDate is not a number"
                raise InvalidRequest(message) from None
    return cookies, expires


def parse_cookies(value: Any) -> tuple[dict[str, str], dict[str, int]]:
    """Normalise the three cookie import formats into ``(cookies, cookie_expires)``.

    ``cookie_expires`` maps cookie name -> unix expiry seconds; only cookie-export
    lists carry expiry info, the other formats return an empty dict.
    """
    if isinstance(value, dict):
        return dict(cast(dict[str, Any], value)), {}
    if isinstance(value, str):
        return _parse_cookie_header(value), {}
    if isinstance(value, list):
        return _parse_cookie_list(cast(list[Any], value))
    message = "cookies must be a dict, a raw 'Cookie' header string, or a cookie-export list"
    raise InvalidRequest(message)


def _build_account(item: AccountCreate) -> Account:
    """Build an Account from a create payload, normalising the cookie format first."""
    cookies, expires = parse_cookies(item.cookies)
    cookies = {k: v for k, v in cookies.items() if v}
    expires = {k: v for k, v in expires.items() if k in cookies}
    if not cookies:
        message = "cookies must not be empty"
        raise InvalidRequest(message)
    return Account(label=item.label, cookies=cookies,
                   cookie_expires={**expires, **item.cookie_expires}, enabled=item.enabled)


def _get(svc: Services, account_id: str) -> Account:
    acc = svc.pool.get(account_id)
    if acc is None:
        message = f"account '{account_id}' not found"
        raise NotFound(message)
    return acc


@router.get("/status")
async def status(svc: Annotated[Services, Depends(get_services)]) -> dict[str, Any]:
    return {
        "version": __version__,
        "driver": await svc.driver.health(),
        "capabilities": svc.driver.capabilities.__dict__,
        "accounts": svc.pool.stats(),
    }


@router.get("/accounts")
async def list_accounts(svc: Annotated[Services, Depends(get_services)]) -> dict[str, Any]:
    return {"data": [a.public() for a in svc.pool.all()]}


@router.post("/accounts")
async def create_account(body: AccountCreate, svc: Annotated[Services, Depends(get_services)]) -> dict[str, Any]:
    acc = _build_account(body)
    await svc.pool.upsert(acc)
    return {"account": acc.public(), "missing_cookies": missing_session_cookies(acc.cookies)}


@router.post("/accounts/import")
async def import_accounts(body: AccountsImportRequest,
                          svc: Annotated[Services, Depends(get_services)]) -> dict[str, Any]:
    """Import several accounts in one request; a failing entry does not abort the batch."""
    results: list[dict[str, Any]] = []
    for i, item in enumerate(body.accounts):
        try:
            acc = _build_account(item)
        except Muse2APIError as e:
            results.append({"index": i, "ok": False, "error": e.message, "code": e.code})
            continue
        await svc.pool.upsert(acc)
        results.append({"index": i, "ok": True, "account": acc.public(),
                        "missing_cookies": missing_session_cookies(acc.cookies)})
    return {"data": results}


@router.patch("/accounts/{account_id}")
async def update_account(account_id: str, body: AccountUpdate,
                         svc: Annotated[Services, Depends(get_services)]) -> dict[str, Any]:
    acc = _get(svc, account_id)
    if body.label is not None:
        acc.label = body.label
    if body.enabled is not None:
        acc.enabled = body.enabled
    if body.cookies is not None:
        acc.cookies = {k: v for k, v in body.cookies.items() if v}
        # Fresh cookies give an invalid account another chance.
        acc.status = AccountStatus.ACTIVE
        acc.last_error = None
    if body.cookie_expires is not None:
        acc.cookie_expires = body.cookie_expires
    await svc.pool.upsert(acc)
    return {"account": acc.public()}


@router.delete("/accounts/{account_id}")
async def delete_account(account_id: str, svc: Annotated[Services, Depends(get_services)]) -> dict[str, Any]:
    if not await svc.pool.remove(account_id):
        message = f"account '{account_id}' not found"
        raise NotFound(message)
    return {"deleted": account_id}


@router.post("/accounts/{account_id}/reset")
async def reset_account(account_id: str, svc: Annotated[Services, Depends(get_services)]) -> dict[str, Any]:
    acc = _get(svc, account_id)
    acc.status, acc.cooldown_until, acc.last_error = AccountStatus.ACTIVE, 0.0, None
    acc.meta.pop("kind_blocked_until", None)  # e.g. video after an unconfirmed VM wake
    acc.meta.pop("wake_blocked_until", None)
    await svc.pool.upsert(acc)
    return {"account": acc.public()}


@router.post("/accounts/{account_id}/renew")
async def renew(account_id: str, svc: Annotated[Services, Depends(get_services)]) -> dict[str, Any]:
    _get(svc, account_id)
    return await renew_account(svc.pool, svc.driver, account_id)


@router.get("/accounts/{account_id}/quota")
async def account_quota(account_id: str,
                        svc: Annotated[Services, Depends(get_services)],
                        *, refresh: bool = False) -> dict[str, Any]:
    """Ask the active driver for the account's quota snapshot and persist it in ``meta``.

    The browser driver caches each account's snapshot for 30 minutes
    (``MUSE2API_QUOTA_CACHE_S``); ``?refresh=1`` reads the page again. It answers
    409 while every tab of the account is serving a request. Drivers that do not
    implement ``MuseDriver.quota`` raise ``FeatureNotImplemented`` (HTTP 501).
    """
    acc = _get(svc, account_id)
    if refresh and (forget := getattr(svc.driver, "forget_quota", None)):
        forget(acc.id)
    snapshot = await svc.driver.quota(acc)
    acc.meta["quota"] = {"checked_at": time.time(), **snapshot}
    await svc.pool.upsert(acc)
    return {"account_id": acc.id, "quota": acc.meta["quota"]}


@router.get("/extension.zip")
async def extension_zip() -> Response:
    """The cookie-import browser extension, zipped in memory for "Load unpacked"."""
    root = EXTENSION_DIR
    # No symlinks (they could point outside the directory) and no dotfiles.
    files = sorted(f for f in root.rglob("*")
                   if f.is_file() and not f.is_symlink()
                   and not any(part.startswith(".") for part in f.relative_to(root).parts)
                   ) if root.is_dir() else []
    if not files:
        message = "the browser extension is not bundled with this server"
        raise NotFound(message)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, f"muse2api-extension/{f.relative_to(root).as_posix()}")
    return Response(buf.getvalue(), media_type="application/zip", headers={
        "Content-Disposition": 'attachment; filename="muse2api-extension.zip"'})


@router.get("/tasks")
async def list_tasks(svc: Annotated[Services, Depends(get_services)],
                     limit: int = 100) -> dict[str, Any]:
    return {"data": [t.model_dump(mode="json") for t in svc.tasks.list(limit)]}


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, svc: Annotated[Services, Depends(get_services)]) -> dict[str, Any]:
    return {"cancelled": await svc.tasks.cancel(task_id)}


# ---- client API keys ----
def _get_key(svc: Services, key_id: str) -> ApiKey:
    key = svc.keys.get(key_id)
    if key is None:
        message = f"key '{key_id}' not found"
        raise NotFound(message)
    return key


# Identities that authenticate without a stored key (see deps.require_api_key).
BUILTIN_KEYS = ("legacy", "admin")
_NO_USAGE = {"total": 0, "requests_24h": 0, "errors_24h": 0, "last_request_at": None}


def _usage(row: dict[str, Any] | None) -> dict[str, Any]:
    if row is None:
        return dict(_NO_USAGE)
    return {k: row[k] for k in _NO_USAGE}


@router.get("/keys")
async def list_keys(svc: Annotated[Services, Depends(get_services)]) -> dict[str, Any]:
    """Stored keys plus the built-in identities, each with request counts from the log."""
    usage = {r["key_id"]: r for r in (await svc.requests.key_usage())["data"]}
    return {
        "data": [{**k.public(), "usage": _usage(usage.get(k.id))} for k in svc.keys.all()],
        "builtin": [{"id": b, "name": b, "usage": _usage(usage.get(b))} for b in BUILTIN_KEYS],
    }


@router.get("/keys/usage")
async def key_usage(svc: Annotated[Services, Depends(get_services)]) -> dict[str, Any]:
    """Raw per-``key_id`` counts, including deleted keys still present in the log."""
    return await svc.requests.key_usage()


@router.post("/keys")
async def create_key(body: KeyCreate, svc: Annotated[Services, Depends(get_services)]) -> dict[str, Any]:
    key, plaintext = await svc.keys.create(body.name.strip(), body.note)
    # The only time the plaintext key is ever returned.
    return {"key": key.public(), "api_key": plaintext}


@router.patch("/keys/{key_id}")
async def update_key(key_id: str, body: KeyUpdate, svc: Annotated[Services, Depends(get_services)]) -> dict[str, Any]:
    key = _get_key(svc, key_id)
    if body.name is not None:
        key.name = body.name.strip()
    if body.note is not None:
        key.note = body.note
    if body.revoked is not None:
        key.revoked = body.revoked
    await svc.keys.save()
    return {"key": key.public()}


@router.delete("/keys/{key_id}")
async def delete_key(key_id: str, svc: Annotated[Services, Depends(get_services)]) -> dict[str, Any]:
    if not await svc.keys.remove(key_id):
        message = f"key '{key_id}' not found"
        raise NotFound(message)
    return {"deleted": key_id}


# ---- request log ----
class RequestFilter(BaseModel):
    """Query filter bundle for ``GET /admin/requests`` (one query param per field)."""
    key_id: str | None = None
    account_id: str | None = None
    status: Literal["2xx", "3xx", "4xx", "5xx", "failed"] | None = None
    path: str | None = None
    since: float | None = Field(default=None, description="Unix seconds")
    hide_polls: bool = False
    limit: int = Field(default=100, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


@router.get("/requests")
async def list_requests(
    svc: Annotated[Services, Depends(get_services)],
    filters: Annotated[RequestFilter, Depends()],
) -> dict[str, Any]:
    return await svc.requests.query(RequestQuery(
        key_id=filters.key_id, account_id=filters.account_id, status=filters.status,
        path=filters.path, since=filters.since, hide_polls=filters.hide_polls,
        limit=filters.limit, offset=filters.offset,
    ))


@router.get("/stats")
async def stats(svc: Annotated[Services, Depends(get_services)],
                window: Literal["1h", "24h", "7d"] = "24h") -> dict[str, Any]:
    return {"now": time.time(), **await svc.requests.stats(window)}
