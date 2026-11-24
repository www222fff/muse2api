from __future__ import annotations

import base64
import json
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from muse2api.config import Settings


def test_health(client: TestClient):
    assert client.get("/healthz").json()["ok"] is True
    assert client.get("/readyz").json()["ready"] is True


def test_auth_required(client: TestClient):
    r = client.get("/v1/models")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_api_key"


def test_models(client: TestClient, auth: dict[str, str]):
    ids = {m["id"] for m in client.get("/v1/models", headers=auth).json()["data"]}
    assert {"muse-chat", "muse-image", "muse-video", "gpt-4o"} <= ids


def test_chat_non_stream(client: TestClient, auth: dict[str, str]):
    r = client.post("/v1/chat/completions", headers=auth, json={
        "model": "gpt-4o", "messages": [{"role": "user", "content": "hello"}]})
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "gpt-4o"
    assert "hello" in body["choices"][0]["message"]["content"]


def _sse_lines(r: Any) -> list[str]:
    return [ln[6:] for ln in r.iter_lines() if ln.startswith("data: ")]


def _chat_chunks(lines: list[str]) -> list[dict[str, Any]]:
    return [json.loads(x) for x in lines[:-1]]


def _chunk_text(chunks: list[dict[str, Any]]) -> str:
    return "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c["choices"])


def test_chat_stream(client: TestClient, auth: dict[str, str]):
    with client.stream("POST", "/v1/chat/completions", headers=auth, json={
        "messages": [{"role": "user", "content": "stream me"}], "stream": True,
        "stream_options": {"include_usage": True},
    }) as r:
        assert r.status_code == 200
        lines = _sse_lines(r)
    assert lines[-1] == "[DONE]"
    chunks = _chat_chunks(lines)
    assert "stream me" in _chunk_text(chunks)
    assert chunks[-1]["usage"]["total_tokens"] > 0


def test_chat_wrong_model_kind(client: TestClient, auth: dict[str, str]):
    r = client.post("/v1/chat/completions", headers=auth, json={
        "model": "muse-video", "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 400


def test_image_url_and_media(client: TestClient, auth: dict[str, str]):
    r = client.post("/v1/images/generations", headers=auth,
                    json={"prompt": "a cat", "n": 2, "size": "1:1"})
    assert r.status_code == 200
    data = r.json()["data"]
    assert len(data) == 2
    path = data[0]["url"].split("testserver", 1)[1]
    media = client.get(path)
    assert media.status_code == 200
    assert media.headers["content-type"] == "image/png"


def test_image_b64(client: TestClient, auth: dict[str, str]):
    r = client.post("/v1/images/generations", headers=auth,
                    json={"prompt": "a dog", "response_format": "b64_json"})
    assert r.json()["data"][0]["b64_json"]


def _fake_matting(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    seen: list[bytes] = []

    async def fake_remove(data: bytes) -> bytes:
        seen.append(data)
        return b"cutout-png"

    monkeypatch.setattr(getattr(client.app, "state").services.matting, "remove", fake_remove)
    return seen


def test_image_transparent_background(client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch):
    seen = _fake_matting(client, monkeypatch)
    r = client.post("/v1/images/generations", headers=auth,
                    json={"prompt": "a fox", "n": 2, "background": "transparent",
                          "response_format": "b64_json"})
    assert r.status_code == 200
    data = r.json()["data"]
    assert len(seen) == 2
    assert all(base64.b64decode(d["b64_json"]) == b"cutout-png" for d in data)
    # The upstream prompt asks for an easy-to-cut background, never "transparent".
    assert "plain" in data[0]["revised_prompt"]
    assert "transparent" not in data[0]["revised_prompt"]


def test_image_transparent_saved_as_png(client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch):
    _fake_matting(client, monkeypatch)
    r = client.post("/v1/images/generations", headers=auth,
                    json={"prompt": "a fox", "background": "transparent"})
    assert r.json()["data"][0]["url"].endswith(".png")


def test_image_opaque_background_skips_matting(client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch):
    seen = _fake_matting(client, monkeypatch)
    r = client.post("/v1/images/generations", headers=auth,
                    json={"prompt": "a fox", "background": "opaque"})
    assert r.status_code == 200
    assert seen == []
    assert r.json()["data"][0]["revised_prompt"] == "a fox"


def _capture_image_requests(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    seen = []
    gateway = getattr(client.app, "state").services.gateway
    original = gateway.generate_image

    async def spy(req):
        seen.append(req)
        return await original(req)

    monkeypatch.setattr(gateway, "generate_image", spy)
    return seen


def test_image_reference_images(client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch):
    seen = _capture_image_requests(client, monkeypatch)
    png = base64.b64encode(b"\x89PNG fake").decode()
    r = client.post("/v1/images/generations", headers=auth,
                    json={"prompt": "a model wearing this hoodie",
                          "image": [f"data:image/png;base64,{png}", f"data:image/jpeg;base64,{png}"]})
    assert r.status_code == 200
    refs = seen[0].reference_images
    assert [i.mime for i in refs] == ["image/png", "image/jpeg"]
    assert refs[0].data == b"\x89PNG fake"


def test_image_single_reference_string(client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch):
    seen = _capture_image_requests(client, monkeypatch)
    png = base64.b64encode(b"x").decode()
    r = client.post("/v1/images/generations", headers=auth,
                    json={"prompt": "p", "image": f"data:image/png;base64,{png}"})
    assert r.status_code == 200
    assert len(seen[0].reference_images) == 1


def test_image_too_many_references(client: TestClient, auth: dict[str, str]):
    png = "data:image/png;base64," + base64.b64encode(b"x").decode()
    r = client.post("/v1/images/generations", headers=auth, json={"prompt": "p", "image": [png] * 5})
    assert r.status_code == 400


def test_image_invalid_background(client: TestClient, auth: dict[str, str]):
    r = client.post("/v1/images/generations", headers=auth,
                    json={"prompt": "a fox", "background": "glass"})
    assert r.status_code == 400


def test_image_async_task(client: TestClient, auth: dict[str, str]):
    import time
    png = "data:image/png;base64," + base64.b64encode(b"x").decode()
    r = client.post("/v1/images/generations", headers=auth,
                    json={"prompt": "a fox", "image": png, "async": True, "response_format": "b64_json"})
    assert r.status_code == 200
    task = r.json()
    assert task["object"] == "image.task" and task["id"].startswith("task_")
    for _ in range(50):
        view = client.get(f"/v1/images/generations/{task['id']}", headers=auth).json()
        if view["status"] == "succeeded":
            break
        time.sleep(0.02)
    assert view["status"] == "succeeded"  # type: ignore[possibly-unbound]
    url = view["result"]["data"][0]["url"]  # async results are always stored as media URLs  # type: ignore[possibly-unbound]
    media = client.get(url.split("http://testserver", 1)[-1], headers=auth)
    assert media.status_code == 200 and media.headers["content-type"] == "image/png"


def test_image_task_not_found(client: TestClient, auth: dict[str, str]):
    assert client.get("/v1/images/generations/task_nope", headers=auth).status_code == 404


def test_video_task(client: TestClient, auth: dict[str, str]):
    r = client.post("/v1/videos", headers=auth, json={"prompt": "waves", "duration": 5})
    task_id = r.json()["id"]
    for _ in range(50):
        task = client.get(f"/v1/videos/{task_id}", headers=auth).json()
        if task["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.02)
    assert task["status"] == "succeeded", task  # type: ignore[possibly-unbound]
    assert task["result"]["url"].endswith(".mp4")  # type: ignore[possibly-unbound]


def _capture_chat_requests(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    gateway = getattr(client.app, "state").services.gateway
    original, seen = gateway.chat_stream, []

    async def spy(req):
        seen.append(req)
        async for d in original(req):
            yield d

    original_complete = gateway.chat_complete

    async def spy_complete(req):
        seen.append(req)
        return await original_complete(req)

    monkeypatch.setattr(gateway, "chat_stream", spy)
    monkeypatch.setattr(gateway, "chat_complete", spy_complete)
    return seen


def test_conversation_key_is_scoped_by_api_key(client: TestClient, auth: dict[str, str], admin: dict[str, str], monkeypatch: pytest.MonkeyPatch):
    seen = _capture_chat_requests(client, monkeypatch)
    other = client.post("/admin/keys", headers=admin, json={"name": "other"}).json()
    body = {"model": "muse-chat", "user": "alice", "messages": [{"role": "user", "content": "hi"}]}
    assert client.post("/v1/chat/completions", headers=auth, json=body).status_code == 200
    assert client.post("/v1/chat/completions", json=body,
                       headers={"Authorization": f"Bearer {other['api_key']}"}).status_code == 200
    assert client.post("/v1/responses", headers=auth,
                       json={"model": "muse-chat", "user": "alice", "input": "hi"}).status_code == 200
    anon = {"model": "muse-chat", "messages": [{"role": "user", "content": "hi"}]}
    assert client.post("/v1/chat/completions", headers=auth, json=anon).status_code == 200
    hints = [r.conversation_hint for r in seen]
    assert hints == ["legacy:alice", f"{other['key']['id']}:alice", "legacy:alice", None]


def test_responses_non_stream_string_input(client: TestClient, auth: dict[str, str]):
    r = client.post("/v1/responses", headers=auth, json={"input": "hello"})
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "response"
    assert body["status"] == "completed"
    assert body["id"].startswith("resp_")
    out = body["output"][0]
    assert out["type"] == "message" and out["role"] == "assistant"
    assert "hello" in out["content"][0]["text"]
    assert body["usage"]["total_tokens"] > 0


def test_responses_items_and_instructions(client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch):
    seen = _capture_chat_requests(client, monkeypatch)
    r = client.post("/v1/responses", headers=auth, json={
        "instructions": "be terse",
        "input": [{"role": "user", "content": "what time"},
                  {"type": "message", "role": "assistant", "content": "noon"},
                  {"role": "user", "content": "and date"}]})
    assert r.status_code == 200
    # instructions folded in as the leading system-instructions block, turns preserved.
    assert seen[0].prompt.startswith("<system_instructions>\nbe terse\n</system_instructions>")
    assert seen[0].turns[0] == ("system", "be terse")


def _responses_events(r: Any) -> list[dict[str, Any]]:
    return [json.loads(ln[6:]) for ln in r.iter_lines() if ln.startswith("data: ")]


def _event_types(events: list[dict[str, Any]]) -> list[str]:
    return [e["type"] for e in events]


def _delta_text(events: list[dict[str, Any]]) -> str:
    return "".join(e["delta"] for e in events if e["type"] == "response.output_text.delta")


def _sequence_numbers(events: list[dict[str, Any]]) -> list[int]:
    return [e["sequence_number"] for e in events]


def _is_continuous(seqs: list[int]) -> bool:
    return seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


def test_responses_stream(client: TestClient, auth: dict[str, str]):
    with client.stream("POST", "/v1/responses", headers=auth, json={
        "input": "stream me", "stream": True,
    }) as r:
        assert r.status_code == 200
        events = _responses_events(r)
    types = _event_types(events)
    assert types[:4] == ["response.created", "response.in_progress",
                        "response.output_item.added", "response.content_part.added"]
    assert set(types[4:-2]) == {"response.output_text.delta"}
    assert types[-2:] == ["response.output_item.done", "response.completed"]
    first, last = events[0], events[-1]
    assert (first["response"]["status"], first["response"]["output"]) == ("in_progress", [])
    final = last["response"]
    text = _delta_text(events)
    assert (final["status"], final["output"][0]["content"][0]["text"]) == ("completed", text)
    assert "stream me" in text
    assert _is_continuous(_sequence_numbers(events))


def test_responses_rejects_bad_input(client: TestClient, auth: dict[str, str]):
    empty = client.post("/v1/responses", headers=auth, json={"input": "  "})
    assert empty.status_code == 400
    tools = client.post("/v1/responses", headers=auth, json={
        "input": [{"type": "function_call", "name": "get_weather",
                   "arguments": "{}"}]})
    assert tools.status_code == 400
    assert "function calling" in tools.json()["error"]["message"]


_PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(b"\x89PNG-ref").decode()


def test_image_generation_with_reference(client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch):
    seen = _capture_image_requests(client, monkeypatch)
    r = client.post("/v1/images/generations", headers=auth,
                    json={"prompt": "same fox, wearing a scarf", "image": _PNG_DATA_URL})
    assert r.status_code == 200
    refs = seen[0].reference_images
    assert [(i.data, i.mime) for i in refs] == [(b"\x89PNG-ref", "image/png")]


def test_image_generation_reference_list_limit(client: TestClient, auth: dict[str, str]):
    r = client.post("/v1/images/generations", headers=auth,
                    json={"prompt": "x", "image": [_PNG_DATA_URL] * 5})
    assert r.status_code == 400


def test_image_edits_multipart(client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch):
    seen = _capture_image_requests(client, monkeypatch)
    r = client.post("/v1/images/edits", headers=auth,
                    data={"prompt": "put them in a cafe", "n": "1", "response_format": "b64_json"},
                    files=[("image[]", ("a.png", b"img-a", "image/png")),
                           ("image[]", ("b.jpg", b"img-b", "application/octet-stream"))])
    assert r.status_code == 200
    assert r.json()["data"][0]["b64_json"]
    req = seen[0]
    assert req.prompt == "put them in a cafe"
    # A generic upload type falls back to the filename's type.
    assert [(i.data, i.mime) for i in req.reference_images] == [
        (b"img-a", "image/png"), (b"img-b", "image/jpeg")]


def test_image_edits_rejects_bad_input(client: TestClient, auth: dict[str, str]):
    png = ("image", ("a.png", b"img", "image/png"))
    no_image = client.post("/v1/images/edits", headers=auth, data={"prompt": "x"})
    assert no_image.status_code == 400
    mask = client.post("/v1/images/edits", headers=auth, data={"prompt": "x"},
                       files=[png, ("mask", ("m.png", b"mask", "image/png"))])
    assert mask.status_code == 400
    bad_n = client.post("/v1/images/edits", headers=auth, data={"prompt": "x", "n": "9"},
                        files=[png])
    assert bad_n.status_code == 400


def test_admin_account_quota(client: TestClient, admin: dict[str, str]):
    r = client.post("/admin/accounts", headers=admin,
                    json={"label": "q1", "cookies": {"hatch_sess": "secretvalue123"}})
    acc_id = r.json()["account"]["id"]

    q = client.get(f"/admin/accounts/{acc_id}/quota", headers=admin)
    assert q.status_code == 200
    body = q.json()
    assert body["account_id"] == acc_id
    assert body["quota"]["plan"] == "free"
    assert body["quota"]["messages_remaining"] == 100
    assert "checked_at" in body["quota"]

    # The snapshot is persisted in meta and visible in the account list view.
    listed = client.get("/admin/accounts", headers=admin).json()["data"][0]
    assert listed["meta"]["quota"]["plan"] == "free"


def test_admin_accounts_crud(client: TestClient, auth: dict[str, str], admin: dict[str, str]):
    assert client.get("/admin/accounts", headers=auth).status_code == 401

    r = client.post("/admin/accounts", headers=admin,
                    json={"label": "a1", "cookies": {"hatch_sess": "secretvalue123"}})
    assert r.status_code == 200
    acc = r.json()["account"]
    assert acc["cookies"]["hatch_sess"] != "secretvalue123"
    assert r.json()["missing_cookies"] == ["hatch_native_auth_device"]

    patched = client.patch(f"/admin/accounts/{acc['id']}", headers=admin, json={"enabled": False})
    assert patched.json()["account"]["enabled"] is False

    assert len(client.get("/admin/accounts", headers=admin).json()["data"]) == 1
    renewed = client.post(f"/admin/accounts/{acc['id']}/renew", headers=admin).json()
    deleted = client.delete(f"/admin/accounts/{acc['id']}", headers=admin)
    total = client.get("/admin/status", headers=admin).json()["accounts"]["total"]
    assert (renewed["ok"], deleted.status_code, total) == (True, 200, 0)


def test_admin_account_cookie_header_string(client: TestClient, admin: dict[str, str]):
    r = client.post("/admin/accounts", headers=admin,
                    json={"label": "hdr", "cookies": "hatch_sess=abc123; hatch_vml=xyz; empty="})
    assert r.status_code == 200
    # The empty value is dropped, like with the dict format.
    acc_id = r.json()["account"]["id"]
    listed = {a["id"]: a for a in client.get("/admin/accounts", headers=admin).json()["data"]}
    masked = listed[acc_id]["cookies"]
    assert set(masked) == {"hatch_sess", "hatch_vml"}
    assert "hatch_vml" not in r.json()["missing_cookies"]
    assert "hatch_gw" not in r.json()["missing_cookies"]


def test_admin_account_cookie_export_list(client: TestClient, admin: dict[str, str]):
    r = client.post("/admin/accounts", headers=admin, json={
        "label": "exp", "cookies": [
            {"name": "hatch_sess", "value": "abc123", "expirationDate": 1790000000.5,
             "domain": ".muse.ai", "httpOnly": True},
            {"name": "hatch_vml", "value": "xyz"},
        ]})
    assert r.status_code == 200
    acc_id = r.json()["account"]["id"]
    listed = {a["id"]: a for a in client.get("/admin/accounts", headers=admin).json()["data"]}
    assert listed[acc_id]["cookie_expires"] == {"hatch_sess": 1790000000}


def test_admin_account_cookie_bad_format(client: TestClient, admin: dict[str, str]):
    bad_entry = client.post("/admin/accounts", headers=admin,
                            json={"cookies": [{"value": "no-name"}]})
    assert bad_entry.status_code == 400
    empty = client.post("/admin/accounts", headers=admin, json={"cookies": "  ;  "})
    assert empty.status_code == 400


def test_admin_accounts_bulk_import(client: TestClient, admin: dict[str, str]):
    r = client.post("/admin/accounts/import", headers=admin, json={"accounts": [
        {"label": "b1", "cookies": {"hatch_sess": "one"}},
        {"label": "b2", "cookies": "hatch_sess=two; hatch_vml=three"},
        {"label": "b3", "cookies": {}},  # fails, but must not abort the batch
    ]})
    assert r.status_code == 200
    rows = r.json()["data"]
    assert [x["ok"] for x in rows] == [True, True, False]
    assert rows[2]["code"] == "invalid_request"
    assert client.get("/admin/status", headers=admin).json()["accounts"]["total"] == 2


def _keepalive_parts(lines: list[str]) -> tuple[list[str], list[str]]:
    keepalives = [ln for ln in lines if ln.startswith(":")]
    data_lines = [ln for ln in lines if ln.startswith("data: ")]
    return keepalives, data_lines


def _streamed_text(data_lines: list[str]) -> str:
    return "".join(
        json.loads(ln[6:])["choices"][0]["delta"].get("content", "")
        for ln in data_lines[:-1] if json.loads(ln[6:])["choices"])


def test_chat_stream_heartbeat(settings: Settings, auth: dict[str, str]):
    # C4: with stream_heartbeat_interval set, the 200 goes out immediately and
    # ': keepalive' SSE comments precede the first data chunk.
    from fastapi.testclient import TestClient

    from muse2api.app import create_app
    from muse2api.drivers.mock import MockDriver

    settings.stream_heartbeat_interval = 0.05
    app = create_app(settings, MockDriver(delay=0.3))
    with TestClient(app) as c:
        with c.stream("POST", "/v1/chat/completions", headers=auth, json={
            "messages": [{"role": "user", "content": "slow stream"}], "stream": True,
        }) as r:
            assert r.status_code == 200
            lines = list(r.iter_lines())
    keepalives, data_lines = _keepalive_parts(lines)
    assert len(keepalives) >= 1, lines
    assert lines.index(keepalives[0]) < lines.index(data_lines[0])
    assert data_lines[-1][6:] == "[DONE]"
    assert "slow stream" in _streamed_text(data_lines)


def test_chat_stream_no_heartbeat_by_default(client: TestClient, auth: dict[str, str]):
    # Default (interval 0): no ': keepalive' comments; first token awaited pre-200.
    with client.stream("POST", "/v1/chat/completions", headers=auth, json={
        "messages": [{"role": "user", "content": "no heartbeat"}], "stream": True,
    }) as r:
        assert r.status_code == 200
        lines = list(r.iter_lines())
    assert not any(ln.startswith(":") for ln in lines)


def test_chat_stream_heartbeat_upstream_error(settings: Settings, auth: dict[str, str]):
    # Heartbeat mode commits the 200 before the first token, so an upstream
    # failure surfaces as an SSE error event instead of an HTTP error.
    from fastapi.testclient import TestClient

    from muse2api.app import create_app
    from muse2api.drivers.mock import MockDriver
    from muse2api.errors import UpstreamError

    class FailingDriver(MockDriver):
        async def chat_stream(self, account, req):
            if False:
                yield
            raise UpstreamError("boom")

    settings.stream_heartbeat_interval = 0.05
    app = create_app(settings, FailingDriver())
    with TestClient(app) as c:
        with c.stream("POST", "/v1/chat/completions", headers=auth, json={
            "messages": [{"role": "user", "content": "x"}], "stream": True,
        }) as r:
            assert r.status_code == 200
            lines = list(r.iter_lines())
    assert any("boom" in ln for ln in lines), lines


# --- SSRF guard for load_image_ref ---

def test_ssrf_rejects_non_http_scheme():
    import pytest

    from muse2api.core.media import _assert_public_url
    from muse2api.errors import InvalidRequest
    with pytest.raises(InvalidRequest):
        _assert_public_url("ftp://example.com/x.png")
    with pytest.raises(InvalidRequest):
        _assert_public_url("file:///etc/passwd")


async def test_ssrf_rejects_non_public_ips():
    import pytest

    from muse2api.core.media import _assert_public_host
    from muse2api.errors import InvalidRequest
    for host in ("127.0.0.1", "10.0.0.1", "192.168.1.1", "169.254.169.254", "::1"):
        with pytest.raises(InvalidRequest):
            await _assert_public_host(host)


async def test_ssrf_accepts_public_ip():
    from muse2api.core.media import _assert_public_host
    await _assert_public_host("8.8.8.8")  # literal IP, no DNS needed


async def test_load_image_ref_rejects_loopback_url():
    import pytest

    from muse2api.core.media import load_image_ref
    from muse2api.errors import InvalidRequest
    with pytest.raises(InvalidRequest):
        await load_image_ref("http://127.0.0.1:9999/x.png")


def test_missing_session_cookies_vm_cookies_optional():
    from muse2api.upstream.muse import missing_session_cookies

    assert missing_session_cookies({"hatch_sess": "s", "hatch_native_auth_device": "d"}) == []
    assert missing_session_cookies({"hatch_sess": "s"}) == ["hatch_native_auth_device"]


async def test_browser_set_cookies_host_prefix_uses_url():
    from muse2api.accounts import Account
    from muse2api.drivers.browser.driver import BrowserDriver

    sent = []

    class FakeSession:
        async def send(self, method, params=None):
            sent.append((method, params))

    acc = Account(cookies={"hatch_sess": "s", "__Host-hatch_one_tap": "t"})
    await BrowserDriver._set_cookies(FakeSession(), acc)  # type: ignore[arg-type]
    by_name = {p["name"]: p for _, p in sent}  # type: ignore[unsupported-operation]
    assert by_name["hatch_sess"]["domain"] == ".muse.ai"  # type: ignore[unsupported-operation]
    host = by_name["__Host-hatch_one_tap"]  # type: ignore[unsupported-operation]
    assert "domain" not in host  # type: ignore[not-iterable]
    assert host["url"] == "https://muse.ai/" and host["path"] == "/" and host["secure"]  # type: ignore[unsupported-operation]
