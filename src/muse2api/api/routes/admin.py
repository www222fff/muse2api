"""Admin API: account pool management, status, tasks, client keys and the request log.

The ``/dashboard`` page is built on these endpoints; a browser extension for
one-click cookie import is planned on top of them too (see docs/ARCHITECTURE.md).
"""

from __future__ import annotations

import time
from typing import Literal

from fastapi import APIRouter, Depends, Query

from ... import __version__
from ...accounts.keepalive import renew_account
from ...accounts.model import Account, AccountStatus
from ...auth.keys import ApiKey
from ...errors import InvalidRequest, NotFound
from ...services.container import Services
from ...upstream.muse import missing_session_cookies
from ..deps import get_services, require_admin_key
from ..schemas import AccountCreate, AccountUpdate, KeyCreate, KeyUpdate

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin_key)])


def _get(svc: Services, account_id: str) -> Account:
    acc = svc.pool.get(account_id)
    if acc is None:
        raise NotFound(f"account '{account_id}' not found")
    return acc


@router.get("/status")
async def status(svc: Services = Depends(get_services)) -> dict:
    return {
        "version": __version__,
        "driver": await svc.driver.health(),
        "capabilities": svc.driver.capabilities.__dict__,
        "accounts": svc.pool.stats(),
    }


@router.get("/accounts")
async def list_accounts(svc: Services = Depends(get_services)) -> dict:
    return {"data": [a.public() for a in svc.pool.all()]}


@router.post("/accounts")
async def create_account(body: AccountCreate, svc: Services = Depends(get_services)) -> dict:
    cookies = {k: v for k, v in body.cookies.items() if v}
    if not cookies:
        raise InvalidRequest("cookies must not be empty")
    acc = Account(label=body.label, cookies=cookies, cookie_expires=body.cookie_expires,
                  enabled=body.enabled)
    await svc.pool.upsert(acc)
    return {"account": acc.public(), "missing_cookies": missing_session_cookies(cookies)}


@router.patch("/accounts/{account_id}")
async def update_account(account_id: str, body: AccountUpdate,
                         svc: Services = Depends(get_services)) -> dict:
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
async def delete_account(account_id: str, svc: Services = Depends(get_services)) -> dict:
    if not await svc.pool.remove(account_id):
        raise NotFound(f"account '{account_id}' not found")
    return {"deleted": account_id}


@router.post("/accounts/{account_id}/reset")
async def reset_account(account_id: str, svc: Services = Depends(get_services)) -> dict:
    acc = _get(svc, account_id)
    acc.status, acc.cooldown_until, acc.last_error = AccountStatus.ACTIVE, 0.0, None
    await svc.pool.upsert(acc)
    return {"account": acc.public()}


@router.post("/accounts/{account_id}/renew")
async def renew(account_id: str, svc: Services = Depends(get_services)) -> dict:
    _get(svc, account_id)
    return await renew_account(svc.pool, svc.driver, account_id)


@router.get("/tasks")
async def list_tasks(limit: int = 100, svc: Services = Depends(get_services)) -> dict:
    return {"data": [t.model_dump(mode="json") for t in svc.tasks.list(limit)]}


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, svc: Services = Depends(get_services)) -> dict:
    return {"cancelled": await svc.tasks.cancel(task_id)}


# ---- client API keys ----
def _get_key(svc: Services, key_id: str) -> ApiKey:
    key = svc.keys.get(key_id)
    if key is None:
        raise NotFound(f"key '{key_id}' not found")
    return key


# Identities that authenticate without a stored key (see deps.require_api_key).
BUILTIN_KEYS = ("legacy", "admin")
_NO_USAGE = {"total": 0, "requests_24h": 0, "errors_24h": 0, "last_request_at": None}


def _usage(row: dict | None) -> dict:
    if row is None:
        return dict(_NO_USAGE)
    return {k: row[k] for k in _NO_USAGE}


@router.get("/keys")
async def list_keys(svc: Services = Depends(get_services)) -> dict:
    """Stored keys plus the built-in identities, each with request counts from the log."""
    usage = {r["key_id"]: r for r in (await svc.requests.key_usage())["data"]}
    return {
        "data": [{**k.public(), "usage": _usage(usage.get(k.id))} for k in svc.keys.all()],
        "builtin": [{"id": b, "name": b, "usage": _usage(usage.get(b))} for b in BUILTIN_KEYS],
    }


@router.get("/keys/usage")
async def key_usage(svc: Services = Depends(get_services)) -> dict:
    """Raw per-``key_id`` counts, including deleted keys still present in the log."""
    return await svc.requests.key_usage()


@router.post("/keys")
async def create_key(body: KeyCreate, svc: Services = Depends(get_services)) -> dict:
    key, plaintext = await svc.keys.create(body.name.strip(), body.note)
    # The only time the plaintext key is ever returned.
    return {"key": key.public(), "api_key": plaintext}


@router.patch("/keys/{key_id}")
async def update_key(key_id: str, body: KeyUpdate, svc: Services = Depends(get_services)) -> dict:
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
async def delete_key(key_id: str, svc: Services = Depends(get_services)) -> dict:
    if not await svc.keys.remove(key_id):
        raise NotFound(f"key '{key_id}' not found")
    return {"deleted": key_id}


# ---- request log ----
@router.get("/requests")
async def list_requests(
    key_id: str | None = None,
    account_id: str | None = None,
    status: Literal["2xx", "3xx", "4xx", "5xx", "failed"] | None = None,
    path: str | None = None,
    since: float | None = Query(default=None, description="Unix seconds"),
    hide_polls: bool = False,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    svc: Services = Depends(get_services),
) -> dict:
    return await svc.requests.query(key_id=key_id, account_id=account_id, status=status,
                                    path=path, since=since, hide_polls=hide_polls,
                                    limit=limit, offset=offset)


@router.get("/stats")
async def stats(window: Literal["1h", "24h", "7d"] = "24h",
                svc: Services = Depends(get_services)) -> dict:
    return {"now": time.time(), **await svc.requests.stats(window)}
