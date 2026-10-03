"""OpenAI-compatible request schemas. Unknown fields are accepted and ignored so that
clients sending newer parameters keep working."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="allow")


class ChatMessage(_Lenient):
    role: str
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None


class ChatCompletionRequest(_Lenient):
    model: str | None = None
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    stream_options: dict[str, Any] | None = None
    user: str | None = None
    # Accepted for compatibility; muse.ai does not expose sampling or tool controls.
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any = None


class ImageGenerationRequest(_Lenient):
    prompt: str = Field(min_length=1)
    model: str | None = None
    n: int = Field(default=1, ge=1, le=4)
    size: str | None = None
    response_format: Literal["url", "b64_json"] = "url"
    # Same values as OpenAI gpt-image-1. "transparent" cuts the subject out locally
    # and returns an RGBA PNG; "auto"/"opaque" leave the image as generated.
    background: Literal["auto", "opaque", "transparent"] | None = None
    image: str | list[str] | None = Field(
        default=None, description="Reference image(s) as data URL, http(s) URL or base64 (max 4)")
    # A sync call can outlive a proxy's timeout (Cloudflare cuts at ~100 s with 524), so
    # "async": true returns a task to poll at GET /v1/images/generations/{task_id}.
    async_: bool = Field(default=False, alias="async")
    user: str | None = None


class VideoCreateRequest(_Lenient):
    prompt: str = Field(min_length=1)
    model: str | None = None
    size: str | None = None
    duration: int | None = Field(default=None, ge=1, le=60)
    seconds: int | None = Field(default=None, ge=1, le=60)
    image: str | None = Field(default=None, description="First frame as data URL or http(s) URL")
    input_reference: str | None = None


class AccountCreate(BaseModel):
    label: str = ""
    cookies: dict[str, str]
    cookie_expires: dict[str, int] = Field(default_factory=dict)
    enabled: bool = True


class AccountUpdate(BaseModel):
    label: str | None = None
    enabled: bool | None = None
    cookies: dict[str, str] | None = None
    cookie_expires: dict[str, int] | None = None


class KeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    note: str = Field(default="", max_length=500)


class KeyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    note: str | None = Field(default=None, max_length=500)
    revoked: bool | None = None
