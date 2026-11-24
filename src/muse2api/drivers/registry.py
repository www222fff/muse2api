from __future__ import annotations

from ..config import Settings
from .base import MuseDriver


def create_driver(settings: Settings) -> MuseDriver:
    """Instantiate the driver selected by ``MUSE2API_DRIVER``. Imports are lazy so that
    optional heavy dependencies of one driver never affect the others."""
    if settings.driver == "mock":
        from .mock import MockDriver  # noqa: PLC0415 - lazy: avoid optional deps

        return MockDriver()
    if settings.driver == "browser":
        from .browser.driver import BrowserDriver  # noqa: PLC0415 - lazy: avoid optional deps

        return BrowserDriver(settings)
    if settings.driver == "http":
        from .http.driver import HttpDriver  # noqa: PLC0415 - lazy: avoid optional deps

        return HttpDriver(settings)
    message = f"unknown driver: {settings.driver}"
    raise ValueError(message)
