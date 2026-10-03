from __future__ import annotations

import base64
import json
import time


def test_health(client):
    assert client.get("/healthz").json()["ok"] is True
    assert client.get("/readyz").json()["ready"] is True


def test_auth_required(client):
    r = client.get("/v1/models")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_api_key"


def test_models(client, auth):
    ids = {m["id"] for m in client.get("/v1/models", headers=auth).json()["data"]}
    assert {"muse-chat", "muse-image", "muse-video", "gpt-4o"} <= ids


def test_chat_non_stream(client, auth):
    r = client.post("/v1/chat/completions", headers=auth, json={
        "model": "gpt-4o", "messages": [{"role": "user", "content": "hello"}]})
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "gpt-4o"
    assert "hello" in body["choices"][0]["message"]["content"]


def test_chat_stream(client, auth):
    with client.stream("POST", "/v1/chat/completions", headers=auth, json={
        "messages": [{"role": "user", "content": "stream me"}], "stream": True,
        "stream_options": {"include_usage": True},
    }) as r:
        assert r.status_code == 200
        lines = [ln[6:] for ln in r.iter_lines() if ln.startswith("data: ")]
    assert lines[-1] == "[DONE]"
    chunks = [json.loads(x) for x in lines[:-1]]
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c["choices"])
    assert "stream me" in text
    assert chunks[-1]["usage"]["total_tokens"] > 0


def test_chat_wrong_model_kind(client, auth):
    r = client.post("/v1/chat/completions", headers=auth, json={
        "model": "muse-video", "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 400


def test_image_url_and_media(client, auth):
    r = client.post("/v1/images/generations", headers=auth,
                    json={"prompt": "a cat", "n": 2, "size": "1:1"})
    assert r.status_code == 200
    data = r.json()["data"]
    assert len(data) == 2
    path = data[0]["url"].split("testserver", 1)[1]
    media = client.get(path)
    assert media.status_code == 200
    assert media.headers["content-type"] == "image/png"


def test_image_b64(client, auth):
    r = client.post("/v1/images/generations", headers=auth,
                    json={"prompt": "a dog", "response_format": "b64_json"})
    assert r.json()["data"][0]["b64_json"]


def _fake_matting(client, monkeypatch) -> list[bytes]:
    seen: list[bytes] = []

    async def fake_remove(data: bytes) -> bytes:
        seen.append(data)
        return b"cutout-png"

    monkeypatch.setattr(client.app.state.services.matting, "remove", fake_remove)
    return seen


def test_image_transparent_background(client, auth, monkeypatch):
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


def test_image_transparent_saved_as_png(client, auth, monkeypatch):
    _fake_matting(client, monkeypatch)
    r = client.post("/v1/images/generations", headers=auth,
                    json={"prompt": "a fox", "background": "transparent"})
    assert r.json()["data"][0]["url"].endswith(".png")


def test_image_opaque_background_skips_matting(client, auth, monkeypatch):
    seen = _fake_matting(client, monkeypatch)
    r = client.post("/v1/images/generations", headers=auth,
                    json={"prompt": "a fox", "background": "opaque"})
    assert r.status_code == 200
    assert seen == []
    assert r.json()["data"][0]["revised_prompt"] == "a fox"


def _capture_image_requests(client, monkeypatch):
    seen = []
    gateway = client.app.state.services.gateway
    original = gateway.generate_image

    async def spy(req):
        seen.append(req)
        return await original(req)

    monkeypatch.setattr(gateway, "generate_image", spy)
    return seen


def test_image_reference_images(client, auth, monkeypatch):
    seen = _capture_image_requests(client, monkeypatch)
    png = base64.b64encode(b"\x89PNG fake").decode()
    r = client.post("/v1/images/generations", headers=auth,
                    json={"prompt": "a model wearing this hoodie",
                          "image": [f"data:image/png;base64,{png}", f"data:image/jpeg;base64,{png}"]})
    assert r.status_code == 200
    refs = seen[0].reference_images
    assert [i.mime for i in refs] == ["image/png", "image/jpeg"]
    assert refs[0].data == b"\x89PNG fake"


def test_image_single_reference_string(client, auth, monkeypatch):
    seen = _capture_image_requests(client, monkeypatch)
    png = base64.b64encode(b"x").decode()
    r = client.post("/v1/images/generations", headers=auth,
                    json={"prompt": "p", "image": f"data:image/png;base64,{png}"})
    assert r.status_code == 200
    assert len(seen[0].reference_images) == 1


def test_image_too_many_references(client, auth):
    png = "data:image/png;base64," + base64.b64encode(b"x").decode()
    r = client.post("/v1/images/generations", headers=auth, json={"prompt": "p", "image": [png] * 5})
    assert r.status_code == 400


def test_image_invalid_background(client, auth):
    r = client.post("/v1/images/generations", headers=auth,
                    json={"prompt": "a fox", "background": "glass"})
    assert r.status_code == 400


def test_image_async_task(client, auth):
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
    assert view["status"] == "succeeded"
    url = view["result"]["data"][0]["url"]  # async results are always stored as media URLs
    media = client.get(url.split("http://testserver", 1)[-1], headers=auth)
    assert media.status_code == 200 and media.headers["content-type"] == "image/png"


def test_image_task_not_found(client, auth):
    assert client.get("/v1/images/generations/task_nope", headers=auth).status_code == 404


def test_video_task(client, auth):
    r = client.post("/v1/videos", headers=auth, json={"prompt": "waves", "duration": 5})
    task_id = r.json()["id"]
    for _ in range(50):
        task = client.get(f"/v1/videos/{task_id}", headers=auth).json()
        if task["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.02)
    assert task["status"] == "succeeded", task
    assert task["result"]["url"].endswith(".mp4")


def test_reserved_endpoints(client, auth):
    assert client.post("/v1/responses", headers=auth, json={}).status_code == 501


def _capture_image_requests(client, monkeypatch) -> list:
    gateway = client.app.state.services.gateway
    original, seen = gateway.generate_image, []

    async def spy(req):
        seen.append(req)
        return await original(req)

    monkeypatch.setattr(gateway, "generate_image", spy)
    return seen


_PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(b"\x89PNG-ref").decode()


def test_image_generation_with_reference(client, auth, monkeypatch):
    seen = _capture_image_requests(client, monkeypatch)
    r = client.post("/v1/images/generations", headers=auth,
                    json={"prompt": "same fox, wearing a scarf", "image": _PNG_DATA_URL})
    assert r.status_code == 200
    refs = seen[0].reference_images
    assert [(i.data, i.mime) for i in refs] == [(b"\x89PNG-ref", "image/png")]


def test_image_generation_reference_list_limit(client, auth):
    r = client.post("/v1/images/generations", headers=auth,
                    json={"prompt": "x", "image": [_PNG_DATA_URL] * 5})
    assert r.status_code == 400


def test_image_edits_multipart(client, auth, monkeypatch):
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


def test_image_edits_rejects_bad_input(client, auth):
    png = ("image", ("a.png", b"img", "image/png"))
    no_image = client.post("/v1/images/edits", headers=auth, data={"prompt": "x"})
    assert no_image.status_code == 400
    mask = client.post("/v1/images/edits", headers=auth, data={"prompt": "x"},
                       files=[png, ("mask", ("m.png", b"mask", "image/png"))])
    assert mask.status_code == 400
    bad_n = client.post("/v1/images/edits", headers=auth, data={"prompt": "x", "n": "9"},
                        files=[png])
    assert bad_n.status_code == 400


def test_admin_accounts_crud(client, auth, admin):
    assert client.get("/admin/accounts", headers=auth).status_code == 401

    r = client.post("/admin/accounts", headers=admin,
                    json={"label": "a1", "cookies": {"hatch_sess": "secretvalue123"}})
    assert r.status_code == 200
    acc = r.json()["account"]
    assert acc["cookies"]["hatch_sess"] != "secretvalue123"
    assert "hatch_vml" in r.json()["missing_cookies"]

    r = client.patch(f"/admin/accounts/{acc['id']}", headers=admin, json={"enabled": False})
    assert r.json()["account"]["enabled"] is False

    assert len(client.get("/admin/accounts", headers=admin).json()["data"]) == 1
    assert client.post(f"/admin/accounts/{acc['id']}/renew", headers=admin).json()["ok"]
    assert client.delete(f"/admin/accounts/{acc['id']}", headers=admin).status_code == 200
    assert client.get("/admin/status", headers=admin).json()["accounts"]["total"] == 0
