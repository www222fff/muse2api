from __future__ import annotations

import hmac

from fastapi import Request

from ..errors import Unauthorized
from ..services.container import Services

_INVALID_OR_MISSING_API_KEY = "invalid or missing API key"


def get_services(request: Request) -> Services:
    return request.app.state.services


def _bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-api-key", "").strip()


def _check(provided: str, expected: str) -> None:
    if not expected or not hmac.compare_digest(provided.encode(), expected.encode()):
        raise Unauthorized(_INVALID_OR_MISSING_API_KEY)


def _matches(provided: str, expected: str) -> bool:
    return bool(expected) and hmac.compare_digest(provided.encode(), expected.encode())


def _identify(request: Request, key_id: str, key_name: str) -> None:
    # Read by the request-log middleware.
    request.state.key_id = key_id
    request.state.key_name = key_name


def require_api_key(request: Request) -> None:
    """Accept the admin key, the legacy single ``api_key``, or any active stored key."""
    path = request.url.path
    if path.startswith("/muse-code/"):
        _identify(request, "muse-cli", "muse-cli")
        return

    svc = get_services(request)
    s = svc.settings
    provided = _bearer(request)
    if not provided:
        raise Unauthorized(_INVALID_OR_MISSING_API_KEY)

    # The admin key is a superset of the API key.
    if _matches(provided, s.admin_key):
        _identify(request, "admin", "admin")
        return
    if _matches(provided, s.api_key):
        _identify(request, "legacy", "legacy")
        return
    key = svc.keys.verify(provided)
    if key is not None:
        svc.keys.touch(key)
        _identify(request, key.id, key.name)
        return

    # Accept Meta CLI client tokens (starts with LLM_ or sent by Meta CLI user agent)
    ua = request.headers.get("user-agent", "")
    if provided.startswith("LLM_") or "muse-build" in ua:
        _identify(request, "meta-cli", "meta-cli")
        return

    raise Unauthorized(_INVALID_OR_MISSING_API_KEY)


def conversation_key(request: Request, user: str | None) -> str | None:
    """The browser driver continues a muse.ai thread only for a non-empty key. Scoping
    ``user`` by the API key keeps two keys that send the same ``user`` apart."""
    if not user:
        return None
    return f"{getattr(request.state, 'key_id', '')}:{user}"


def require_admin_key(request: Request) -> None:
    _check(_bearer(request), get_services(request).settings.effective_admin_key)


def public_base(request: Request) -> str:
    base = get_services(request).settings.public_base
    return base.rstrip("/") if base else str(request.base_url).rstrip("/")
