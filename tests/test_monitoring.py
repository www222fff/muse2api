from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from muse2api.auth.keys import hash_key
from muse2api.config import Settings


def _bearer(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _requests(client: TestClient, admin: dict[str, str], **params):
    r = client.get("/admin/requests", headers=admin, params=params)
    assert r.status_code == 200
    return r.json()["data"]


def _assert_created_key(plaintext: str, key: dict, stored_text: str) -> None:
    assert plaintext.startswith("m2a-")
    assert key["prefix"] == plaintext[:8]
    assert "hash" not in key
    assert plaintext not in stored_text
    assert json.loads(stored_text)[0]["hash"] == hash_key(plaintext)


def _assert_listed_key(listed: list[dict]) -> None:
    assert [k["name"] for k in listed] == ["alice"]
    assert "hash" not in listed[0]
    assert listed[0]["last_used_at"] > 0


def test_key_lifecycle(client: TestClient, admin: dict[str, str], settings: Settings):
    r = client.post("/admin/keys", headers=admin, json={"name": "alice", "note": "laptop"})
    assert r.status_code == 200
    body = r.json()
    plaintext, key = body["api_key"], body["key"]
    _assert_created_key(plaintext, key, settings.keys_file.read_text(encoding="utf-8"))

    assert client.get("/v1/models", headers=_bearer(plaintext)).status_code == 200
    _assert_listed_key(client.get("/admin/keys", headers=admin).json()["data"])

    revoked = client.patch(f"/admin/keys/{key['id']}", headers=admin, json={"revoked": True})
    assert (revoked.json()["key"]["revoked"], client.get("/v1/models", headers=_bearer(plaintext)).status_code) == (True, 401)

    renamed = client.patch(f"/admin/keys/{key['id']}", headers=admin,
                           json={"revoked": False, "name": "alice2"})
    assert (renamed.json()["key"]["name"], client.get("/v1/models", headers=_bearer(plaintext)).status_code) == ("alice2", 200)

    deleted = client.delete(f"/admin/keys/{key['id']}", headers=admin)
    again = client.delete(f"/admin/keys/{key['id']}", headers=admin)
    assert (deleted.status_code, again.status_code,
            client.get("/v1/models", headers=_bearer(plaintext)).status_code) == (200, 404, 401)


def test_keys_require_admin(client: TestClient, auth: dict[str, str]):
    assert client.get("/admin/keys", headers=auth).status_code == 401
    assert client.post("/admin/keys", headers=auth, json={"name": "x"}).status_code == 401


def test_legacy_and_admin_keys_still_work(client: TestClient, auth: dict[str, str], admin: dict[str, str]):
    assert client.get("/v1/models", headers=auth).status_code == 200
    assert client.get("/v1/models", headers=admin).status_code == 200
    assert client.get("/v1/models", headers=_bearer("m2a-nope")).status_code == 401
    names = [r["key_name"] for r in _requests(client, admin)]
    assert names == [None, "admin", "legacy"]


def _assert_request_identity(row: dict, key: dict) -> None:
    assert row["method"] == "POST"
    assert row["path"] == "/v1/chat/completions"
    assert row["model"] == "gpt-4o"
    assert row["key_id"] == key["key"]["id"]
    assert row["key_name"] == "bob"
    assert row["account_id"] == "anonymous"  # mock driver without accounts


def _assert_request_outcome(row: dict) -> None:
    assert row["status_code"] == 200
    assert row["latency_ms"] >= 0
    assert row["client_ip"] == "203.0.113.7"
    assert row["stream"] is False
    assert row["error"] is None


def test_request_logged(client: TestClient, admin: dict[str, str]):
    key = client.post("/admin/keys", headers=admin, json={"name": "bob"}).json()
    headers = {**_bearer(key["api_key"]), "cf-connecting-ip": "203.0.113.7"}
    r = client.post("/v1/chat/completions", headers=headers, json={
        "model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    row = _requests(client, admin)[0]
    _assert_request_identity(row, key)
    _assert_request_outcome(row)


def test_stream_and_error_logged(client: TestClient, auth: dict[str, str], admin: dict[str, str]):
    with client.stream("POST", "/v1/chat/completions", headers=auth, json={
        "messages": [{"role": "user", "content": "s"}], "stream": True}) as r:
        assert r.status_code == 200
        list(r.iter_lines())
    r = client.post("/v1/chat/completions", headers=auth, json={
        "model": "muse-video", "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 400

    bad, streamed = _requests(client, admin)[:2]
    assert streamed["stream"] is True and streamed["status_code"] == 200
    assert bad["status_code"] == 400
    assert bad["model"] == "muse-video"
    assert bad["error"]

    assert [x["id"] for x in _requests(client, admin, status="4xx")] == [bad["id"]]
    assert [x["id"] for x in _requests(client, admin, status="2xx")] == [streamed["id"]]


def test_polls_flagged_and_media_skipped(client: TestClient, auth: dict[str, str], admin: dict[str, str]):
    task = client.post("/v1/videos", headers=auth, json={"prompt": "a wave"}).json()
    assert client.get(f"/v1/videos/{task['id']}", headers=auth).status_code == 200
    client.get("/v1/media/does-not-exist.png")

    rows = _requests(client, admin)
    assert [r["path"] for r in rows] == [f"/v1/videos/{task['id']}", "/v1/videos"]
    poll, submit = rows
    assert poll["poll"] is True and poll["task_id"] == task["id"]
    assert submit["poll"] is False and submit["task_id"] == task["id"]
    assert submit["model"] == "muse-video"
    assert [r["path"] for r in _requests(client, admin, hide_polls="true")] == ["/v1/videos"]


def _assert_group_shapes(s: dict) -> None:
    for group in ("by_key", "by_account", "by_model", "by_status"):
        assert isinstance(s[group], list)


def test_stats_shape(client: TestClient, auth: dict[str, str], admin: dict[str, str]):
    client.get("/v1/models", headers=auth)
    client.get("/v1/models")  # 401
    r = client.get("/admin/stats", headers=admin, params={"window": "1h"})
    assert r.status_code == 200
    s = r.json()
    assert s["window"] == "1h"
    assert (s["total"], s["errors"], s["error_rate"]) == (2, 1, 0.5)
    assert set(s["latency_ms"]) == {"p50", "p95"}
    _assert_group_shapes(s)
    assert {g["status_code"] for g in s["by_status"]} == {200, 401}
    assert len(s["series"]) >= 59
    assert sum(p["count"] for p in s["series"]) == 2
    assert client.get("/admin/stats", headers=admin, params={"window": "2y"}).status_code == 400


def _assert_key_usage_summary(body: dict) -> None:
    by_name = {k["name"]: k for k in body["data"]}
    assert by_name["alice"]["usage"]["total"] == 3
    assert by_name["alice"]["usage"]["requests_24h"] == 3
    assert by_name["alice"]["usage"]["errors_24h"] == 1
    assert by_name["alice"]["usage"]["last_request_at"] > 0
    assert by_name["idle"]["usage"] == {"total": 0, "requests_24h": 0, "errors_24h": 0,
                                        "last_request_at": None}


def _assert_builtin_usage(body: dict) -> None:
    builtin = {b["id"]: b["usage"] for b in body["builtin"]}
    assert set(builtin) == {"legacy", "admin"}
    assert builtin["legacy"]["total"] == 1 and builtin["legacy"]["errors_24h"] == 0
    assert builtin["admin"]["total"] == 0  # /admin/* calls are not logged


def _assert_raw_usage_rows(raw: dict, alice_key_id: str) -> None:
    assert raw["since"] > 0
    rows = {row["key_id"]: row for row in raw["data"]}
    assert rows[alice_key_id]["key_name"] == "alice"
    assert rows[alice_key_id]["total"] == 3
    assert None not in rows  # unauthenticated requests have no key


def test_key_usage(client: TestClient, auth: dict[str, str], admin: dict[str, str]):
    alice = client.post("/admin/keys", headers=admin, json={"name": "alice"}).json()
    client.post("/admin/keys", headers=admin, json={"name": "idle"})
    a = _bearer(alice["api_key"])
    assert client.get("/v1/models", headers=a).status_code == 200
    assert client.post("/v1/chat/completions", headers=a, json={
        "model": "muse-video", "messages": [{"role": "user", "content": "x"}]}).status_code == 400
    task = client.post("/v1/videos", headers=a, json={"prompt": "a wave"}).json()
    client.get(f"/v1/videos/{task['id']}", headers=a)  # poll: not counted
    client.get("/v1/models", headers=auth)

    r = client.get("/admin/keys", headers=admin)
    assert r.status_code == 200
    body = r.json()
    _assert_key_usage_summary(body)
    _assert_builtin_usage(body)

    raw = client.get("/admin/keys/usage", headers=admin).json()
    _assert_raw_usage_rows(raw, alice["key"]["id"])
    assert client.get("/admin/keys/usage", headers=auth).status_code == 401


async def test_key_usage_windows(settings: Settings):
    import time

    from muse2api.services.request_log import RequestLog

    log = RequestLog(settings.requests_db)
    base = {"method": "GET", "path": "/v1/models", "latency_ms": 1, "key_id": "k1"}
    now = time.time()
    await log.add({**base, "ts": now - 3 * 86400, "status_code": 500, "key_name": "old"})
    await log.add({**base, "ts": now - 60, "status_code": 200, "key_name": "new"})
    await log.add({**base, "ts": now - 30, "status_code": 503, "key_name": "new"})
    await log.add({**base, "ts": now - 10, "status_code": 200, "poll": 1})
    (row,) = (await log.key_usage())["data"]
    assert row["total"] == 3
    assert row["requests_24h"] == 2
    assert row["errors_24h"] == 1
    assert row["key_name"] == "new"
    assert abs(row["last_request_at"] - (now - 30)) < 1e-3
    await log.close()


async def test_prune(settings: Settings):
    from muse2api.services.request_log import RequestLog

    log = RequestLog(settings.requests_db)
    base = {"method": "GET", "path": "/v1/models", "status_code": 200, "latency_ms": 1}
    await log.add({**base, "ts": 1.0})
    await log.add({**base, "ts": 9e12})
    assert await log.prune(14) == 1
    assert (await log.query())["total"] == 1
    await log.close()


def test_dashboard_html(client: TestClient):
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "<title>muse2api dashboard</title>" in r.text


def _wait_task(client: TestClient, auth: dict[str, str], task_id: str):
    import time

    for _ in range(200):
        body = client.get(f"/v1/videos/{task_id}", headers=auth).json()
        if body["status"] in ("completed", "failed", "succeeded", "cancelled"):
            return body
        time.sleep(0.01)
    raise AssertionError(f"task {task_id} did not finish")


def _poll_task_rows(client: TestClient, admin: dict[str, str]) -> dict[str, dict]:
    """Wait until every submit row carries a task outcome, then return by task_id."""
    import time

    rows: dict[str, dict] = {}
    for _ in range(100):
        rows = {r["task_id"]: r for r in _requests(client, admin, hide_polls="true")}
        if all(r["task_status"] for r in rows.values()):
            break
        time.sleep(0.01)
    return rows


def test_failed_task_counts_as_error(client: TestClient, auth: dict[str, str], admin: dict[str, str], monkeypatch: pytest.MonkeyPatch):
    from muse2api.drivers.mock import MockDriver
    from muse2api.errors import UpstreamTimeout

    ok = client.post("/v1/videos", headers=auth, json={"prompt": "a wave"}).json()
    _wait_task(client, auth, ok["id"])

    async def boom(self, account, req):
        raise UpstreamTimeout("video generation timed out")

    monkeypatch.setattr(MockDriver, "generate_video", boom)
    bad = client.post("/v1/videos", headers=auth, json={"prompt": "a wave"}).json()
    _wait_task(client, auth, bad["id"])

    rows = _poll_task_rows(client, admin)
    # Both submits answered 200; only the task outcome tells them apart.
    assert rows[ok["id"]]["status_code"] == rows[bad["id"]]["status_code"] == 200
    assert (rows[ok["id"]]["task_status"], rows[bad["id"]]["task_status"]) == ("succeeded", "failed")
    assert rows[bad["id"]]["error"] == "video generation timed out"
    assert rows[bad["id"]]["task_ms"] >= 0
    assert [r["task_id"] for r in _requests(client, admin, status="failed")] == [bad["id"]]

    s = client.get("/admin/stats", headers=admin, params={"window": "1h"}).json()
    assert (s["total"], s["errors"], s["server_errors"]) == (2, 1, 0)
    assert (sum(p["count"] for p in s["series"]), sum(p["errors"] for p in s["series"])) == (2, 1)
    usage = client.get("/admin/keys", headers=admin).json()["builtin"][0]["usage"]
    assert (usage["requests_24h"], usage["errors_24h"]) == (2, 1)


async def test_task_outcome_before_row_and_backfill(settings: Settings):
    import time
    from types import SimpleNamespace

    from muse2api.services.request_log import RequestLog
    from muse2api.services.tasks import TaskStatus

    log = RequestLog(settings.requests_db)
    now = time.time()
    base = {"method": "POST", "path": "/v1/images/generations", "status_code": 200,
            "latency_ms": 5, "ts": now}
    # The task fails before the middleware has written its submit row.
    await log.finish_task("task_early", "failed", now + 1, "no usable account", None)
    await log.add({**base, "task_id": "task_early"})
    # A row from before outcomes were recorded, filled in at startup.
    await log.add({**base, "task_id": "task_old"})
    old = SimpleNamespace(id="task_old", status=TaskStatus.FAILED, finished=True,
                          updated_at=now + 2, error={"message": "interrupted"})
    await log.backfill_tasks([old])

    rows = {r["task_id"]: r for r in (await log.query())["data"]}
    assert rows["task_early"]["task_status"] == "failed"
    assert rows["task_early"]["error"] == "no usable account"
    assert rows["task_old"]["task_status"] == "failed"
    assert (await log.stats("1h"))["errors"] == 2
    await log.close()
