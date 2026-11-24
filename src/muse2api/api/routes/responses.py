"""OpenAI Responses API (``/v1/responses``).

Maps ``input`` / ``instructions`` onto ``flatten_messages`` and reuses
``Gateway.chat_stream``. Streaming emits the standard Responses events
(``response.created`` / ``response.output_text.delta`` / ``response.completed``).

muse.ai has no function calling, so tool items (``function_call``,
``function_call_output``) are rejected with a clear error instead of being
silently dropped (see C3).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from ...core.media import load_image_ref
from ...core.models import resolve_model
from ...core.prompt import FlatPrompt, flatten_messages, message_turns
from ...drivers.base import ChatRequest, InputImage
from ...errors import InvalidRequest, Muse2APIError
from ...services.container import Services
from ..deps import conversation_key, get_services, require_api_key

log = logging.getLogger(__name__)
router = APIRouter(tags=["responses"], dependencies=[Depends(require_api_key)])


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _sse(payload: dict[str, Any] | str) -> bytes:
    data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return f"data: {data}\n\n".encode()


class ResponseRequest(BaseModel):
    model: str | None = None
    input: Any = None
    instructions: str | None = None
    stream: bool = False
    user: str | None = None
    max_output_tokens: int | None = None

    model_config = ConfigDict(extra="allow")


# Item types the official SDK round-trips but muse.ai cannot fulfil.
_TOOL_ITEMS = {"function_call", "function_call_output", "reasoning", "computer_call",
               "file_search_call", "web_search_call"}


def _input_item_to_message(item: Any) -> dict[str, Any]:
    """Convert one Responses ``input`` list item to a chat-style message."""
    if not isinstance(item, dict):
        message = "input items must be objects"
        raise InvalidRequest(message)
    d = cast(dict[str, Any], item)
    itype = d.get("type")
    if itype in _TOOL_ITEMS:
        message = (
            f"input item type '{itype}' is not supported: "
            "muse.ai has no function calling"
        )
        raise InvalidRequest(message)
    if "role" in d or itype in ("message", None):
        return {"role": d.get("role", "user"),
                "content": d.get("content")}
    message = f"unsupported input item type '{itype}'"
    raise InvalidRequest(message)


def _to_messages(body: ResponseRequest) -> list[dict[str, Any]]:
    """Convert Responses ``input`` / ``instructions`` to chat-style messages."""
    messages: list[dict[str, Any]] = []
    if body.instructions:
        messages.append({"role": "system", "content": body.instructions})
    inp = body.input
    if isinstance(inp, str):
        if inp.strip():
            messages.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        items = cast(list[Any], inp)
        messages.extend(_input_item_to_message(item) for item in items)
    elif inp is not None:
        message = "input must be a string or a list of items"
        raise InvalidRequest(message)
    return messages


def _message_item(msg_id: str, text: str) -> dict[str, Any]:
    return {"id": msg_id, "type": "message", "status": "completed", "role": "assistant",
            "content": [{"type": "output_text", "annotations": [], "text": text}]}


def _usage(input_text: str, output_text: str) -> dict[str, Any]:
    i, o = _approx_tokens(input_text), _approx_tokens(output_text)
    return {"input_tokens": i, "output_tokens": o, "total_tokens": i + o,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0}}


def _response_object(ids: _ResponseIds, text: str, input_text: str,
                     *, status: str = "completed") -> dict[str, Any]:
    return {"id": ids.resp_id, "object": "response", "created_at": ids.created,
            "model": ids.model_name, "status": status,
            "output": [_message_item(ids.msg_id, text)] if status == "completed" else [],
            "usage": _usage(input_text, text), "parallel_tool_calls": True,
            "tool_choice": "auto", "tools": []}


async def _collect_first(stream: AsyncIterator[str], svc: Services) -> tuple[str, list[bytes]]:
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


@dataclass(frozen=True)
class _RespStreamCtx:
    """Bundle passed to the Responses-API streaming event builder."""
    svc: Services
    cancel: asyncio.Event
    ids: _ResponseIds
    flat: FlatPrompt


async def _response_events(
    stream: AsyncIterator[str], ctx: _RespStreamCtx, first: str | None = None,
) -> AsyncIterator[bytes]:
    """SSE event stream for a Responses-API request (heartbeat-aware)."""
    ids = ctx.ids
    msg_id = ids.msg_id
    seq = 0
    try:
        if first is None:
            first, heartbeats = await _collect_first(stream, ctx.svc)
            for beat in heartbeats:
                yield beat
        yield _sse({"type": "response.created", "sequence_number": seq,
                    "response": _response_object(ids, "", ctx.flat.text,
                                                 status="in_progress")})
        seq += 1
        yield _sse({"type": "response.in_progress", "sequence_number": seq,
                    "response": _response_object(ids, "", ctx.flat.text,
                                                 status="in_progress")})
        seq += 1
        yield _sse({"type": "response.output_item.added", "sequence_number": seq,
                    "output_index": 0,
                    "item": {"id": msg_id, "type": "message", "status": "in_progress",
                             "role": "assistant", "content": []}})
        seq += 1
        yield _sse({"type": "response.content_part.added", "sequence_number": seq,
                    "item_id": msg_id, "output_index": 0, "content_index": 0,
                    "part": {"type": "output_text", "text": "", "annotations": []}})
        seq += 1
        produced = first or ""

        def delta_event(delta: str) -> dict[str, Any]:
            return {"type": "response.output_text.delta", "sequence_number": seq,
                    "item_id": msg_id, "output_index": 0, "content_index": 0,
                    "delta": delta, "logprobs": []}

        if produced:
            yield _sse(delta_event(produced))
            seq += 1
        async for delta in stream:
            produced += delta
            yield _sse(delta_event(delta))
            seq += 1
        yield _sse({"type": "response.output_item.done", "sequence_number": seq,
                    "output_index": 0, "item": _message_item(msg_id, produced)})
        seq += 1
        yield _sse({"type": "response.completed", "sequence_number": seq,
                    "response": _response_object(ids, produced, ctx.flat.text)})
    except Muse2APIError as exc:
        yield _sse(exc.to_body())
    except Exception:
        log.exception("responses stream crashed")
        yield _sse({"error": {"message": "internal error", "type": "server_error"}})
    finally:
        ctx.cancel.set()
        await stream.aclose()  # type: ignore[attr-defined]  # upstream stream is an async generator


@dataclass(frozen=True)
class _ResponseIds:
    resp_id: str
    msg_id: str
    created: int
    model_name: str


async def _build_response_chat(
    body: ResponseRequest, request: Request, svc: Services, flat: FlatPrompt,
) -> tuple[ChatRequest, asyncio.Event]:
    """Assemble the upstream ChatRequest plus a cancellation event."""
    images = [InputImage(*(await load_image_ref(ref))) for ref in flat.images]
    cancel = asyncio.Event()
    req = ChatRequest(
        prompt=flat.text,
        model=resolve_model(body.model, "chat").id,
        images=images,
        timeout=svc.settings.chat_timeout,
        first_token_timeout=svc.settings.first_token_timeout,
        conversation_hint=conversation_key(request, body.user),
        turns=message_turns(_to_messages(body)),
        cancel=cancel,
    )
    return req, cancel


def _non_streaming_response(text: str, ids: _ResponseIds, flat: FlatPrompt) -> dict[str, Any]:
    return _response_object(ids, text, flat.text)


@router.post("/v1/responses", response_model=None)
async def create_response(body: ResponseRequest, request: Request,
                          svc: Annotated[Services, Depends(get_services)]) -> dict[str, Any] | StreamingResponse:
    request.state.model = body.model  # logged even if it does not resolve
    spec = resolve_model(body.model, "chat")
    request.state.model = body.model or spec.id
    flat = flatten_messages(_to_messages(body))
    if not flat.text and not flat.images:
        message = "input contains no text or images"
        raise InvalidRequest(message)

    req, cancel = await _build_response_chat(body, request, svc, flat)
    ids = _ResponseIds(
        resp_id="resp_" + uuid.uuid4().hex[:24],
        msg_id="msg_" + uuid.uuid4().hex[:24],
        created=int(time.time()),
        model_name=body.model or spec.id,
    )

    if not body.stream:
        # The driver's final reply text itself, not a join of streamed deltas.
        return _non_streaming_response(await svc.gateway.chat_complete(req), ids, flat)

    stream = svc.gateway.chat_stream(req)
    ctx = _RespStreamCtx(svc, cancel, ids, flat)

    def streaming(first: str | None) -> StreamingResponse:
        return StreamingResponse(
            _response_events(stream, ctx, first),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # With stream_heartbeat_interval set (C4), the 200 goes out immediately;
    # upstream errors then surface as SSE error events instead of HTTP codes.
    if svc.settings.stream_heartbeat_interval > 0:
        return streaming(None)

    # Default: pull the first delta before committing to a 200 so that
    # account/upstream failures surface as proper HTTP errors instead of a
    # broken event stream.
    try:
        first = await anext(stream)
    except StopAsyncIteration:
        first = ""

    return streaming(first)
