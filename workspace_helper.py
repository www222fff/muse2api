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
    """Look for relative paths, filenames, or module folders in text."""
    for w in re.findall(r"[a-zA-Z0-9_\-./\\]+\.[a-zA-Z0-9]+", text):
        _try_find_file_path(w, base, found, text)

    # Also search for module directory names (e.g. gap_reversal, strategies, options_trading)
    ignore_tokens = {
        "check", "explore", "figure", "inspect", "files", "folder", "directory",
        "imports", "import", "project", "workspace", "local", "access", "enabled",
        "please", "there", "their", "about", "would", "could", "should", "first",
    }
    for tok in re.findall(r"\b[a-zA-Z0-9_-]{4,}\b", text):
        if tok.lower() not in ignore_tokens:
            _try_find_dir_path(tok, base, found)


def _try_find_file_path(w: str, base: Path, found: list[Path], text: str = "") -> None:
    """Try to resolve a relative file path (and glob for deeper matches)."""
    cand = base / w
    if _try_add_path(found, cand):
        return
    if "." in w:
        matches = [m for m in base.glob(f"**/{w}") if not any(p.startswith(".") for p in m.parts)]
        if matches:
            if text and len(matches) > 1:
                t_lower = text.lower()
                matches.sort(
                    key=lambda m: -sum(1 for part in m.parts if part.lower() in t_lower)
                )
            if matches[0] not in found:
                found.append(matches[0])


def _try_find_dir_path(tok: str, base: Path, found: list[Path]) -> None:
    """Try to resolve a module directory name (and glob for deeper matches)."""
    cand = base / tok
    if cand.is_dir() and _try_add_path(found, cand):
        return
    matches = [
        m for m in base.glob(f"**/{tok}")
        if m.is_dir() and not any(p.startswith(".") for p in m.parts)
    ]
    if matches and matches[0] not in found:
        found.append(matches[0])


def _resolve_sister_imports(text: str, base: Path, found: list[Path]) -> None:
    """If inspecting a python file, resolve imported sister modules in the same package."""
    _attach_sister_modules(text, found)
    _attach_project_manifest(base, found)


def _attach_sister_modules(text: str, found: list[Path]) -> None:
    """Attach sibling modules next to any inspected Python file (when imports matter)."""
    trigger_words = (
        "import", "cross-file", "check", "fix", "explore", "detector",
        "aging", "runner", "evaluate", "fixes", "remaining", "module", "gap"
    )
    if not any(term in text.lower() for term in trigger_words):
        return
    gap_sisters = (
        "config.py", "gap_quality_filters.py", "models.py", "__init__.py",
        "aging_detector.py", "aging_execution.py", "aging_runner.py",
        "dynamic_ledger.py", "runner.py", "gap_entry_confirmation.py",
        "gap_quality_filters.py", "detector.py",
    )
    for f in list(found):
        pkg_dir = f.parent if f.is_file() else f
        for s_name in gap_sisters:
            s_path = pkg_dir / s_name
            if s_path.exists() and s_path not in found:
                found.append(s_path)


def _attach_project_manifest(base: Path, found: list[Path]) -> None:
    """Attach pyproject.toml from the repo root when present."""
    pyproj = base / "pyproject.toml"
    if pyproj.exists() and pyproj not in found:
        found.append(pyproj)


def get_git_context(base: Path, text: str) -> str:
    """Retrieve git context (log, diff, status) if git commands or history are mentioned."""
    t_lower = text.lower()
    git_triggers = (
        "commit", "commits", "git", "history", "diff", "log",
        "recent changes", "push", "branch", "last 5", "last five"
    )
    if not any(k in t_lower for k in git_triggers):
        return ""
    try:
        status_res = subprocess.run(
            ["git", "status", "--short"],
            cwd=base, capture_output=True, text=True, timeout=5.0
        )
        log_res = subprocess.run(
            ["git", "log", "-5", "--stat", "--pretty=format:Commit: %h | Date: %ad | Subject: %s"],
            cwd=base, capture_output=True, text=True, timeout=8.0
        )
        recent_diff = subprocess.run(
            ["git", "show", "--stat", "HEAD"],
            cwd=base, capture_output=True, text=True, timeout=8.0
        )
        return (
            f"\n[Local Git Context — 5 Most Recent Commits and Diff Stat]\n"
            f"Active Branch & Status:\n{status_res.stdout.strip() or 'Clean working tree'}\n\n"
            f"Recent Commits:\n{log_res.stdout.strip()}\n\n"
            f"Latest Commit Diff Stat:\n{recent_diff.stdout[:4000].strip()}\n"
        )
    except Exception as e:
        return f"\n[Git Context Fetch Error: {e}]\n"


def find_local_paths(
    text: str,
    base_dir: Path | None = None,
    history: list[dict] | None = None,
) -> list[Path]:
    """Extract existing absolute and relative paths mentioned in user text or recent history."""
    base = base_dir or get_default_workspace()
    combined_text = text
    if history:
        for msg in reversed(history[-2:]):
            combined_text += " " + str(msg.get("content", ""))

    found: list[Path] = []
    _find_absolute_paths(combined_text, found)
    _find_relative_paths(combined_text, base, found)

    # If gap_reversal is referenced or implied, locate the module directory directly
    triggers = ("gap_reversal", "aging", "remaining gap", "detector", "runner", "fixes required")
    if any(k in combined_text.lower() for k in triggers):
        gap_dir = base / "options_trading" / "strategies" / "gap_reversal"
        if gap_dir.is_dir() and gap_dir not in found:
            found.append(gap_dir)

    # Always ensure base workspace is included
    if not found or base not in found:
        found.insert(0, base)

    _resolve_sister_imports(combined_text, base, found)
    return found


def _collect_code_files(p: Path, limit: int = 50) -> list[str]:
    """Walk directory and collect relative paths of code files (capped at limit)."""
    files: list[str] = []
    allowed_exts = (".py", ".json", ".md", ".yaml", ".yml", ".toml", ".sh")
    for root, dirs, fnames in os.walk(p):
        dirs[:] = [d for d in dirs if not _is_skipped_dir(d)]
        rel_root = Path(root).relative_to(p)
        _collect_from_dir(files, fnames, rel_root, allowed_exts, limit)
        if len(files) >= limit:
            break
    return sorted(files)


def _collect_from_dir(files: list[str], fnames: list[str], rel_root: Path,
                      allowed_exts: tuple[str, ...], limit: int) -> None:
    """Collect code files from a single walked directory into ``files`` (capped)."""
    for f in fnames:
        if _is_code_file(f, allowed_exts):
            rel_path = rel_root / f if str(rel_root) != "." else Path(f)
            files.append(str(rel_path))
            if len(files) >= limit:
                break


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


def build_context_for_paths(
    paths: list[Path],
    prompt: str = "",
    max_chars: int = 35000,
) -> str:
    """Build a structured context block from local files or directories."""
    base = get_default_workspace()
    blocks = [
        "[Local Workspace Access Enabled - Connected to macOS Machine]",
        f"Connected Workspace Root: {base}",
        "Local file and repository access is active. You have full visibility into the local workspace.",
        "When providing code edits, output the full code in a ```python ... ``` code block.",
        "The user CLI supports saving your code directly to disk via `/save <filepath>`.",
        "DO NOT say you lack access or request file uploads when files or git info are provided below.",
    ]
    if prompt:
        git_ctx = get_git_context(base, prompt)
        if git_ctx:
            blocks.append(git_ctx)

    seen_files: set[Path] = set()
    for p in paths:
        _append_path_context(p, base, blocks, seen_files, max_chars)

    return "\n".join(blocks)


def _append_path_context(p: Path, base: Path, blocks: list[str],
                         seen_files: set[Path], max_chars: int) -> None:
    """Append the formatted block for one path (directory or file)."""
    if p.is_dir():
        files = _collect_code_files(p)
        _format_directory_block(p, blocks, files)
        # If p is a specific subfolder (e.g. gap_reversal), also attach the
        # actual code files inside it!
        if p != base and len(files) <= 15:
            for f_rel in files:
                _append_small_file(p / f_rel, blocks, seen_files)
    elif p.is_file() and p not in seen_files:
        seen_files.add(p)
        _format_file_block(p, max_chars, blocks)


def _append_small_file(f_p: Path, blocks: list[str], seen_files: set[Path]) -> None:
    """Attach a small code file to the context (12k char cap)."""
    if (f_p.is_file() and f_p.suffix in (".py", ".json", ".toml", ".yaml", ".md")
            and f_p not in seen_files):
        seen_files.add(f_p)
        _format_file_block(f_p, 12000, blocks)


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
            res = subprocess.run(["python", "-m", "py_compile", str(p)],
                                 capture_output=True, text=True, check=False)
            if res.returncode != 0:
                return False, f"Syntax error: {res.stderr}"
        return True, f"Saved successfully to {p}"
    except Exception as e:
        return False, str(e)
