from pathlib import Path

import pytest

from muse2api.core.models import resolve_model
from muse2api.core.prompt import flatten_messages, followup_text, message_turns
from muse2api.errors import InvalidRequest


def test_single_user_message_verbatim():
    assert flatten_messages([{"role": "user", "content": "hi"}]).text == "hi"


def test_multi_turn_has_roles_and_images():
    flat = flatten_messages([
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": [
            {"type": "text", "text": "what is this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]},
    ])
    assert flat.text.startswith("<system_instructions>\nbe brief\n</system_instructions>")
    assert "what is this" in flat.text
    assert flat.images == ["data:image/png;base64,AAAA"]


def test_followup_sends_only_new_user_text():
    first = message_turns([{"role": "user", "content": "hi"}])
    second = message_turns([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "again"},
    ])
    assert followup_text(first, second) == "again"


def test_followup_rejects_a_different_history():
    first = message_turns([{"role": "user", "content": "hi"}])
    other = message_turns([{"role": "user", "content": "a different chat"}])
    edited = message_turns([
        {"role": "user", "content": "hi, edited"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "again"},
    ])
    assert followup_text(first, other) is None
    assert followup_text(first, edited) is None
    assert followup_text(first, first) is None


def test_thread_url_ignores_the_new_thread_page():
    from muse2api.drivers.browser.driver import is_thread_url

    assert is_thread_url("https://muse.ai/thread/abc")
    assert not is_thread_url("https://muse.ai/thread/new")
    assert not is_thread_url("https://muse.ai/thread/new/")
    assert not is_thread_url("https://muse.ai/")


def test_resolve_model():
    assert resolve_model("gpt-4o", "chat").id == "muse-chat"
    assert resolve_model("unknown", "image").id == "muse-image"
    assert resolve_model(None, "video").id == "muse-video"
    with pytest.raises(InvalidRequest):
        resolve_model("muse-image", "chat")


def test_media_prune_removes_only_expired_files(tmp_path: Path):
    import os
    import time

    from muse2api.core.media import MediaStore

    store = MediaStore(tmp_path / "media")
    old_name = store.save(b"old", "image/png")
    new_name = store.save(b"new", "image/png")
    old_time = time.time() - 30 * 86400  # 30 days ago
    os.utime(store.path_of(old_name), (old_time, old_time))

    removed = store.prune(14 * 86400)
    assert removed == 1
    assert not (tmp_path / "media" / old_name).exists()
    assert (tmp_path / "media" / new_name).exists()


def test_media_prune_disabled_and_foreign_files(tmp_path: Path):
    from muse2api.core.media import MediaStore

    store = MediaStore(tmp_path / "media")
    name = store.save(b"data", "image/png")
    (tmp_path / "media" / "foreign.txt").write_text("do not touch")

    assert store.prune(0) == 0
    assert (tmp_path / "media" / name).exists()
    # prune with negative threshold never deletes
    assert store.prune(-1) == 0
    assert (tmp_path / "media" / "foreign.txt").exists()


def test_blank_browser_profile_dir_falls_back_to_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from muse2api.config import Settings

    monkeypatch.setenv("MUSE2API_BROWSER_PROFILE_DIR", "")
    settings = Settings(_env_file=None, data_dir=tmp_path)  # type: ignore[call-arg]
    assert settings.profile_dir == tmp_path / "browser-profile"
