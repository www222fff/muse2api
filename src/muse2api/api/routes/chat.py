from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from ...core.media import load_image_ref
from ...core.models import resolve_model
from ...core.prompt import flatten_messages, message_turns
from ...drivers.base import ChatRequest, InputImage
from ...errors import InvalidRequest, Muse2APIError
from ...services.container import Services
from ..deps import conversation_key, get_services, require_api_key
from ..schemas import ChatCompletionRequest

log = logging.getLogger(__name__)
router = APIRouter(tags=["chat"], dependencies=[Depends(require_api_key)])


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _sse(payload: dict | str) -> bytes:
    data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return f"data: {data}\n\n".encode()


async def _build_chat_request(request: Request, body: ChatCompletionRequest,
                              svc, flat):
    """Assemble the upstream ChatRequest from the flattened message payload."""
    images = [InputImage(*(await load_image_ref(ref))) for ref in flat.images]
    cancel = asyncio.Event()
    req = ChatRequest(
        prompt=flat.text,
        model=resolve_model(body.model, "chat").id,
        images=images,
        timeout=svc.settings.chat_timeout,
        first_token_timeout=svc.settings.first_token_timeout,
        conversation_hint=conversation_key(request, body.user),
        turns=message_turns([m.model_dump() for m in body.messages]),
        cancel=cancel,
    )
    return req, cancel


def _completion_payload(completion_id: str, created: int, model_name: str,
                        text: str, prompt_text: str):
    """Assemble the non-streaming chat completion response."""
    usage = {
        "prompt_tokens": _approx_tokens(prompt_text),
        "completion_tokens": _approx_tokens(text),
        "total_tokens": _approx_tokens(prompt_text) + _approx_tokens(text),
    }
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model_name,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
        "usage": usage,
    }


async def _collect_first(stream, svc):
    """Pre-fetch the first delta; emit SSE heartbeats while waiting (C4)."""
    interval = float(svc.settings.stream_heartbeat_interval)
    if interval <= 0:
        try:
            return await anext(stream), []
        except StopAsyncIteration:
            return "", []
    pending = asyncio.ensure_future(anext(stream))
    heartbeats: list[bytes] = []
    try:
        while True:
            done, _ = await asyncio.wait({pending}, timeout=interval)
            if done:
                try:
                    return pending.result(), heartbeats
                except StopAsyncIteration:
                    return "", heartbeats
            heartbeats.append(b": keepalive\n\n")
    finally:
        if not pending.done():
            pending.cancel()


@router.post("/v1/chat/completions")
async def chat_completions(body: ChatCompletionRequest, request: Request,
                           svc: Annotated[Services, Depends(get_services)]):
    request.state.model = body.model  # logged even if it does not resolve
    spec = resolve_model(body.model, "chat")
    request.state.model = body.model or spec.id
    flat = flatten_messages([m.model_dump() for m in body.messages])
    if not flat.text and not flat.images:
        raise InvalidRequest("messages contain no text or images")

    req, cancel = await _build_chat_request(request, body, svc, flat)
    completion_id = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())
    model_name = body.model or spec.id
    stream = svc.gateway.chat_stream(req)

    if not body.stream:
        text = "".join([d async for d in stream])
        return _completion_payload(completion_id, created, model_name, text, flat.text)

    def chunk(delta: dict, finish: str | None = None) -> dict:
        return {"id": completion_id, "object": "chat.completion.chunk", "created": created,
                "model": model_name,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    async def events(first: str | None) -> AsyncIterator[bytes]:
        # ``first`` is the pre-fetched first delta when heartbeats are disabled;
        # when enabled it is None and the first token is awaited here, after the
        # 200 (headers) has already been committed to the client.
        try:
            if first is None:
                first, heartbeats = await _collect_first(stream, svc)
                for beat in heartbeats:
                    yield beat
            produced = first or ""
            yield _sse(chunk({"role": "assistant", "content": produced}))
            async for delta in stream:
                produced += delta
                yield _sse(chunk({"content": delta}))
            yield _sse(chunk({}, "stop"))
            if (body.stream_options or {}).get("include_usage"):
                usage = {"prompt_tokens": _approx_tokens(flat.text),
                         "completion_tokens": _approx_tokens(produced)}
                usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
                yield _sse({"id": completion_id, "object": "chat.completion.chunk",
                            "created": created, "model": model_name, "choices": [],
                            "usage": usage})
        except Muse2APIError as exc:
            yield _sse(exc.to_body())
        except Exception:
            log.exception("stream crashed")
            yield _sse({"error": {"message": "internal error", "type": "server_error"}})
        finally:
            cancel.set()
            await stream.aclose()  # type: ignore[attr-defined]  # upstream stream is an async generator
        yield _sse("[DONE]")

    # With stream_heartbeat_interval set (C4), the 200 goes out immediately and
    # ': keepalive' SSE comments are sent while the first token is pending;
    # upstream errors then surface as SSE error events instead of HTTP codes.
    if svc.settings.stream_heartbeat_interval > 0:
        return StreamingResponse(events(None), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    # Default: pull the first delta before committing to a 200 so that
    # account/upstream failures surface as proper HTTP errors instead of a
    # broken event stream.
    try:
        first = await anext(stream)
    except StopAsyncIteration:
        first = ""

    return StreamingResponse(events(first), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
