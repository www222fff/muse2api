"""Local workspace inspector and file bridge for muse_cli."""

import os
import re
import subprocess
import time
from pathlib import Path

_REPO_INDEX: dict[str, Path] = {}
_LAST_INDEX_TIME: float = 0.0

# Constants for known module paths
LIQUIDITY_SWEEP_STRATEGY: str = "options_trading/strategies/liquidity_sweep"

KNOWN_MODULES: dict[str, str] = {
    "gap_reversal": "options_trading/strategies/gap_reversal",
    "aging_gap": "options_trading/strategies/gap_reversal",
    "liquidity_sweep": LIQUIDITY_SWEEP_STRATEGY,
    "liquidity sweep": LIQUIDITY_SWEEP_STRATEGY,
    "sw_sl": LIQUIDITY_SWEEP_STRATEGY,
    "eqh_eql": LIQUIDITY_SWEEP_STRATEGY,
    "pdh_pdl": LIQUIDITY_SWEEP_STRATEGY,
    "atm_opening_range": "options_trading/strategies/atm_opening_range",
    "hero_zero": "options_trading/strategies/hero_zero.py",
    "order_block": "options_trading/common",
    "shoonya": "options_trading/data/shoonya",
    "angelone": "options_trading/data/angelone",
    "fyers": "options_trading/data/fyers",
    "sensibull": "options_trading/data/sensibull",
}

# Constants for file extensions
EXT_PY: str = ".py"
EXT_JSON: str = ".json"
EXT_TOML: str = ".toml"
EXT_YAML: str = ".yaml"
EXT_YML: str = ".yml"
EXT_MD: str = ".md"
EXT_SH: str = ".sh"


def get_default_workspace(prompt: str = "") -> Path:
    """Return default workspace directory, resolving muse2api vs trading repo from prompt."""
    env_root = os.environ.get("MUSE_WORKSPACE_ROOT")
    if env_root and os.path.exists(env_root):
        return Path(env_root)
    if prompt and "muse2api" in prompt.lower():
        muse_dir = Path("/Users/Gaurav_Batra/muse2api")
        if muse_dir.exists():
            return muse_dir
    trading_dir = Path("/Volumes/Drive D/Indian Firm/Ai-indian-trading-firm")
    if trading_dir.exists():
        return trading_dir
    return Path(os.getcwd())


def _get_file_index(base: Path) -> dict[str, Path]:
    """In-memory cached file index of repository code files for instant O(1) resolution."""
    global _REPO_INDEX, _LAST_INDEX_TIME
    now = time.time()
    if _REPO_INDEX and (now - _LAST_INDEX_TIME < 60.0):
        return _REPO_INDEX

    idx: dict[str, Path] = {}
    skipped = {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        "graphify-out",
        "packaging",
        "starters",
        "scratch",
        "data",
        "logs",
    }
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in skipped and not d.startswith(".")]
        for f in files:
            if f.endswith((EXT_PY, EXT_JSON, EXT_TOML, EXT_YAML, EXT_YML, EXT_MD, EXT_SH)):
                idx[f.lower()] = Path(root) / f
    _REPO_INDEX = idx
    _LAST_INDEX_TIME = now
    return _REPO_INDEX


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
    """Look for relative paths, filenames, or known module folders in text."""
    idx = _get_file_index(base)
    # Check for specific files with extensions
    for w in re.findall(r"[a-zA-Z0-9_\-/\\]+\.[a-zA-Z0-9]+", text):
        cand = base / w
        if _try_add_path(found, cand):
            continue
        fname = Path(w).name.lower()
        if fname in idx:
            _try_add_path(found, idx[fname])

    # Check for known module directories by direct dictionary lookup (instant)
    text_lower = text.lower()
    for mod_key, rel_path in KNOWN_MODULES.items():
        if mod_key in text_lower:
            cand = base / rel_path
            if cand.exists() and cand not in found:
                found.append(cand)


def _resolve_sister_imports(text: str, base: Path, found: list[Path]) -> None:
    """If inspecting a python file, resolve imported sister modules in the same package."""
    _attach_sister_modules(text, base, found)
    _attach_sweep_modules(text, base, found)
    _attach_dirty_files(text, base, found)
    _attach_project_manifest(base, found)


def _has_gap_triggers(text: str) -> bool:
    """Check if text mentions gap reversal or aging gap strategies."""
    t_lower = text.lower()
    return any(k in t_lower for k in ("gap_reversal", "aging_gap", "aging_detector"))


def _get_gap_sisters() -> tuple[str, ...]:
    """Get the list of sister module names for gap strategies."""
    return (
        "config.py",
        "gap_quality_filters.py",
        "models.py",
        "__init__.py",
        "aging_detector.py",
        "aging_execution.py",
        "aging_runner.py",
        "dynamic_ledger.py",
        "runner.py",
        "gap_entry_confirmation.py",
    )


def _attach_sister_modules(text: str, base: Path, found: list[Path]) -> None:
    """Attach sibling modules next to any inspected Python file only when relevant."""
    if not _has_gap_triggers(text):
        return

    gap_sisters = _get_gap_sisters()
    for f in list(found):
        if "gap_reversal" in str(f):
            pkg_dir = f if f.is_dir() else f.parent
            for s_name in gap_sisters:
                s_path = pkg_dir / s_name
                if s_path.exists() and s_path not in found:
                    found.append(s_path)
            break


def _has_sweep_triggers(text: str) -> bool:
    """Check if text mentions liquidity sweep or related strategies."""
    t_lower = text.lower()
    sweep_triggers = (
        "liquidity_sweep",
        "sw_sl",
        "sweep",
        "eqh_eql",
        "pdh_pdl",
        "naturalgas",
        "crudeoil",
        "silverm",
        "ultra_accuracy_95",
        "re-entry",
        "sl floor",
        "sl_hit",
        "sl hit",
        "reclaim",
    )
    return any(k in t_lower for k in sweep_triggers)


def _get_sweep_core() -> tuple[str, ...]:
    """Get the core sweep module names."""
    return (
        "option_sweep_detector.py",
        "option_sweep_scanner.py",
        "config.py",
        "commodity_params.py",
        "runner.py",
        "_runner_dispatch_impl.py",
        "models.py",
    )


def _attach_sweep_modules(text: str, base: Path, found: list[Path]) -> None:
    """Attach liquidity sweep strategy modules when triggered."""
    if not _has_sweep_triggers(text):
        return
    sweep_dir = base / "options_trading" / "strategies" / "liquidity_sweep"
    if sweep_dir.is_dir():
        if sweep_dir not in found:
            found.append(sweep_dir)
        for s_name in _get_sweep_core():
            s_path = sweep_dir / s_name
            if s_path.exists() and s_path not in found:
                found.append(s_path)


def _add_dirty_file(line: str, base: Path, found: list[Path]) -> bool:
    """Append one porcelain status line's file when it is a tracked code file."""
    if len(line) < 4:
        return False
    rel_f = line[3:].strip()
    if not rel_f.endswith((".py", ".toml")):
        return False
    f_path = base / rel_f
    if f_path.is_file() and f_path not in found:
        found.append(f_path)
        return True
    return False


def _attach_dirty_files(text: str, base: Path, found: list[Path], max_count: int = 3) -> None:
    """Attach recently modified git files only when explicitly discussing patches or reviews."""
    t_lower = text.lower()
    patch_triggers = (
        "patch",
        "uncommitted",
        "dirty",
        "git diff",
        "git status",
        "review my changes",
        "review diff",
        "review modified",
    )
    if not any(k in t_lower for k in patch_triggers):
        return
    try:
        res = subprocess.run(
            ["git", "status", "--porcelain"], cwd=base, capture_output=True, text=True, timeout=2.0
        )
        if res.returncode == 0:
            count = 0
            for line in res.stdout.splitlines():
                if _add_dirty_file(line, base, found):
                    count += 1
                    if count >= max_count:
                        break
    except Exception:
        pass


def _attach_project_manifest(base: Path, found: list[Path]) -> None:
    """Attach pyproject.toml from the repo root when present."""
    pyproj = base / "pyproject.toml"
    if pyproj.exists() and pyproj not in found:
        found.append(pyproj)


def get_git_context(base: Path, text: str) -> str:
    """Retrieve git context (log, diff, status) if git commands or history are mentioned."""
    t_lower = text.lower()
    git_triggers = (
        "commit",
        "commits",
        "git log",
        "git history",
        "git diff",
        "last 5 commits",
        "last five commits",
        "recent commits",
    )
    if not any(k in t_lower for k in git_triggers):
        return ""
    try:
        status_res = subprocess.run(
            ["git", "status", "--short"], cwd=base, capture_output=True, text=True, timeout=3.0
        )
        log_res = subprocess.run(
            ["git", "log", "-5", "--stat", "--pretty=format:Commit: %h | Date: %ad | Subject: %s"],
            cwd=base,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        recent_diff = subprocess.run(
            ["git", "show", "--stat", "HEAD"], cwd=base, capture_output=True, text=True, timeout=5.0
        )
        return (
            f"\n[Local Git Context — 5 Most Recent Commits and Diff Stat]\n"
            f"Active Branch & Status:\n{status_res.stdout.strip() or 'Clean working tree'}\n\n"
            f"Recent Commits:\n{log_res.stdout.strip()}\n\n"
            f"Latest Commit Diff Stat:\n{recent_diff.stdout[:3000].strip()}\n"
        )
    except Exception as e:
        return f"\n[Git Context Fetch Error: {e}]\n"


def find_local_paths(
    text: str,
    base_dir: Path | None = None,
    history: list[dict] | None = None,
    max_files: int = 5,
    **kwargs,
) -> list[Path]:
    """Extract file paths mentioned in the active user prompt (token-capped)."""
    base = base_dir or get_default_workspace()
    found: list[Path] = [base]

    # Search paths only from the active user instruction, never assistant responses
    _find_absolute_paths(text, found)
    _find_relative_paths(text, base, found)
    _resolve_sister_imports(text, base, found)

    # Strictly cap total attached files to max_files (plus workspace root)
    if len(found) > max_files + 1:
        other_files = [p for p in found if p != base]
        found = [base] + other_files[:max_files]
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


def _collect_from_dir(
    files: list[str], fnames: list[str], rel_root: Path, allowed_exts: tuple[str, ...], limit: int
) -> None:
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
    base = get_default_workspace(prompt)
    blocks = [
        "[Autonomous Agent Mode Active — Direct Multi-Drive Host Access]",
        "Host: macOS",
        "Accessible Workspaces:",
        "- Main Drive: /Users/Gaurav_Batra/muse2api",
        "- D Drive: /Volumes/Drive D/Indian Firm/Ai-indian-trading-firm",
        f"Active Target Root: {base}",
        "You have direct write and edit access to the local codebase through the terminal bridge.",
        "When asked to fix bugs, refactor, implement features, or edit files:",
        "1. Always specify the relative target file path in your code block header or before it:",
        "   ```python:path/to/file.py",
        "   <full corrected file content or replacement>",
        "   ```",
        "   Or use: `### File: path/to/file.py` immediately followed by the code block.",
        "2. The bridge automatically writes your code directly to disk upon completion.",
        "   The user will immediately see the changes in their Git Source Control / Changes tab.",
        "3. NEVER say 'I cannot write directly to your Mac' or tell the "
        "user to manually run '/save'.",
        "   Confirm that the file has been edited on disk for the user to "
        "review in Source Control.",
        "4. CRITICAL REPOSITORY CODING RULES:",
        "   - STRICTLY PROHIBITED: `from __future__ import annotations` "
        "(deprecated in Python 3.14+).",
        "   - Hard cap: <= 250 lines per .py file.",
        "   - Zero floating-point equality (== or != against floats). "
        "Use thresholds or math.isclose.",
        "   - Flake8 compliance: max line length 100 characters.",
    ]
    if prompt:
        git_ctx = get_git_context(base, prompt)
        if git_ctx:
            blocks.append(git_ctx)

    seen_files: set[Path] = set()
    current_chars = sum(len(b) for b in blocks)
    for p in paths:
        if current_chars >= max_chars:
            break
        file_cap = min(10000, max_chars - current_chars)
        _append_path_context(p, base, blocks, seen_files, file_cap)
        current_chars = sum(len(b) for b in blocks)

    return "\n".join(blocks)


def _append_path_context(
    p: Path, base: Path, blocks: list[str], seen_files: set[Path], max_chars: int
) -> None:
    """Append the formatted block for one path (directory or file)."""
    if p.is_dir():
        files = _collect_code_files(p)
        _format_directory_block(p, blocks, files)
    elif p.is_file() and p not in seen_files:
        seen_files.add(p)
        _format_file_block(p, max_chars, blocks)


def _append_small_file(f_p: Path, blocks: list[str], seen_files: set[Path]) -> None:
    """Attach a small code file to the context (12k char cap)."""
    if (
        f_p.is_file()
        and f_p.suffix in (".py", ".json", ".toml", ".yaml", ".md")
        and f_p not in seen_files
    ):
        seen_files.add(f_p)
        _format_file_block(f_p, 12000, blocks)


def extract_code_block(text: str) -> str | None:
    """Extract code from python markdown block in text."""
    m = re.search(r"```(?:python|py)?[^\n]*\n(.*?)```", text, re.DOTALL)
    return m.group(1).strip() if m else None


# Edit-extraction patterns (kept at module scope for testability).
_RE_EDIT_BLOCK = re.compile(
    r"```(?:python|py)?[:\s]+([a-zA-Z0-9_\-./\\]+\.[a-zA-Z0-9_]+)[^\n]*\n(.*?)```",
    re.DOTALL,
)
_RE_EDIT_FILE = re.compile(
    r"(?:###?\s*File:\s*|File:\s*|\*\*File\*\*:\s*)(`?[a-zA-Z0-9_\-./\\]+\.[a-zA-Z0-9_]+`?)"
    r"[^\n]*\n+```(?:[a-zA-Z0-9_\-]+)?\n(.*?)```",
    re.DOTALL,
)
_RE_EDIT_SAVE = re.compile(r"(?:Save with:\s*)?/save\s+(`?[a-zA-Z0-9_\-./\\]+\.[a-zA-Z0-9_]+`?)")
_RE_EDIT_PATH = re.compile(r"([a-zA-Z0-9_\-./\\]+\.(?:py|json|toml|yaml|md))")
_EDIT_INTENT_KEYWORDS = (
    "fix",
    "edit",
    "save",
    "update",
    "patch",
    "refactor",
    "rewrite",
    "correct",
    "replace",
    "add",
    "implement",
    "todo",
)


def _collect_simple_matches(
    regex, reply: str, seen_targets: set[str], *, strip_ticks: bool = False
) -> list[tuple[str, str]]:
    """Collect non-duplicate (target, code) matches from a regex over reply."""
    out: list[tuple[str, str]] = []
    for m in regex.finditer(reply):
        tgt = m.group(1).strip()
        if strip_ticks:
            tgt = tgt.strip("`*")
        code = m.group(2).strip()
        if tgt and code and tgt not in seen_targets:
            seen_targets.add(tgt)
            out.append((tgt, code))
    return out


def _resolve_fallback_target(prompt: str, reply: str, paths: list[Path] | None) -> str | None:
    """Resolve the single target file for the fallback extraction pattern."""
    py_matches = _RE_EDIT_PATH.findall(f"{prompt} {reply}")
    valid_py = [f for f in py_matches if _is_editable_py_path(f)]
    if valid_py:
        return valid_py[0]
    if paths:
        file_paths = [p for p in paths if p.is_file()]
        if len(file_paths) == 1:
            return str(file_paths[0])
    return None


def _is_editable_py_path(path: str) -> bool:
    """A path that names a real file (not a language tag)."""
    return not path.startswith("python") and ("/" in path or path.endswith(".py"))


def _collect_save_match(reply: str, seen_targets: set[str]) -> list[tuple[str, str]]:
    """Edits from a ``/save <path>`` marker plus the following code block."""
    save_match = _RE_EDIT_SAVE.search(reply)
    if not save_match:
        return []
    tgt = save_match.group(1).strip().strip("`*")
    code = extract_code_block(reply)
    if tgt and code and tgt not in seen_targets:
        seen_targets.add(tgt)
        return [(tgt, code)]
    return []


def _has_edit_intent(prompt: str) -> bool:
    lowered = prompt.lower()
    return any(word in lowered for word in _EDIT_INTENT_KEYWORDS)


def _collect_fallback_edit(reply: str, prompt: str, paths: list[Path] | None,
                           seen_targets: set[str]) -> list[tuple[str, str]]:
    """One edit when a single target file was requested and a code block exists."""
    code = extract_code_block(reply)
    if not code or len(code.splitlines()) < 2:
        return []
    target_file = _resolve_fallback_target(prompt, reply, paths)
    if target_file and target_file not in seen_targets and _has_edit_intent(prompt):
        seen_targets.add(target_file)
        return [(target_file, code)]
    return []


def extract_file_edits(
    reply: str,
    prompt: str = "",
    paths: list[Path] | None = None,
) -> list[tuple[str, str]]:
    """Extract (file_path, code) pairs from assistant reply."""
    edits: list[tuple[str, str]] = []
    seen_targets: set[str] = set()

    # Pattern 1: ```python:path/to/file.ext or ```python path/to/file.ext
    edits += _collect_simple_matches(_RE_EDIT_BLOCK, reply, seen_targets)

    # Pattern 2: ### File: path/to/file.ext or File: path/to/file.ext before code block
    edits += _collect_simple_matches(_RE_EDIT_FILE, reply, seen_targets, strip_ticks=True)

    # Pattern 3: Save with: /save path/to/file.ext or /save path/to/file.ext in text
    edits += _collect_save_match(reply, seen_targets)

    # Pattern 4: Fallback if single target file was requested in prompt or passed in paths
    if not edits:
        edits += _collect_fallback_edit(reply, prompt, paths, seen_targets)

    return edits


def save_code_to_file(path_str: str, code: str) -> tuple[bool, str]:
    """Write code to file, strip prohibited annotations, and verify with py_compile."""
    try:
        p = Path(path_str.strip())
        if not p.is_absolute():
            p = get_default_workspace() / p

        if p.suffix == ".py":
            # Repository rule: NO from __future__ import annotations
            code = re.sub(
                r"^[ \t]*from\s+__future__\s+import\s+annotations[^\n]*\n?",
                "",
                code,
                flags=re.MULTILINE,
            )

        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(code, encoding="utf-8")
        if p.suffix == ".py":
            res = subprocess.run(
                ["python", "-m", "py_compile", str(p)],
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode != 0:
                return False, f"Syntax error in {p.name}: {res.stderr}"
        return True, f"Saved successfully to {p}"
    except Exception as e:
        return False, str(e)
