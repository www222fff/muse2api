"""Model registry: public model ids exposed on /v1/models and alias resolution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..errors import InvalidRequest

ModelKind = Literal["chat", "image", "video"]


@dataclass(frozen=True)
class ModelSpec:
    id: str
    kind: ModelKind
    description: str = ""
    owned_by: str = "muse"


MODELS: dict[str, ModelSpec] = {
    m.id: m
    for m in (
        ModelSpec("muse-spark-1.3-contributor", "chat", "Muse Spark 1.3 Contributor (4B Free Web Tokens)"),
        ModelSpec("muse-spark-1.3", "chat", "Muse Spark 1.3 (Fast reasoning)"),
        ModelSpec("muse-spark-1.2", "chat", "Muse Spark 1.2 (Stable)"),
        ModelSpec("muse-spark", "chat", "Muse Spark"),
        ModelSpec("meta.muse-spark-1.3", "chat", "Meta Muse Spark 1.3"),
        ModelSpec("muse-chat", "chat", "Default muse.ai assistant"),
        ModelSpec("muse-image", "image", "Text-to-image / image editing"),
        ModelSpec("muse-video", "video", "Text-to-video / first-frame image-to-video"),
    )
}

# Popular client-side model names are routed to the closest muse model so that
# existing OpenAI tooling works without reconfiguration.
ALIASES: dict[str, str] = {
    "spark": "muse-spark-1.3-contributor",
    "spark-1.3": "muse-spark-1.3",
    "spark-1.2": "muse-spark-1.2",
    "gpt-4o": "muse-chat",
    "gpt-4o-mini": "muse-chat",
    "gpt-4.1": "muse-chat",
    "gpt-5": "muse-chat",
    "claude": "muse-chat",
    "claude-sonnet-4": "muse-chat",
    "deepseek-chat": "muse-chat",
    "dall-e-3": "muse-image",
    "gpt-image-1": "muse-image",
    "sora": "muse-video",
}

DEFAULT_BY_KIND: dict[ModelKind, str] = {
    "chat": "muse-spark-1.3",
    "image": "muse-image",
    "video": "muse-video",
}


def resolve_model(name: str | None, kind: ModelKind) -> ModelSpec:
    """Resolve a client-supplied model name to a ``ModelSpec`` of the expected kind.

    Unknown names fall back to the default model of that kind; a known model of the
    wrong kind is rejected so that e.g. ``muse-video`` is never silently used for chat.
    """
    if not name:
        return MODELS[DEFAULT_BY_KIND[kind]]
    key = ALIASES.get(name, name)
    spec = MODELS.get(key)
    if spec is None:
        return MODELS[DEFAULT_BY_KIND[kind]]
    if spec.kind != kind:
        message = f"model '{name}' is a {spec.kind} model, expected {kind}"
        raise InvalidRequest(message)
    return spec
