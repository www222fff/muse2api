"""Regression tests that drive the API through the official ``openai`` SDK.

The `/v1/responses` endpoint (C1) was verified by hand against openai 3.37.0;
these tests lock that in so a future change cannot silently break SDK
compatibility. The `/v1/images/edits` endpoint (C2) is covered the same way.
Requires the ``openai`` package (declared in the ``dev`` extra).
"""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient
from openai import AsyncOpenAI


def _sdk_client(client: TestClient, token: str):
    transport = httpx.ASGITransport(app=client.app)
    http_client = httpx.AsyncClient(transport=transport, base_url="http://testserver")
    sdk = AsyncOpenAI(base_url="http://testserver/v1", api_key=token, http_client=http_client)  # type: ignore[arg-type]
    return sdk, http_client


async def test_sdk_responses_non_streaming(client: TestClient, auth: dict[str, str]):
    token = auth["Authorization"].split(" ", 1)[1]
    sdk, http_client = _sdk_client(client, token)
    try:
        resp = await sdk.responses.create(model="anything-at-all", input="sdk non-stream hello")
        assert resp.object == "response"
        assert resp.status == "completed"
        assert "sdk non-stream hello" in resp.output_text
        assert resp.usage.total_tokens > 0  # type: ignore[attr-defined]
    finally:
        await http_client.aclose()


async def test_sdk_images_edit(client: TestClient, auth: dict[str, str]):
    token = auth["Authorization"].split(" ", 1)[1]
    sdk, http_client = _sdk_client(client, token)
    try:
        resp = await sdk.images.edit(
            image=("a.png", b"\x89PNG-sdk", "image/png"),
            prompt="sdk edit hello",
            response_format="b64_json",
        )
        assert resp.data and resp.data[0].b64_json
    finally:
        await http_client.aclose()


async def test_sdk_responses_streaming(client: TestClient, auth: dict[str, str]):
    token = auth["Authorization"].split(" ", 1)[1]
    sdk, http_client = _sdk_client(client, token)
    try:
        async with sdk.responses.stream(model="anything-at-all", input="sdk stream hello") as stream:
            async for _event in stream:
                pass
            final = await stream.get_final_response()
        assert final.status == "completed"
        assert "sdk stream hello" in final.output_text
    finally:
        await http_client.aclose()
