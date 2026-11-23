"""Local media storage for generated images/videos and helpers for image inputs."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import mimetypes
import re
import socket
import time
import urllib.parse
import uuid
from pathlib import Path

import httpx

from ..errors import InvalidRequest, NotFound

DEFAULT_IMAGE_MIME = "image/png"
DEFAULT_IMAGE_TIMEOUT = 20.0

_EXT_BY_MIME = {
    DEFAULT_IMAGE_MIME: ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
}
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_\-]+\.[A-Za-z0-9]+$")


class MediaStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, data: bytes, mime: str, prefix: str = "m") -> str:
        ext = _EXT_BY_MIME.get(mime.split(";", maxsplit=1)[0].strip().lower(), ".bin")
        name = f"{prefix}_{uuid.uuid4().hex[:16]}{ext}"
        (self.root / name).write_bytes(data)
        return name

    def path_of(self, name: str) -> Path:
        if not _SAFE_NAME.match(name):
            message = "media not found"
            raise NotFound(message)
        p = self.root / name
        if not p.is_file():
            message = "media not found"
            raise NotFound(message)
        return p

    def prune(self, max_age_seconds: float) -> int:
        """Delete stored media files older than ``max_age_seconds``.

        Only removes files whose names match the store's safe pattern, so a
        foreign file dropped into the directory is never touched. Returns the
        number of deleted files.
        """
        if max_age_seconds <= 0:
            return 0
        cutoff = time.time() - max_age_seconds
        removed = 0
        for child in self.root.iterdir():
            if (
                child.is_file()
                and _SAFE_NAME.match(child.name)
                and child.stat().st_mtime < cutoff
            ):
                child.unlink()
                removed += 1
        return removed

    @staticmethod
    def mime_of(name: str) -> str:
        return mimetypes.guess_type(name)[0] or "application/octet-stream"


_MAX_FETCH_BYTES = 20 * 1024 * 1024  # 20 MiB cap on server-side image fetches
_MAX_REDIRECTS = 5


def _assert_public_url(url: str) -> str:
    """Reject non-http(s) URLs and hosts resolving to non-public IPs (SSRF guard)."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        message = "image_url must be a public http(s) URL"
        raise InvalidRequest(message)
    return url


def _is_private_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for any non-public address class (SSRF guard)."""
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def _assert_public_host(hostname: str) -> None:
    """Reject hostnames resolving to non-public IPs (SSRF guard)."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            hostname, None, type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        message = "image_url host does not resolve"
        raise InvalidRequest(message) from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.version == 6 and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if _is_private_ip(ip):
            message = "image_url must resolve to a public IP address"
            raise InvalidRequest(message)


async def _fetch_public_image(url: str) -> tuple[bytes, str]:
    """Fetch an image URL with manual redirect validation and a size cap."""
    url = _assert_public_url(url)
    await _assert_public_host(urllib.parse.urlparse(url).hostname or "")
    async with httpx.AsyncClient(timeout=DEFAULT_IMAGE_TIMEOUT, follow_redirects=False) as client:
        for _ in range(_MAX_REDIRECTS + 1):
            async with client.stream("GET", url) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location")
                    if not location:
                        message = "image_url redirect missing location"
                        raise InvalidRequest(message)
                    url = _assert_public_url(urllib.parse.urljoin(url, location))
                    await _assert_public_host(urllib.parse.urlparse(url).hostname or "")
                    continue
                resp.raise_for_status()
                mime = resp.headers.get("content-type", DEFAULT_IMAGE_MIME).split(";")[0]
                body = bytearray()
                async for chunk in resp.aiter_bytes(65536):
                    body.extend(chunk)
                    if len(body) > _MAX_FETCH_BYTES:
                        message = "image_url response exceeds 20 MiB"
                        raise InvalidRequest(message)
                return bytes(body), mime
        message = "image_url exceeded redirect limit"
        raise InvalidRequest(message)


async def load_image_ref(ref: str) -> tuple[bytes, str]:
    """Decode a data URL or download an http(s) URL. Returns ``(bytes, mime)``."""
    ref = ref.strip()
    if ref.startswith("data:"):
        header, _, payload = ref.partition(",")
        mime = header[5:].split(";")[0] or DEFAULT_IMAGE_MIME
        try:
            return base64.b64decode(payload), mime
        except ValueError as exc:
            message = "invalid base64 image data"
            raise InvalidRequest(message) from exc
    if ref.startswith(("http://", "https://")):
        return await _fetch_public_image(ref)
    try:
        return base64.b64decode(ref, validate=True), DEFAULT_IMAGE_MIME
    except ValueError as exc:
        message = "image must be a data URL, http(s) URL or base64 string"
        raise InvalidRequest(message) from exc
