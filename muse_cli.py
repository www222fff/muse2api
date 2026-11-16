#!/usr/bin/env python3
"""Interactive and command-line CLI for muse.ai (via muse2api gateway).

Features:
- Rich ANSI visual styling: distinct Question & Muse response containers
- Native GitHub and Git integration via `/git <cmd>`
- Automatic local workspace and multi-file code inspection
- Streaming chat responses with live markdown rendering
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
from openai import OpenAI
from workspace_helper import (
    build_context_for_paths,
    extract_code_block,
    find_local_paths,
    get_default_workspace,
    save_code_to_file,
)

BASE_URL = os.environ.get("MUSE2API_URL", "http://127.0.0.1:18610/v1")
GATEWAY_BASE = BASE_URL.rstrip("/v1").rstrip("/")
API_KEY = os.environ.get("MUSE2API_API_KEY", "sk-muse2api-local-secret")
ADMIN_KEY = os.environ.get("MUSE2API_ADMIN_KEY", "sk-muse2api-admin-secret")
DEFAULT_MODEL = os.environ.get("MUSE2API_DEFAULT_MODEL", "muse-spark-1.3")

client = OpenAI(base_url=BASE_URL, api_key=API_KEY)

# ANSI Styling Palette
RST = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
ITALIC = "\033[3m"
UNDER = "\033[4m"

C_CYAN = "\033[38;5;51m"
C_BLUE = "\033[38;5;39m"
C_GREEN = "\033[38;5;48m"
C_YELLOW = "\033[38;5;220m"
C_MAGENTA = "\033[38;5;207m"
C_PURPLE = "\033[38;5;141m"
C_RED = "\033[38;5;203m"
C_GRAY = "\033[38;5;244m"
C_WHITE = "\033[38;5;255m"


def _term_width() -> int:
    return min(shutil.get_terminal_size((80, 24)).columns, 100)


def print_user_box(prompt: str, paths: list[Path] | None = None) -> None:
    """Print an eye-catching, structured box for the user's prompt."""
    w = _term_width()
    div = "─" * max(10, w - 18)
    print(f"\n{BOLD}{C_CYAN}┌── ❯ QUESTION {div}{RST}")
    for line in prompt.splitlines():
        print(f"{BOLD}{C_CYAN}│{RST} {C_WHITE}{BOLD}{line}{RST}")
    print(f"{BOLD}{C_CYAN}└──{div}──{RST}")
    if paths:
        for p in paths:
            print(f"  {C_YELLOW}↳ ⚡ [Local Workspace Context: {p}]{RST}")


def print_assistant_header(model: str) -> None:
    """Print styled header for Muse's response."""
    w = _term_width()
    div = "─" * max(10, w - 24)
    print(f"\n{BOLD}{C_PURPLE}╭── ◆ MUSE ({model}) {div}{RST}")


def print_assistant_footer() -> None:
    """Print closing border for Muse's response."""
    w = _term_width()
    div = "─" * max(10, w - 4)
    print(f"{BOLD}{C_PURPLE}╰{div}{RST}\n")


def get_git_info(ws: Path) -> tuple[str, str]:
    """Retrieve remote URL and current branch if git is initialized."""
    try:
        remote = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=ws, capture_output=True, text=True, timeout=2.0
        ).stdout.strip()
        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=ws, capture_output=True, text=True, timeout=2.0
        ).stdout.strip()
        return remote or "No remote configured", branch or "main"
    except Exception:
        return "Not a git repository", ""


def stream_chat(
    prompt: str,
    model: str = DEFAULT_MODEL,
    history: list | None = None,
) -> tuple[str, str]:
    """Stream chat response with live output inside assistant container."""
    paths = find_local_paths(prompt)
    if paths:
        ctx = build_context_for_paths(paths)
        print_user_box(prompt, paths)
        actual_prompt = f"{ctx}\n\n[User Instruction]: {prompt}"
    else:
        print_user_box(prompt)
        actual_prompt = prompt

    messages = list(history or [])
    messages.append({"role": "user", "content": actual_prompt})

    print_assistant_header(model)
    full_text: list[str] = []
    try:
        stream = client.chat.completions.create(
            model=model,
            messages=messages,
            stream=True,
            timeout=180.0,
        )
        for chunk in stream:
            delta = chunk.choices[0].delta.content or ""
            full_text.append(delta)
            sys.stdout.write(delta)
            sys.stdout.flush()
        print()
    except Exception as e:
        print(f"\n{C_RED}[Error]: {e}{RST}")
    finally:
        print_assistant_footer()

    return "".join(full_text), actual_prompt


def display_models(current_model: str = ""):
    """Display available models with rich formatting."""
    w = _term_width()
    print("\n" + f"{BOLD}{C_BLUE}" + "═" * w + f"{RST}")
    print(f"  {BOLD}{C_WHITE}Available Models (muse.ai Web Gateway - 4B Free Tokens){RST}")
    print(f"{BOLD}{C_BLUE}" + "═" * w + f"{RST}")
    try:
        items = client.models.list().data
    except Exception:
        items = []

    for m in items:
        is_active = m.id == current_model
        star = f"{BOLD}{C_GREEN} ➜ {RST}" if is_active else "   "
        act = f" {BOLD}{C_GREEN}(Active){RST}" if is_active else ""
        kind = getattr(m, "kind", "chat")
        print(f"{star}{C_CYAN}{m.id:<30}{RST} {C_GRAY}[{kind}]{RST}{act}")

    print(f"{C_GRAY}" + "─" * w + f"{RST}")
    print(f" {C_YELLOW}• Switch in chat:{RST}  {BOLD}/model <model_id>{RST}")
    print(f" {C_YELLOW}• Switch from CLI:{RST} {BOLD}muse -m <model_id> [prompt]{RST}\n")


def _handle_git(prompt: str) -> None:
    """Execute git commands in the active workspace and format output."""
    ws = get_default_workspace()
    cmd_args = prompt.strip().split()
    args = cmd_args[1:] if len(cmd_args) > 1 else ["status"]

    print(f"\n{BOLD}{C_BLUE}┌── ⚡ GIT EXECUTION: git {' '.join(args)} ──{RST}")
    print(f"{C_GRAY}│ Working directory: {ws}{RST}")
    try:
        res = subprocess.run(
            ["git", *args],
            cwd=ws,
            capture_output=True,
            text=True,
            timeout=30.0,
        )
        if res.stdout:
            for line in res.stdout.rstrip().splitlines():
                print(f"{C_WHITE}{line}{RST}")
        if res.stderr:
            for line in res.stderr.rstrip().splitlines():
                print(f"{C_YELLOW}{line}{RST}")
        status_color = C_GREEN if res.returncode == 0 else C_RED
        print(f"{BOLD}{C_BLUE}└── {status_color}[Exit code: {res.returncode}]{RST}\n")
    except Exception as e:
        print(f"{C_RED}│ Error running git: {e}{RST}")
        print(f"{BOLD}{C_BLUE}└────────────────────────────────────────────────{RST}\n")


def _handle_clear() -> tuple[list, str]:
    """Clear conversation history; return new history and empty last_reply."""
    ws = get_default_workspace()
    print(f"\n{C_GREEN}✓ Local Workspace Access Active:{RST} {ws}")
    print(f"{C_YELLOW}✓ Conversation history cleared.{RST}\n")
    return [], ""


def _handle_model_switch(prompt: str) -> str:
    """Switch active model from /model <id> command; return new model."""
    parts = prompt.split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip():
        new_m = parts[1].strip()
        print(f"\n{C_GREEN}✓ Active model switched to: {BOLD}{new_m}{RST}\n")
        return new_m
    return DEFAULT_MODEL


def _handle_help() -> None:
    """Display styled command help."""
    print(f"\n{BOLD}{C_WHITE}Muse Interactive Commands:{RST}")
    commands = [
        ("/git <command>", "Run git operations directly (e.g. /git status, /git diff)"),
        ("/open <path>", "Open and attach local file or folder to conversation context"),
        ("/save <path>", "Save code block from last reply directly to file"),
        ("/model <id>", "View available models or switch active model"),
        ("/cd <dir>", "Switch active workspace root directory"),
        ("/status", "Check gateway health and token quota"),
        ("clear", "Reset conversation context"),
        ("exit", "Exit session"),
    ]
    for cmd, desc in commands:
        print(f"  {BOLD}{C_CYAN}{cmd:<18}{RST} {C_GRAY}➜{RST} {desc}")
    print()


def _handle_save(prompt: str, last_reply: str) -> None:
    """Save code block from last reply to specified or default path."""
    code = extract_code_block(last_reply)
    if not code:
        print(f"\n{C_RED}✗ No code block found in last reply to save.{RST}\n")
        return
    parts = prompt.split(maxsplit=1)
    target = parts[1].strip() if len(parts) > 1 else "output.py"
    ok, msg = save_code_to_file(target, code)
    if ok:
        print(f"\n{C_GREEN}✓ {msg}{RST}\n")
    else:
        print(f"\n{C_RED}✗ {msg}{RST}\n")


def _handle_open_cmd(
    prompt: str,
    model: str,
    history: list,
    last_reply: str,
) -> tuple[str, list, str, str]:
    """Attach a local path and feed an inspection prompt to the model."""
    target = prompt.split(maxsplit=1)[1].strip()
    p = Path(target)
    if not p.is_absolute():
        p = get_default_workspace() / p
    if not p.exists():
        print(f"\n{C_RED}✗ Path not found: {target}{RST}\n")
        return "continue", history, last_reply, model
    sub_prompt = f"Please inspect the attached local code and analyze it:\n{p}"
    reply, actual_prompt = stream_chat(sub_prompt, model=model, history=history)
    if reply:
        history.append({"role": "user", "content": actual_prompt})
        history.append({"role": "assistant", "content": reply})
        last_reply = reply
    return "continue", history, last_reply, model


def _handle_cd_cmd(prompt: str, model: str, history: list,
                   last_reply: str) -> tuple[str, list, str, str]:
    """Switch the active workspace directory."""
    new_ws = prompt.split(maxsplit=1)[1].strip()
    new_p = Path(new_ws)
    if new_p.is_dir():
        os.environ["MUSE_WORKSPACE_ROOT"] = str(new_p)
        print(f"\n{C_GREEN}✓ Active workspace switched to: {new_p}{RST}\n")
    else:
        print(f"\n{C_RED}✗ Directory not found: {new_ws}{RST}\n")
    return "continue", history, last_reply, model


def _dispatch_command(
    prompt_lower: str,
    prompt: str,
    model: str,
    history: list,
    last_reply: str,
) -> tuple[str, list, str, str] | None:
    """Handle CLI commands. Returns (action, history, last_reply, model) or None."""
    if prompt_lower in ("/clear", "clear", "/new", "new"):
        clear_hist, clear_reply = _handle_clear()
        return "continue", clear_hist, clear_reply, model
    if prompt_lower in ("exit", "quit", ":q"):
        print(f"\n{BOLD}{C_CYAN}Goodbye! Happy coding with Muse.{RST}\n")
        return "break", history, last_reply, model
    if prompt_lower.startswith(("/git", "git")):
        _handle_git(prompt)
        return "continue", history, last_reply, model
    if prompt_lower in ("/model", "/models", "model", "models", "/m"):
        display_models(current_model=model)
        return "continue", history, last_reply, model
    if prompt_lower.startswith(("/model ", "/m ")):
        new_model = _handle_model_switch(prompt)
        return "continue", history, last_reply, new_model
    if prompt_lower.startswith(("/save", "/write")):
        _handle_save(prompt, last_reply)
        return "continue", history, last_reply, model
    if prompt_lower.startswith(("/open ", "/file ", "/folder ")):
        return _handle_open_cmd(prompt, model, history, last_reply)
    if prompt_lower.startswith(("/cd ", "/workspace ")):
        return _handle_cd_cmd(prompt, model, history, last_reply)
    if prompt_lower in ("/status", "status"):
        show_status()
        return "continue", history, last_reply, model
    if prompt_lower in ("/help", "help", "/?"):
        _handle_help()
        return "continue", history, last_reply, model
    return None


def interactive_chat(model: str = DEFAULT_MODEL):
    """Run interactive terminal session with rich visuals and git support."""
    ws = get_default_workspace()
    remote, branch = get_git_info(ws)
    w = _term_width()

    print(f"\n{BOLD}{C_BLUE}" + "═" * w + f"{RST}")
    print(f"  {BOLD}{C_WHITE}Muse.ai Terminal Workspace Bridge{RST} {C_GREEN}● ACTIVE{RST}")
    print(f"  {C_GRAY}Model:{RST} {BOLD}{C_CYAN}{model}{RST}  │  {C_GRAY}Tokens:{RST} {BOLD}{C_GREEN}4B Free Tier{RST}")
    print(f"  {C_GRAY}Workspace:{RST} {C_WHITE}{ws}{RST}")
    print(f"  {C_GRAY}GitHub:{RST}    {C_YELLOW}{remote}{RST} {C_PURPLE}[{branch}]{RST}")
    print(f"  {C_GRAY}Shortcuts:{RST} {C_CYAN}/git <cmd>{RST}, {C_CYAN}/open <file>{RST}, {C_CYAN}/save <file>{RST}, {C_CYAN}/model <id>{RST}, {C_CYAN}/help{RST}")
    print(f"{BOLD}{C_BLUE}" + "═" * w + f"{RST}\n")

    history: list[dict] = []
    last_reply = ""
    current_model = model

    while True:
        try:
            prompt_indicator = f"{BOLD}{C_PURPLE}[{current_model}]{RST} {BOLD}{C_GREEN}You >{RST} "
            prompt = input(prompt_indicator).strip()
            if not prompt:
                continue

            dispatched = _dispatch_command(
                prompt.lower(), prompt, current_model, history, last_reply
            )
            if dispatched is not None:
                action, history, last_reply, current_model = dispatched
                if action == "break":
                    break
                continue

            reply, actual_prompt = stream_chat(prompt, model=current_model, history=history)
            if reply:
                history.append({"role": "user", "content": actual_prompt})
                history.append({"role": "assistant", "content": reply})
                last_reply = reply
        except (KeyboardInterrupt, EOFError):
            print(f"\n\n{BOLD}{C_CYAN}Goodbye!{RST}\n")
            break


def show_status():
    """Display gateway status and token quota."""
    print(f"\n{BOLD}{C_BLUE}Checking gateway status at {GATEWAY_BASE}...{RST}")
    headers = {"Authorization": f"Bearer {ADMIN_KEY}"}
    try:
        h = httpx.get(f"{GATEWAY_BASE}/healthz", timeout=5.0)
        status_c = C_GREEN if h.status_code == 200 else C_RED
        print(f"  Gateway Health: {status_c}{h.status_code} ({h.text}){RST}")
        acc = httpx.get(f"{GATEWAY_BASE}/admin/accounts", headers=headers, timeout=5.0).json()
        accounts = acc.get("data", acc) if isinstance(acc, dict) else acc
        for a in accounts:
            lbl = a.get("label", "default")
            st = a.get("status", "unknown")
            print(f"  {C_CYAN}• Account [{lbl}]:{RST} {st}")
            q = httpx.get(
                f"{GATEWAY_BASE}/admin/accounts/{a.get('id')}/quota",
                headers=headers,
                timeout=10.0,
            )
            if q.status_code == 200:
                qd = q.json().get("quota", {})
                print(f"    {C_GRAY}Plan:{RST} {qd.get('plan')}  {C_GRAY}Left:{RST} {C_GREEN}{qd.get('extra_left')}{RST}")
        print()
    except Exception as e:
        print(f"  {C_RED}[Error]: {e}{RST}\n")


def main():
    argv = sys.argv[1:]
    selected_model = DEFAULT_MODEL
    filtered_argv = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-m", "--model") and i + 1 < len(argv):
            selected_model = argv[i + 1]
            i += 2
        elif arg.startswith("--model="):
            selected_model = arg.split("=", 1)[1]
            i += 1
        else:
            filtered_argv.append(arg)
            i += 1

    if not filtered_argv:
        interactive_chat(model=selected_model)
        return

    first = filtered_argv[0].lower()
    if first in ("models", "/models", "model", "/model"):
        display_models(current_model=selected_model)
    elif first == "chat":
        interactive_chat(model=selected_model)
    elif first in ("status", "/status"):
        show_status()
    else:
        prompt = " ".join(filtered_argv)
        stream_chat(prompt, model=selected_model)


if __name__ == "__main__":
    main()
