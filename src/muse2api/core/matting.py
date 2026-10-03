"""Background removal for ``background="transparent"`` image requests.

muse.ai only returns flattened RGB images, so transparency is produced locally
after generation: BiRefNet (via rembg) predicts a soft alpha mask, then the edge
colours are re-estimated (pymatting) so semi-transparent fur or hair does not
carry a tint of the old background.

Optional dependency: ``pip install 'muse2api[matting]'``. The model is loaded on
first use and cached in ``~/.u2net`` (downloaded automatically if missing).
"""

from __future__ import annotations

import asyncio
import io
import threading

from ..errors import FeatureNotImplemented


class Matting:
    def __init__(self, model: str) -> None:
        self.model = model
        self._session = None
        # Inference is CPU-bound and already multi-threaded; run one image at a time.
        self._lock = threading.Lock()

    def remove_sync(self, data: bytes) -> bytes:
        """Return ``data`` as an RGBA PNG with the background removed."""
        try:
            import numpy as np
            from PIL import Image
            from pymatting import estimate_foreground_ml
            from rembg import new_session, remove
        except ImportError as exc:
            raise FeatureNotImplemented(
                "background=transparent needs the optional matting extra: "
                "pip install 'muse2api[matting]'"
            ) from exc

        src = Image.open(io.BytesIO(data)).convert("RGB")
        with self._lock:
            if self._session is None:
                self._session = new_session(self.model)
            cut = remove(src, session=self._session)

        alpha = np.asarray(cut.getchannel("A"), dtype=np.float64) / 255.0
        rgb = np.asarray(src, dtype=np.float64) / 255.0
        fg = estimate_foreground_ml(rgb, alpha)
        rgba = np.dstack([np.clip(fg, 0.0, 1.0), alpha])
        buf = io.BytesIO()
        Image.fromarray((rgba * 255).round().astype(np.uint8), "RGBA").save(buf, "PNG")
        return buf.getvalue()

    async def remove(self, data: bytes) -> bytes:
        return await asyncio.to_thread(self.remove_sync, data)
