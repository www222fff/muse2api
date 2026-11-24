"""Session keepalive cadence and side effects.

muse2api A also skips accounts renewed recently ("only when due"); this keepalive
has no such rule (it renews every usable account each interval), so only the
shared intent is tested: which accounts it renews, that maintenance never counts
as use, and that a renewal's wake outcome reaches the pool's routing."""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path

from muse2api.accounts import Account, AccountPool, AccountStatus, JsonAccountStore
from muse2api.accounts.keepalive import keepalive_loop, renew_account
from muse2api.drivers.base import SessionInfo
from muse2api.drivers.mock import MockDriver
from muse2api.errors import FeatureNotImplemented


class RecordingDriver(MockDriver):
    def __init__(self, meta: dict | None = None) -> None:
        super().__init__(delay=0)
        self.calls: list[str] = []
        self.meta = meta or {"session_status": "assigned"}
        self.two_rounds = asyncio.Event()

    async def renew_session(self, account: Account) -> SessionInfo:
        self.calls.append(account.id)
        if len(self.calls) >= 2:
            self.two_rounds.set()
        return SessionInfo(ok=True, cookies={"hatch_sess": "rotated"}, meta=dict(self.meta))


async def _pool(tmp_path: Path):
    pool = AccountPool(JsonAccountStore(tmp_path / "acc.json"))
    await pool.upsert(Account(id="on", cookies={"hatch_sess": "x"}, last_used_at=1234.0,
                              success_count=7))
    await pool.upsert(Account(id="off", cookies={"hatch_sess": "x"}, enabled=False))
    await pool.upsert(Account(id="bad", cookies={"hatch_sess": "x"},
                              status=AccountStatus.INVALID))
    return pool


async def test_loop_renews_only_usable_accounts_each_interval(tmp_path: Path):
    pool, driver = await _pool(tmp_path), RecordingDriver()
    task = asyncio.create_task(keepalive_loop(pool, driver, 0.01))
    try:
        await asyncio.wait_for(driver.two_rounds.wait(), 2)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert driver.calls[:2] == ["on", "on"]
    assert set(driver.calls) == {"on"}


async def test_loop_waits_one_interval_before_the_first_round(tmp_path: Path):
    pool, driver = await _pool(tmp_path), RecordingDriver()
    task = asyncio.create_task(keepalive_loop(pool, driver, 60))
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert driver.calls == []


async def test_maintenance_does_not_count_as_use(tmp_path: Path):
    pool, driver = await _pool(tmp_path), RecordingDriver()
    result = await renew_account(pool, driver, "on")
    acc = pool.get("on")
    assert result["ok"] and acc.cookies["hatch_sess"] == "rotated"  # type: ignore[attr-defined]
    assert acc.last_used_at == 1234.0 and acc.success_count == 7  # type: ignore[attr-defined]
    assert acc.meta["renewed_at"] > time.time() - 5  # type: ignore[attr-defined]
    assert pool.stats()["inflight"] == 0


async def test_unconfirmed_wake_from_keepalive_blocks_video(tmp_path: Path):
    pool = await _pool(tmp_path)
    driver = RecordingDriver({"session_status": "assigned", "vm_wake_ok": False,
                              "vm_state_after": "SUSPENDED"})
    await renew_account(pool, driver, "on")
    acc = pool.get("on")
    assert acc.meta["vm_wake_ok"] is False and acc.meta["vm_state_after"] == "SUSPENDED"  # type: ignore[attr-defined]
    assert acc.meta["kind_blocked_until"]["video"] > time.time()  # type: ignore[attr-defined]

    driver.meta = {"session_status": "assigned", "vm_wake_ok": True, "vm_state_after": "RUNNING"}
    await renew_account(pool, driver, "on")
    assert "video" not in acc.meta["kind_blocked_until"]  # type: ignore[attr-defined]


async def test_driver_without_renewal_is_skipped(tmp_path: Path):
    class NoRenew(MockDriver):
        async def renew_session(self, account):
            raise FeatureNotImplemented("no renewal")

    pool = await _pool(tmp_path)
    result = await renew_account(pool, NoRenew(), "on")
    assert result == {"ok": False, "error": "no renewal"}
    assert "renewed_at" not in pool.get("on").meta  # type: ignore[attr-defined]
