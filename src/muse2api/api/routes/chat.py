from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from ...core.media import load_image_ref
from ...core.models import resolve_model
from ...core.prompt import flatten_messages, message_turns
from ...core.tools import extract_tool_calls, sanitize_response_text
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
                        text: str, prompt_text: str) -> dict:
    """Assemble the non-streaming chat completion response."""
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model_name,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
        "usage": _usage(prompt_text, text),
    }


def _usage(prompt_text: str, completion_text: str) -> dict:
    pt = _approx_tokens(prompt_text)
    ct = _approx_tokens(completion_text)
    return {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct}


def _tool_calls_payload(completion_id: str, created: int, model_name: str,
                        text: str, tool_calls: list[dict[str, Any]],
                        prompt_text: str) -> dict:
    """Assemble a non-streaming completion that ends in tool calls."""
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model_name,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": text,
                "tool_calls": tool_calls,
            },
            "finish_reason": "tool_calls",
        }],
        "usage": _usage(prompt_text, text),
    }


def _tool_deltas(tool_calls: list[dict[str, Any]]) -> list[dict]:
    """Render tool calls as streaming delta fragments."""
    out = []
    for i, tc in enumerate(tool_calls):
        out.append({
            "index": i,
            "id": tc["id"],
            "type": "function",
            "function": {
                "name": tc["function"]["name"],
                "arguments": tc["function"]["arguments"],
            },
        })
    return out


def _tool_calls_chunk(completion_id: str, created: int, model_name: str,
                      remaining: str, tool_calls: list[dict[str, Any]]) -> bytes:
    return _sse({
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model_name,
        "choices": [{
            "index": 0,
            "delta": {
                "role": "assistant",
                "content": remaining,
                "tool_calls": _tool_deltas(tool_calls),
            },
            "finish_reason": None,
        }],
    })


def _tool_calls_finish(completion_id: str, created: int, model_name: str) -> bytes:
    return _sse({
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model_name,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
    })


def _usage_chunk(completion_id: str, created: int, model_name: str,
                 prompt_text: str, completion_text: str) -> bytes:
    return _sse({
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model_name,
        "choices": [],
        "usage": _usage(prompt_text, completion_text),
    })


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


def _available_tool_names(body: ChatCompletionRequest) -> list[str]:
    return [
        t.get("function", t).get("name") for t in (body.tools or []) if isinstance(t, dict)
    ]


def _last_user_query_from_body(body: ChatCompletionRequest) -> str:
    for m in reversed(body.messages):
        if m.role == "user":
            if isinstance(m.content, str):
                return m.content
            if isinstance(m.content, list):
                parts: list[str] = []
                for p in m.content:
                    if isinstance(p, dict) and "text" in p:
                        parts.append(str(p["text"]))
                return " ".join(parts)
    return ""


async def _non_streaming(stream, body: ChatCompletionRequest, flat, completion_id: str,
                         created: int, model_name: str) -> dict:
    """Non-streaming completion, with optional tool-call reconstruction."""
    text = "".join([d async for d in stream])
    user_q = _last_user_query_from_body(body)
    if body.tools:
        tool_calls, remaining = extract_tool_calls(
            text, _available_tool_names(body), user_query=user_q
        )
        if tool_calls:
            return _tool_calls_payload(completion_id, created, model_name,
                                       sanitize_response_text(remaining or ""), tool_calls, flat.text)
    clean_text = sanitize_response_text(text)
    return _completion_payload(completion_id, created, model_name, clean_text, flat.text)


@router.post("/v1/chat/completions")
async def chat_completions(body: ChatCompletionRequest, request: Request,
                           svc: Annotated[Services, Depends(get_services)]):
    request.state.model = body.model  # logged even if it does not resolve
    spec = resolve_model(body.model, "chat")
    request.state.model = body.model or spec.id
    flat = flatten_messages([m.model_dump() for m in body.messages], tools=body.tools)
    if not flat.text and not flat.images:
        raise InvalidRequest("messages contain no text or images")

    req, cancel = await _build_chat_request(request, body, svc, flat)
    completion_id = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())
    model_name = body.model or spec.id
    stream = svc.gateway.chat_stream(req)

    if not body.stream:
        return await _non_streaming(stream, body, flat, completion_id, created, model_name)

    return await _streaming(
        stream, svc, body, flat, _CompletionIds(completion_id, created, model_name), cancel
    )


@dataclass(frozen=True)
class _CompletionIds:
    """Identifiers shared by every completion response built in this module."""
    completion_id: str
    created: int
    model_name: str


async def _streaming(stream, svc, body: ChatCompletionRequest, flat,
                     ids: _CompletionIds, cancel) -> StreamingResponse:
    """Build the streaming SSE response (with or without first-delta pre-fetch)."""
    completion_id, created, model_name = ids.completion_id, ids.created, ids.model_name
    available_tool_names = _available_tool_names(body)

    def chunk(delta: dict, finish: str | None = None) -> dict:
        return {
            "id": completion_id, "object": "chat.completion.chunk", "created": created,
            "model": model_name,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    async def _stream_tools(first: str) -> AsyncIterator[bytes]:
        """Streamed chat with tool extraction (full output is buffered)."""
        produced = first or ""
        async for delta in stream:
            produced += delta
        user_q = _last_user_query_from_body(body)
        tool_calls, remaining = extract_tool_calls(
            produced, available_tool_names, user_query=user_q
        )
        if tool_calls:
            clean_remaining = sanitize_response_text(remaining or "")
            yield _tool_calls_chunk(completion_id, created, model_name, clean_remaining, tool_calls)
            yield _tool_calls_finish(completion_id, created, model_name)
        else:
            clean_text = sanitize_response_text(produced)
            yield _sse(chunk({"role": "assistant", "content": clean_text}))
            yield _sse(chunk({}, "stop"))
        if (body.stream_options or {}).get("include_usage"):
            yield _usage_chunk(completion_id, created, model_name, flat.text, produced)

    async def _stream_plain(first: str) -> AsyncIterator[bytes]:
        """Streamed chat without tool extraction (deltas are forwarded)."""
        produced = first or ""
        yield _sse(chunk({"role": "assistant", "content": produced}))
        async for delta in stream:
            produced += delta
            yield _sse(chunk({"content": delta}))
        yield _sse(chunk({}, "stop"))
        if (body.stream_options or {}).get("include_usage"):
            yield _usage_chunk(completion_id, created, model_name, flat.text, produced)

    async def events(first: str | None) -> AsyncIterator[bytes]:
        # ``first`` is the pre-fetched first delta when heartbeats are disabled;
        # when enabled it is None and the first token is awaited here, after the
        # 200 (headers) has already been committed to the client.
        try:
            if first is None:
                first, heartbeats = await _collect_first(stream, svc)
                for beat in heartbeats:
                    yield beat
            if body.tools:
                async for ev in _stream_tools(first):
                    yield ev
            else:
                async for ev in _stream_plain(first):
                    yield ev
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
