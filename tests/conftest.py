from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from muse2api.app import create_app
from muse2api.config import Settings
from muse2api.drivers.mock import MockDriver

API_KEY = "test-key"
ADMIN_KEY = "admin-key"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        data_dir=tmp_path / "data",
        api_key=API_KEY,
        admin_key=ADMIN_KEY,
        driver="mock",
    )


@pytest.fixture
def client(settings):
    app = create_app(settings, MockDriver(delay=0))
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth() -> dict:
    return {"Authorization": f"Bearer {API_KEY}"}


@pytest.fixture
def admin() -> dict:
    return {"Authorization": f"Bearer {ADMIN_KEY}"}
