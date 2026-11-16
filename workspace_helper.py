"""Local workspace inspector and file bridge for muse_cli."""

import os
import re
import subprocess
from pathlib import Path


def get_default_workspace() -> Path:
    """Return default workspace directory."""
    env_root = os.environ.get("MUSE_WORKSPACE_ROOT")
    if env_root and os.path.exists(env_root):
        return Path(env_root)
    cwd = Path(os.getcwd())
    trading_dir = Path("/Volumes/Drive D/Indian Firm/Ai-indian-trading-firm")
    return trading_dir if trading_dir.exists() else cwd


def _try_add_path(found: list[Path], cand: Path) -> bool:
    """Add candidate path to found list if it exists and is not already present."""
    if cand.exists() and cand not in found:
        found.append(cand)
        return True
    return False


def _find_absolute_paths(text: str, found: list[Path]) -> None:
    """Look for absolute paths (/Volumes/..., /Users/..., etc.) in text."""
    for m in re.finditer(r"(/(?:Volumes|Users|private|tmp)[^\n,;\"\'\)]+)", text):
        raw = m.group(1).strip()
        parts = raw.split(" ")
        for i in range(len(parts), 0, -1):
            cand = Path(" ".join(parts[:i]))
            if _try_add_path(found, cand):
                break


def _find_relative_paths(text: str, base: Path, found: list[Path]) -> None:
    """Look for relative paths or filenames in text."""
    words = re.findall(r"[a-zA-Z0-9_\-./\\]+\.[a-zA-Z0-9]+", text)
    for w in words:
        cand = base / w
        if _try_add_path(found, cand):
            continue
        if "." in w:
            matches = list(base.glob(f"**/{w}"))
            if matches and matches[0] not in found:
                found.append(matches[0])


def find_local_paths(text: str, base_dir: Path | None = None) -> list[Path]:
    """Extract existing absolute and relative paths mentioned in user text."""
    base = base_dir or get_default_workspace()
    found: list[Path] = []
    _find_absolute_paths(text, found)
    _find_relative_paths(text, base, found)
    keywords = ("local file", "local repo", "workspace", "repo", "trading-firm", "codebase", "make edit")
    if not found and any(k in text.lower() for k in keywords):
        found.append(base)
    return found


def _collect_code_files(p: Path, limit: int = 50) -> list[str]:
    """Walk directory and collect relative paths of code files (capped at limit)."""
    files: list[str] = []
    allowed_exts = (".py", ".json", ".md", ".yaml", ".yml", ".toml", ".sh")
    for root, dirs, fnames in os.walk(p):
        dirs[:] = [d for d in dirs if not _is_skipped_dir(d)]
        rel_root = Path(root).relative_to(p)
        for f in fnames:
            if _is_code_file(f, allowed_exts):
                rel_path = rel_root / f if str(rel_root) != "." else Path(f)
                files.append(str(rel_path))
                if len(files) >= limit:
                    break
        if len(files) >= limit:
            break
    return sorted(files)


def _is_skipped_dir(d: str) -> bool:
    """Return True if directory should be skipped during walk."""
    return d.startswith((".", "__pycache__", "venv", ".venv"))


def _is_code_file(f: str, allowed_exts: tuple[str, ...]) -> bool:
    """Return True if file should be included (not hidden, has code extension)."""
    return not f.startswith(".") and f.endswith(allowed_exts)


def _format_directory_block(p: Path, blocks: list[str], files: list[str]) -> None:
    """Append directory listing block to blocks."""
    blocks.extend(
        [
            f"\n[Verified Local Directory: {p}]",
            f"Contains {len(files)} visible code files:",
        ]
    )
    for f in files[:35]:
        blocks.append(f"  - {f}")
    if len(files) > 35:
        blocks.append(f"  ... and {len(files) - 35} more files.")


def _format_file_block(p: Path, max_chars: int, blocks: list[str]) -> None:
    """Append file content block to blocks."""
    try:
        content = p.read_text(encoding="utf-8", errors="replace")
        if len(content) > max_chars:
            content = content[:max_chars] + f"\n... [Truncated at {max_chars} characters]"
        blocks.extend(
            [
                f"\n--- LOCAL FILE: {p} ---",
                content,
                f"--- END FILE: {p} ---\n",
            ]
        )
    except Exception as e:
        blocks.append(f"\n[Error reading {p}: {e}]")


def build_context_for_paths(paths: list[Path], max_chars: int = 35000) -> str:
    """Build a structured context block from local files or directories."""
    if not paths:
        return ""

    blocks = [
        "[Local Workspace Access Enabled - Connected to macOS Machine]",
        "Local file access is active. You can inspect local files and provide code updates.",
        "When providing code edits, output the full code in a ```python ... ``` code block.",
        "The user CLI supports saving your code directly to disk via `/save <filepath>`.",
    ]
    for p in paths:
        if p.is_dir():
            files = _collect_code_files(p)
            _format_directory_block(p, blocks, files)
        elif p.is_file():
            _format_file_block(p, max_chars, blocks)

    return "\n".join(blocks)


def extract_code_block(text: str) -> str | None:
    """Extract code from python markdown block in text."""
    m = re.search(r"```(?:python)?\n(.*?)```", text, re.DOTALL)
    return m.group(1).strip() if m else None


def save_code_to_file(path_str: str, code: str) -> tuple[bool, str]:
    """Write code to file and verify with py_compile."""
    try:
        p = Path(path_str.strip())
        if not p.is_absolute():
            p = get_default_workspace() / p
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(code, encoding="utf-8")
        if p.suffix == ".py":
            res = subprocess.run(["python", "-m", "py_compile", str(p)], capture_output=True, text=True)
            if res.returncode != 0:
                return False, f"Syntax error: {res.stderr}"
        return True, f"Saved successfully to {p}"
    except Exception as e:
        return False, str(e)
