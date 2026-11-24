"""Driver interface: the only layer that knows how to talk to muse.ai.

A driver turns normalised requests into upstream actions for one account at a
time. Scheduling, retries, persistence and the OpenAI wire format all live
above this layer, so a new transport (browser automation, direct HTTP/WebSocket
protocol, ...) only needs to implement this class.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, NoReturn

from ..errors import FeatureNotImplemented

if TYPE_CHECKING:
    import asyncio

    from ..accounts.model import Account

ProgressCallback = Callable[[int], None]
"""Receives a 0-100 progress estimate."""


@dataclass
class InputImage:
    data: bytes
    mime: str = "image/png"


@dataclass
class ChatRequest:
    prompt: str
    model: str
    images: list[InputImage] = field(default_factory=list[InputImage])
    timeout: float = 300.0
    first_token_timeout: float = 75.0
    conversation_hint: str | None = None
    turns: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    """Role/text pairs of the request, used to continue a hot page instead of resending history."""
    cancel: asyncio.Event | None = None


@dataclass
class ImageRequest:
    prompt: str
    model: str
    size: str | None = None
    """Aspect ratio such as ``1:1`` / ``16:9`` or an OpenAI size like ``1024x1024``."""
    n: int = 1
    reference_images: list[InputImage] = field(default_factory=list[InputImage])
    timeout: float = 240.0
    on_progress: ProgressCallback | None = None
    cancel: asyncio.Event | None = None


@dataclass
class VideoRequest:
    prompt: str
    model: str
    size: str | None = None
    duration: int | None = None
    first_frame: InputImage | None = None
    timeout: float = 600.0
    on_progress: ProgressCallback | None = None
    cancel: asyncio.Event | None = None


@dataclass
class MediaResult:
    data: bytes
    mime: str
    kind: Literal["image", "video"]
    width: int | None = None
    height: int | None = None
    revised_prompt: str | None = None


@dataclass
class SessionInfo:
    ok: bool
    cookies: dict[str, str] = field(default_factory=dict[str, str])
    cookie_expires: dict[str, int] = field(default_factory=dict[str, int])
    meta: dict[str, Any] = field(default_factory=dict[str, Any])


@dataclass(frozen=True)
class DriverCapabilities:
    chat: bool = False
    chat_images: bool = False
    image: bool = False
    image_edit: bool = False
    video: bool = False
    renew_session: bool = False
    quota: bool = False


class MuseDriver(ABC):
    name: str = "base"
    requires_account: bool = True
    capabilities: DriverCapabilities = DriverCapabilities()

    async def startup(self) -> None:  # noqa: B027
        """Acquire long-lived resources (browser process, HTTP client, ...)."""

    async def shutdown(self) -> None:  # noqa: B027
        """Release resources acquired in ``startup``."""

    def prewarm(self, account: Account) -> None:  # noqa: B027
        """Start getting ``account`` ready for its first request in the background."""

    async def health(self) -> dict[str, Any]:
        return {"driver": self.name, "ok": True}

    def _unsupported(self, what: str) -> NoReturn:
        """Raise FeatureNotImplemented for a capability this driver lacks."""
        message = f"driver '{self.name}' does not support {what}"
        raise FeatureNotImplemented(message)

    @abstractmethod
    def chat_stream(self, account: Account, req: ChatRequest) -> AsyncIterator[str]:
        """Yield assistant text deltas. Implementations are async generators."""

    async def chat_complete(self, account: Account, req: ChatRequest) -> str:
        """The full reply text for a non-streaming call.

        The default assembles the delta stream, which is exact for drivers
        that yield true deltas. Drivers whose native text is a cumulative
        snapshot override this so the reply is the final snapshot itself,
        never a join of deltas that had to stay append-only while streaming.
        """
        return "".join([delta async for delta in self.chat_stream(account, req)])

    async def generate_image(self, account: Account, req: ImageRequest) -> list[MediaResult]:  # noqa: ARG002
        self._unsupported("image generation")

    async def generate_video(self, account: Account, req: VideoRequest) -> MediaResult:  # noqa: ARG002
        self._unsupported("video generation")

    async def renew_session(self, account: Account) -> SessionInfo:  # noqa: ARG002
        self._unsupported("session renewal")

    async def quota(self, account: Account) -> dict[str, Any]:  # noqa: ARG002
        self._unsupported("quota lookup")
