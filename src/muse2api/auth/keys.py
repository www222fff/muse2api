"""Client API keys, persisted to ``data/keys.json``.

Only a SHA-256 hash and a short display prefix are stored; the plaintext key is
returned once, when it is created. ``last_used_at`` is kept in memory and
written out by ``flush()`` so that authenticating a request never touches disk.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
import time
import uuid
from pathlib import Path

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

KEY_PREFIX = "m2a-"
_DISPLAY_LEN = 8


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def generate_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(24)


class ApiKey(BaseModel):
    id: str = Field(default_factory=lambda: "key_" + uuid.uuid4().hex[:10])
    name: str
    prefix: str
    hash: str
    created_at: float = Field(default_factory=time.time)
    last_used_at: float = 0.0
    revoked: bool = False
    note: str = ""

    def public(self) -> dict:
        """Serialisable view without the hash."""
        return self.model_dump(mode="json", exclude={"hash"})


class KeyStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._keys: dict[str, ApiKey] = {}
        self._lock = asyncio.Lock()
        self._dirty = False

    async def load(self) -> None:
        if not self.path.is_file():
            return
        raw = await asyncio.to_thread(self.path.read_text, encoding="utf-8")
        if raw.strip():
            self._keys = {k.id: k for k in (ApiKey.model_validate(i) for i in json.loads(raw))}

    def all(self) -> list[ApiKey]:
        return sorted(self._keys.values(), key=lambda k: k.created_at)

    def get(self, key_id: str) -> ApiKey | None:
        return self._keys.get(key_id)

    def verify(self, provided: str) -> ApiKey | None:
        """Return the active key matching ``provided``, comparing hashes in constant time."""
        if not provided:
            return None
        digest = hash_key(provided)
        found = None
        # No early exit, so timing does not reveal which (or whether a) key matched.
        for key in self._keys.values():
            if hmac.compare_digest(digest, key.hash) and not key.revoked:
                found = key
        return found

    def touch(self, key: ApiKey) -> None:
        now = time.time()
        # Second resolution is plenty for a "last used" column.
        if now - key.last_used_at >= 1:
            key.last_used_at = now
            self._dirty = True

    async def create(self, name: str, note: str = "") -> tuple[ApiKey, str]:
        plaintext = generate_key()
        key = ApiKey(name=name, note=note, prefix=plaintext[:_DISPLAY_LEN],
                     hash=hash_key(plaintext))
        self._keys[key.id] = key
        await self.save()
        return key, plaintext

    async def remove(self, key_id: str) -> bool:
        if self._keys.pop(key_id, None) is None:
            return False
        await self.save()
        return True

    async def save(self) -> None:
        self._dirty = False
        payload = json.dumps([k.model_dump(mode="json") for k in self.all()],
                             ensure_ascii=False, indent=2)
        async with self._lock:
            await asyncio.to_thread(self._atomic_write, payload)

    async def flush(self) -> None:
        """Persist pending ``last_used_at`` updates, if any."""
        if self._dirty:
            try:
                await self.save()
            except OSError:
                self._dirty = True
                log.exception("failed to write %s", self.path)

    def _atomic_write(self, payload: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".keys.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
