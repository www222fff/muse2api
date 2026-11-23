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
from typing import Any, cast

from ..errors import FeatureNotImplemented

try:
    import numpy as np
    from PIL import Image
    from pymatting import estimate_foreground_ml  # type: ignore[import-not-found]
    from rembg import new_session, remove  # type: ignore[import-not-found]
except ImportError:
    np = None  # type: ignore[assignment]
    Image = None  # type: ignore[assignment]
    estimate_foreground_ml = None  # type: ignore[assignment]
    new_session = None  # type: ignore[assignment]
    remove = None  # type: ignore[assignment]

_MATTING_EXTRA = (
    "background=transparent needs the optional matting extra: "
    "pip install 'muse2api[matting]'"
)


class Matting:
    def __init__(self, model: str) -> None:
        self.model = model
        self._session: Any = None
        # Inference is CPU-bound and already multi-threaded; run one image at a time.
        self._lock = threading.Lock()

    def remove_sync(self, data: bytes) -> bytes:
        """Return ``data`` as an RGBA PNG with the background removed."""
        if (np is None or Image is None or estimate_foreground_ml is None
                or new_session is None or remove is None):
            raise FeatureNotImplemented(_MATTING_EXTRA)

        src: Any = Image.open(io.BytesIO(data)).convert("RGB")
        with self._lock:
            new_sess = cast(Any, new_session(self.model)) if self._session is None else self._session
            self._session = new_sess
            cut = cast(Any, remove(src, session=new_sess))

        alpha = np.asarray(cut.getchannel("A"), dtype=np.float64) / 255.0
        rgb = np.asarray(src, dtype=np.float64) / 255.0
        fg = cast(Any, estimate_foreground_ml(rgb, alpha))
        rgba = np.dstack([np.clip(fg, 0.0, 1.0), alpha])
        buf = io.BytesIO()
        Image.fromarray((rgba * 255).round().astype(np.uint8), "RGBA").save(buf, "PNG")
        return buf.getvalue()

    async def remove(self, data: bytes) -> bytes:
        return await asyncio.to_thread(self.remove_sync, data)
