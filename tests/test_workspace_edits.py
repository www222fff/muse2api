"""Unit tests for workspace code extraction, auto-save, and repository rules."""

from __future__ import annotations

from pathlib import Path

import pytest
from workspace_helper import extract_file_edits, save_code_to_file


def test_extract_file_edits_block_header():
    reply = """
Here is the fix:
```python:options_trading/strategies/gap_reversal/aging_runner.py
# aging_runner updated
x = 10
```
"""
    edits = extract_file_edits(reply)
    assert len(edits) == 1
    assert edits[0][0] == "options_trading/strategies/gap_reversal/aging_runner.py"
    assert "x = 10" in edits[0][1]


def test_extract_file_edits_file_header():
    reply = """
### File: options_trading/strategies/gap_reversal/aging_runner.py
```python
# new runner
y = 20
```
"""
    edits = extract_file_edits(reply)
    assert len(edits) == 1
    assert edits[0][0] == "options_trading/strategies/gap_reversal/aging_runner.py"
    assert "y = 20" in edits[0][1]


def test_extract_file_edits_save_instruction():
    reply = """
Save with: /save options_trading/strategies/gap_reversal/aging_runner.py
```python
# saved runner
z = 30
```
"""
    edits = extract_file_edits(reply)
    assert len(edits) == 1
    assert edits[0][0] == "options_trading/strategies/gap_reversal/aging_runner.py"
    assert "z = 30" in edits[0][1]


def test_extract_file_edits_fallback_from_prompt():
    reply = """
Here is the corrected code:
```python
def foo():
    return 42
```
"""
    edits = extract_file_edits(
        reply,
        prompt="please fix options_trading/strategies/gap_reversal/aging_runner.py",
    )
    assert len(edits) == 1
    assert "aging_runner.py" in edits[0][0]


def test_save_code_strips_prohibited_future_import(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("workspace_helper.get_default_workspace", lambda: tmp_path)
    code = (
        '"""Docstring."""\n'
        'from __future__ import annotations\n'
        '\n'
        'import logging\n'
        'A: int = 1\n'
    )
    ok, msg = save_code_to_file("sample.py", code)
    assert ok, msg
    saved = (tmp_path / "sample.py").read_text(encoding="utf-8")
    assert "from __future__ import annotations" not in saved
    assert "import logging" in saved
    assert "A: int = 1" in saved
