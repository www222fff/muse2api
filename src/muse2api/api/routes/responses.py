"""OpenAI Responses API (``/v1/responses``) — reserved.

TODO(contributors): map ``input`` / ``instructions`` onto ``flatten_messages``
and emit ``response.output_text.delta`` events from ``Gateway.chat_stream``.
"""

from fastapi import APIRouter, Depends, Request

from ...errors import FeatureNotImplemented
from ..deps import require_api_key

router = APIRouter(tags=["responses"], dependencies=[Depends(require_api_key)])


@router.post("/v1/responses")
async def create_response(request: Request) -> None:
    try:
        body = await request.json()
        request.state.model = body.get("model") if isinstance(body, dict) else None
    except ValueError:
        pass
    raise FeatureNotImplemented("/v1/responses is planned; use /v1/chat/completions for now")
