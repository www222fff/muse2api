"""Quota: parsing the Settings -> Usage text (en / zh-CN / zh-TW), the browser
driver's quota read (idle tab only, per-account lock, cache, UI always closed) and
the admin routes around it."""

from __future__ import annotations

import asyncio
import io
import itertools
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from muse2api.accounts import Account
from muse2api.config import Settings
from muse2api.drivers.browser import dom
from muse2api.drivers.browser.cdp import CDPError
from muse2api.drivers.browser.driver import AccountBusy, BrowserDriver, Tab
from muse2api.errors import UpstreamError

parse = dom.parse_usage_text

EN = ("Settings\nGeneral\nUsage\nFree plan\nWeekly limit resets on Sep 30\n"
      "1% used\nAdditional tokens\nNever expires\n0% used (2B tokens left)\nLanguage")
ZH_TW = ("設定\n一般\nMeta 帳戶\n用量\n免費計劃\n每星期上限會於 10月30日重設\n已用 7%\n"
         "額外的符元\n永不過期\n已使用 0%（剩餘 20 億個憑證）\n升級\n語言\n外觀")
ZH_CN = ("设置\n用量\n免费计划\n每周上限将于 10月22日重置\n已使用 12.5%\n"
         "额外令牌\n永不过期\n已使用 0%（剩余 20 亿令牌）\n语言")


# ---------------------------------------------------------------- parser
@pytest.mark.parametrize("text, want", [
    (EN, {"plan": "Free plan", "weekly_used_pct": 1.0, "resets_at": "Sep 30",
          "extra_left": "2B tokens left"}),
    (ZH_TW, {"plan": "免費計劃", "weekly_used_pct": 7.0, "resets_at": "10月30日",
             "extra_left": "剩餘 20 億個憑證"}),
    (ZH_CN, {"plan": "免费计划", "weekly_used_pct": 12.5, "resets_at": "10月22日",
             "extra_left": "剩余 20 亿令牌"}),
])
def test_localized_usage(text, want):
    assert parse(text) == {**want, "found": True}


@pytest.mark.parametrize("text", ["0% used", "已用 0%", "已使用 0%", "已用 ０％"])
def test_zero_percent_is_a_reading(text):
    assert parse(text)["found"] and parse(text)["weekly_used_pct"] == 0


@pytest.mark.parametrize("text", ["12.75% used", "已用 12.75%", "已使用 １２．７５％"])
def test_decimal_and_fullwidth_percentages(text):
    assert parse(text)["weekly_used_pct"] == 12.75


@pytest.mark.parametrize("plan", ["Free plan", "免費計劃", "免费计划", "免費方案"])
def test_plan_alone_is_not_found(plan):
    result = parse(plan)
    assert result["found"] is False and result["plan"] == plan
    assert result["weekly_used_pct"] is None


@pytest.mark.parametrize("text", [
    "Usage\nFree plan\nAdditional tokens\nNever expires\n0% used (2B tokens left)",
    "用量\n免費計劃\n額外的符元\n永不過期\n已使用 0%（剩餘 20 億個憑證）",
    "Usage\nExtra tokens\n7.5% used (10 tokens left)",
])
def test_extra_tokens_are_not_weekly_usage(text):
    result = parse(text)
    assert result["found"] is False and result["weekly_used_pct"] is None
    assert result["extra_left"]


@pytest.mark.parametrize("text", [
    "", None, "Settings\nGeneral", "Upgrade\n100% discount",
    # A redesigned panel with labels we do not know: say so rather than guess.
    "Nutzung\nKostenloser Tarif\n7 % verbraucht",
])
def test_unmatched_text_is_not_found(text):
    assert parse(text) == {"plan": None, "weekly_used_pct": None, "resets_at": None,
                           "extra_left": None, "found": False}


def test_text_outside_the_usage_section_is_ignored():
    result = parse("Profile\n99% used\nUsage\nFree plan\n0% used\nLanguage\n88% used")
    assert result["weekly_used_pct"] == 0


def test_percentage_bounds():
    assert not parse("101% used")["found"]
    assert parse("100% used")["weekly_used_pct"] == 100


# ---------------------------------------------------------------- driver
class FakePage:
    """Answers the quota scripts like muse.ai's page would."""

    def __init__(self, dialog: str = EN, *, entry=True, menu=True, closes=True) -> None:
        self.dialog, self.entry, self.menu, self.closes = dialog, entry, menu, closes
        self.open = False
        self.closed = False
        self.sent: list[tuple[str, dict]] = []
        self.fail_on: str | None = None

    async def evaluate(self, expr, **_):
        if expr is dom.PAGE_STATE:
            return {"hasInput": True, "ready": True}
        if expr is dom.QUOTA_SETTINGS_ENTRY:
            return {"x": 10, "y": 20} if self.entry else None
        if expr is dom.QUOTA_MENU_ITEM:
            if self.fail_on == "menu":
                raise CDPError("target crashed")
            if self.menu:
                self.open = True
                return {"x": 30, "y": 40}
            return None
        if expr is dom.QUOTA_DIALOG_TEXT:
            return self.dialog if self.open else ""
        raise AssertionError(f"unexpected script {expr[:60]}")

    async def send(self, method, params=None, **_):
        self.sent.append((method, params or {}))
        if method == "Input.dispatchKeyEvent" and params.get("key") == "Escape" and self.closes:  # type: ignore[attr-defined]
            self.open = False
        return {}

    async def close(self) -> None:
        self.closed = True

    def escapes(self) -> int:
        return sum(1 for m, p in self.sent
                   if m == "Input.dispatchKeyEvent" and p.get("type") == "keyDown")


ACC = Account(id="q0", cookies={"hatch_sess": "x"})


@pytest.fixture
def driver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    drv = BrowserDriver(Settings(_env_file=None, data_dir=tmp_path, account_max_concurrency=2))  # type: ignore[call-arg]
    ids = itertools.count()
    drv.pages = []  # type: ignore[attr-defined]

    async def fake_open(account: Account) -> Tab:
        page = FakePage()
        drv.pages.append(page)  # type: ignore[attr-defined]
        return Tab(account.id, "ctx", f"t{next(ids)}", page)  # type: ignore[arg-type]

    monkeypatch.setattr(drv, "_open_tab", fake_open)
    monkeypatch.setattr(BrowserDriver, "_QUOTA_CLICK_TIMEOUT", 0.05)
    monkeypatch.setattr(BrowserDriver, "_QUOTA_READ_TIMEOUT", 0.05)
    monkeypatch.setattr(BrowserDriver, "_ESCAPE_PAUSE", 0)
    return drv


async def test_quota_reads_and_closes_the_dialog(driver: Any):
    q = await driver.quota(ACC)
    assert {k: q[k] for k in ("plan", "weekly_used_pct", "resets_at", "extra_left", "found",
                              "source")} == {
        "plan": "Free plan", "weekly_used_pct": 1.0, "resets_at": "Sep 30",
        "extra_left": "2B tokens left", "found": True, "source": "browser"}
    assert q["fetched_at"] > 0
    page = driver.pages[0]
    assert not page.open and page.escapes() >= 2  # before and after
    tab = driver._tabs[ACC.id][0]
    assert not tab.busy and not tab.stale


async def test_quota_is_cached_until_refreshed(driver: Any):
    first = await driver.quota(ACC)
    page = driver.pages[0]
    page.dialog = EN.replace("1% used", "40% used")
    assert (await driver.quota(ACC)) == first
    driver.forget_quota(ACC.id)
    assert (await driver.quota(ACC))["weekly_used_pct"] == 40


async def test_cache_window_from_env(driver: Any, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MUSE2API_QUOTA_CACHE_S", "0")
    await driver.quota(ACC)
    driver.pages[0].dialog = EN.replace("1% used", "3% used")
    assert (await driver.quota(ACC))["weekly_used_pct"] == 3


async def test_concurrent_reads_scrape_once(driver: Any):
    results = await asyncio.gather(*(driver.quota(ACC) for _ in range(4)))
    assert len({r["fetched_at"] for r in results}) == 1
    assert len(driver.pages) == 1


async def test_busy_tab_is_never_taken(driver: Any):
    tab = await driver._checkout(ACC)  # a request is running on the only tab
    with pytest.raises(AccountBusy) as err:
        await driver.quota(ACC)
    assert err.value.status_code == 409
    assert tab.busy and driver.pages[0].sent == []
    assert len(driver._tabs[ACC.id]) == 1  # and no extra tab was opened next to it


async def test_an_idle_tab_beside_a_busy_one_is_used(driver: Any):
    busy = await driver._checkout(ACC)
    idle = await driver._checkout(ACC)
    await driver._checkin(idle)
    assert (await driver.quota(ACC))["found"]
    assert busy.busy and getattr(busy.session, "sent") == [] and getattr(idle.session, "sent")


async def test_unknown_labels_give_found_false(driver: Any):
    await driver.quota(ACC)
    driver.forget_quota(ACC.id)
    driver.pages[0].dialog = "Nutzung\nKostenloser Tarif\n7 % verbraucht"
    q = await driver.quota(ACC)
    assert q["found"] is False and q["weekly_used_pct"] is None and not driver.pages[0].open


async def test_missing_settings_entry_gives_found_false(driver: Any):
    await driver.quota(ACC)
    driver.forget_quota(ACC.id)
    driver.pages[0].entry = False
    q = await driver.quota(ACC)
    assert q["found"] is False and "settings entry" in q["error"]


async def test_dialog_that_will_not_close_retires_the_tab(driver: Any):
    await driver.quota(ACC)
    driver.forget_quota(ACC.id)
    page = driver.pages[0]
    page.closes = False
    await driver.quota(ACC)
    assert page.closed and driver._tabs[ACC.id] == []


async def test_browser_error_closes_the_tab_and_is_not_cached(driver: Any):
    await driver.quota(ACC)
    driver.forget_quota(ACC.id)
    driver.pages[0].fail_on = "menu"
    with pytest.raises(UpstreamError):
        await driver.quota(ACC)
    assert driver.pages[0].closed
    assert (await driver.quota(ACC))["found"]  # a new tab, not a cached failure


async def test_capability_flag():
    assert BrowserDriver.capabilities.quota


# ---------------------------------------------------------------- admin routes
def _account(client: TestClient, admin: dict[str, str]):
    r = client.post("/admin/accounts", headers=admin,
                    json={"cookies": {"hatch_sess": "secretvalue123"}})
    return r.json()["account"]["id"]


class CountingQuota:
    def __init__(self) -> None:
        self.calls, self.forgotten = 0, []

    async def quota(self, account):
        self.calls += 1
        return {"plan": "Free plan", "weekly_used_pct": float(self.calls), "resets_at": "Sep 30",
                "extra_left": None, "found": True, "source": "browser", "fetched_at": 1.0}

    def forget_quota(self, account_id):
        self.forgotten.append(account_id)


def test_refresh_param_bypasses_the_driver_cache(client: TestClient, admin: dict[str, str], monkeypatch: pytest.MonkeyPatch):
    acc_id = _account(client, admin)
    fake = CountingQuota()
    driver = getattr(client.app, "state").services.driver
    monkeypatch.setattr(driver, "quota", fake.quota)
    monkeypatch.setattr(driver, "forget_quota", fake.forget_quota, raising=False)
    client.get(f"/admin/accounts/{acc_id}/quota", headers=admin)
    assert fake.forgotten == []
    body = client.get(f"/admin/accounts/{acc_id}/quota?refresh=1", headers=admin).json()
    assert fake.forgotten == [acc_id] and body["quota"]["weekly_used_pct"] == 2
    listed = client.get("/admin/accounts", headers=admin).json()["data"][0]["meta"]["quota"]
    assert listed["found"] and listed["source"] == "browser" and listed["weekly_used_pct"] == 2


def test_quota_route_maps_a_busy_account_to_409(client: TestClient, admin: dict[str, str], monkeypatch: pytest.MonkeyPatch):
    acc_id = _account(client, admin)

    async def busy(account):
        raise AccountBusy("busy")

    monkeypatch.setattr(getattr(client.app, "state").services.driver, "quota", busy)
    r = client.get(f"/admin/accounts/{acc_id}/quota", headers=admin)
    assert r.status_code == 409 and r.json()["error"]["code"] == "account_busy"


def test_refresh_without_a_cache_still_works(client: TestClient, admin: dict[str, str]):
    # The mock driver has no cache and no forget_quota.
    r = client.get(f"/admin/accounts/{_account(client, admin)}/quota?refresh=1", headers=admin)
    assert r.status_code == 200


def test_extension_zip_missing_is_404(client: TestClient, admin: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from muse2api.api.routes import admin as admin_routes

    monkeypatch.setattr(admin_routes, "EXTENSION_DIR", tmp_path / "nope")
    assert client.get("/admin/extension.zip", headers=admin).status_code == 404


def test_extension_zip(client: TestClient, admin: dict[str, str], auth: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from muse2api.api.routes import admin as admin_routes

    ext = tmp_path / "extension"
    (ext / "icons").mkdir(parents=True)
    (ext / "manifest.json").write_text('{"manifest_version": 3}')
    (ext / "icons" / "16.png").write_bytes(b"\x89PNG")
    (ext / ".DS_Store").write_bytes(b"junk")
    monkeypatch.setattr(admin_routes, "EXTENSION_DIR", ext)
    assert client.get("/admin/extension.zip", headers=auth).status_code == 401
    r = client.get("/admin/extension.zip", headers=admin)
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert sorted(names) == ["muse2api-extension/icons/16.png",
                             "muse2api-extension/manifest.json"]
