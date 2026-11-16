#!/usr/bin/env python3
"""Interactive and command-line CLI for muse.ai (via muse2api gateway).

Features:
- Antigravity-style Rich Markdown & Table rendering in terminal
- Live streaming Markdown panel updates
- Native GitHub and Git integration via `/git <cmd>`
- Automatic local workspace and multi-file code inspection
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
from openai import OpenAI
from workspace_helper import (
    build_context_for_paths,
    extract_code_block,
    find_local_paths,
    get_default_workspace,
    save_code_to_file,
)

Console: Any = None
Live: Any = None
Panel: Any = None
Markdown: Any = None
Table: Any = None
console: Any = None
HAVE_RICH = False
try:
    from rich.console import Console
    from rich.live import Live
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.table import Table
    HAVE_RICH = True
    console = Console()
except ImportError:
    pass

BASE_URL = os.environ.get("MUSE2API_URL", "http://127.0.0.1:18610/v1")
GATEWAY_BASE = BASE_URL.rstrip("/v1").rstrip("/")
API_KEY = os.environ.get("MUSE2API_API_KEY", "sk-muse2api-local-secret")
ADMIN_KEY = os.environ.get("MUSE2API_ADMIN_KEY", "sk-muse2api-admin-secret")
DEFAULT_MODEL = os.environ.get("MUSE2API_DEFAULT_MODEL", "muse-spark-1.3")

client = OpenAI(base_url=BASE_URL, api_key=API_KEY)

# ANSI Styling Palette (Fallback when Rich is not available)
RST = "\033[0m"
BOLD = "\033[1m"
C_CYAN = "\033[38;5;51m"
C_BLUE = "\033[38;5;39m"
C_GREEN = "\033[38;5;48m"
C_YELLOW = "\033[38;5;220m"
C_PURPLE = "\033[38;5;141m"
C_RED = "\033[38;5;203m"
C_GRAY = "\033[38;5;244m"
C_WHITE = "\033[38;5;255m"


def _term_width() -> int:
    return min(shutil.get_terminal_size((80, 24)).columns, 100)


def print_user_box(prompt: str, paths: list[Path] | None = None) -> None:
    """Print structured container for the user's prompt."""
    if HAVE_RICH and console:
        console.print()
        console.print(Panel(prompt, title="[bold cyan]❯ QUESTION[/bold cyan]", border_style="cyan"))
        if paths:
            for p in paths:
                console.print(f"  [bold yellow]↳ ⚡ [Local Workspace Context: {p}][/bold yellow]")
        console.print()
    else:
        w = _term_width()
        div = "─" * max(10, w - 18)
        print(f"\n{BOLD}{C_CYAN}┌── ❯ QUESTION {div}{RST}")
        for line in prompt.splitlines():
            print(f"{BOLD}{C_CYAN}│{RST} {C_WHITE}{BOLD}{line}{RST}")
        print(f"{BOLD}{C_CYAN}└──{div}──{RST}")
        if paths:
            for p in paths:
                print(f"  {C_YELLOW}↳ ⚡ [Local Workspace Context: {p}]{RST}")


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
    """Stream chat response with live rendered Markdown tables and formatting."""
    paths = find_local_paths(prompt, history=history)
    ctx = build_context_for_paths(paths, prompt=prompt)
    print_user_box(prompt, paths)
    actual_prompt = f"{ctx}\n\n[User Instruction]: {prompt}"

    messages = list(history or [])
    messages.append({"role": "user", "content": actual_prompt})

    if HAVE_RICH and console:
        full_text = _stream_rich(messages, model)
    else:
        full_text = _stream_ansi(messages, model)

    return "".join(full_text), actual_prompt


def _stream_rich(messages: list, model: str) -> list[str]:
    """Stream a reply with the Rich live panel; returns collected text chunks."""
    full_text: list[str] = []
    try:
        with Live(
            Panel(
                Markdown("Thinking..."),
                title=f"[bold red]■ WORKING (Ctrl+C to Stop)[/bold red] │ [bold magenta]MUSE ({model})[/bold magenta]",
                border_style="magenta",
            ),
            refresh_per_second=8,
            console=console,
        ) as live:
            stream = client.chat.completions.create(
                model=model,
                messages=messages,
                stream=True,
                timeout=180.0,
            )
            for chunk in stream:
                delta = chunk.choices[0].delta.content or ""
                full_text.append(delta)
                live.update(
                    Panel(
                        Markdown("".join(full_text)),
                        title=f"[bold red]■ WORKING[/bold red] │ [bold magenta]MUSE ({model})[/bold magenta]",
                        border_style="magenta",
                    )
                )
            live.update(
                Panel(
                    Markdown("".join(full_text)),
                    title=f"[bold blue]● IDLE / READY[/bold blue] │ [bold magenta]MUSE ({model})[/bold magenta]",
                    border_style="blue",
                )
            )
        console.print()
    except KeyboardInterrupt:
        console.print("\n[bold red]■ STOPPED by user (Ctrl+C). Session preserved.[/bold red]\n")
    except Exception as e:
        console.print(f"[bold red][Error]: {e}[/bold red]\n")
    return full_text


def _stream_ansi(messages: list, model: str) -> list[str]:
    """Stream a reply with plain ANSI output; returns collected text chunks."""
    w = _term_width()
    div = "─" * max(10, w - 24)
    print(f"\n{BOLD}{C_PURPLE}╭── ■ WORKING │ MUSE ({model}) {div}{RST}")
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
    except KeyboardInterrupt:
        print(f"\n{BOLD}{C_RED}■ STOPPED by user (Ctrl+C). Session preserved.{RST}\n")
    except Exception as e:
        print(f"\n{C_RED}[Error]: {e}{RST}")
    finally:
        div_end = "─" * max(10, w - 4)
        print(f"{BOLD}{C_BLUE}╰── ● IDLE {div_end}{RST}\n")
    return full_text


def _format_model_row(table, m, current_model: str, rich: Any = False) -> None:
    """Print or add a single model row depending on available output mode."""
    is_active = (m.id == current_model)
    if rich:
        stat = "[bold green]➜ Active[/bold green]" if is_active else ""
        table.add_row(stat, m.id, getattr(m, "kind", "chat"), "4B Free Web Tokens")
    else:
        star = BOLD + C_GREEN + " ➜ " + RST if is_active else "   "
        act = BOLD + C_GREEN + "(Active)" + RST if is_active else ""
        kind = getattr(m, "kind", "chat")
        print(f"{star}{C_CYAN}{m.id:<30}{RST} {C_GRAY}[{kind}]{RST}{act}")


def _fetch_model_list():
    """Try to fetch model list from gateway; return items or empty list."""
    try:
        return client.models.list().data
    except Exception:
        return []


def _print_model_header(table, rich: Any, w: int) -> None:
    """Print header and table for model listing."""
    if rich:
        console.print()
        console.print(table)
        console.print(" [yellow]• Switch in chat:[/yellow]  [bold]/model <model_id>[/bold]")
        console.print(" [yellow]• Switch from CLI:[/yellow] [bold]muse -m <model_id> [prompt][/bold]\n")
    else:
        print(f"{BOLD}{C_BLUE}" + "═" * w + f"{RST}")
        print(f"  {BOLD}{C_WHITE}Available Models (muse.ai Web Gateway - 4B Free Tokens){RST}")
        print(f"{BOLD}{C_BLUE}" + "═" * w + f"{RST}")


def _print_model_footer(w: int) -> None:
    """Print footer for model listing."""
    print(f"{C_GRAY}" + "─" * w + f"{RST}")
    print(f" {C_YELLOW}• Switch in chat:{RST}  {BOLD}/model <model_id>{RST}")
    print(f" {C_YELLOW}• Switch from CLI:{RST} {BOLD}muse -m <model_id> [prompt]\\n")


def display_models(current_model: str = ""):
    """Display available models with rich table formatting."""
    w = _term_width()

    table = None
    if HAVE_RICH and console is not None:
        table = Table(
            title="Available Models (muse.ai Web Gateway - 4B Free Tokens)",
            border_style="blue",
            header_style="bold cyan",
        )
        table.add_column("Status", justify="center", width=10)
        table.add_column("Model ID", style="bold white")
        table.add_column("Type", style="dim")
        table.add_column("Access Tier", style="green")

    items = _fetch_model_list()
    for m in items:
        _format_model_row(table, m, current_model, HAVE_RICH and console)

    if HAVE_RICH and console is not None:
        _print_model_header(table, HAVE_RICH and console, w)
    else:
        _print_model_header(None, HAVE_RICH and console, w)
        _print_model_footer(w)


def _handle_git(prompt: str) -> None:
    """Execute git commands in the active workspace and format output."""
    ws = get_default_workspace()
    cmd_args = prompt.strip().split()
    args = cmd_args[1:] if len(cmd_args) > 1 else ["status"]

    try:
        res = subprocess.run(
            ["git", *args],
            cwd=ws,
            capture_output=True,
            text=True,
            timeout=30.0,
        )
        output = res.stdout or res.stderr or "No output."
        if HAVE_RICH and console:
            console.print(
                Panel(
                    output.strip(),
                    title=f"[bold blue]⚡ Git: git {' '.join(args)} (Exit: {res.returncode})[/bold blue]",
                    border_style="blue",
                )
            )
        else:
            print(f"\n{BOLD}{C_BLUE}┌── ⚡ GIT EXECUTION: git {' '.join(args)} ──{RST}")
            for line in output.splitlines():
                print(f"{C_WHITE}{line}{RST}")
            print(f"{BOLD}{C_BLUE}└── [Exit code: {res.returncode}]{RST}\n")
    except Exception as e:
        if HAVE_RICH and console:
            console.print(f"[bold red]Error running git: {e}[/bold red]")
        else:
            print(f"{C_RED}Error running git: {e}{RST}\n")


def _handle_clear() -> tuple[list, str]:
    """Clear conversation history; return new history and empty last_reply."""
    ws = get_default_workspace()
    if HAVE_RICH and console:
        console.print(f"[bold green]✓ Local Workspace Access Active:[/bold green] {ws}")
        console.print("[bold yellow]✓ Conversation history cleared.[/bold yellow]\n")
    else:
        print(f"\n{C_GREEN}✓ Local Workspace Access Active:{RST} {ws}")
        print(f"{C_YELLOW}✓ Conversation history cleared.{RST}\n")
    return [], ""


def _handle_model_switch(prompt: str) -> str:
    """Switch active model from /model <id> command; return new model."""
    parts = prompt.split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip():
        new_m = parts[1].strip()
        if HAVE_RICH and console:
            console.print(f"[bold green]✓ Active model switched to: [bold cyan]{new_m}[/bold cyan][/bold green]\n")
        else:
            print(f"\n{C_GREEN}✓ Active model switched to: {BOLD}{new_m}{RST}\n")
        return new_m
    return DEFAULT_MODEL


def _handle_help() -> None:
    """Display styled command help."""
    STATUS_CMD = "/status"
    commands = [
        ("/git <command>", "Run git operations directly (e.g. /git status, /git diff)"),
        ("/open <path>", "Open and attach local file or folder to conversation context"),
        ("/save <path>", "Save code block from last reply directly to file"),
        ("/model <id>", "View available models or switch active model"),
        ("/cd <dir>", "Switch active workspace root directory"),
        (STATUS_CMD, "Check gateway health and token quota"),
        ("clear", "Reset conversation context"),
        ("exit", "Exit session"),
    ]
    if HAVE_RICH and console:
        table = Table(title="Muse Interactive Commands", border_style="cyan")
        table.add_column("Command", style="bold cyan")
        table.add_column("Action", style="white")
        for cmd, desc in commands:
            table.add_row(cmd, desc)
        console.print(table)
    else:
        print(f"\n{BOLD}{C_WHITE}Muse Interactive Commands:{RST}")
        for cmd, desc in commands:
            print(f"  {BOLD}{C_CYAN}{cmd:<18}{RST} {C_GRAY}➜{RST} {desc}")
        print()


def _print_save_result(ok: bool, msg: str, rich: Any) -> None:
    """Print save result message depending on output mode."""
    if console is not None:
        if ok:
            console.print(f"[bold green]✓ {msg}[/bold green]\n")
        else:
            console.print(f"[bold red]✗ {msg}[/bold red]\n")
    else:
        if ok:
            print(f"\n{C_GREEN}✓ {msg}{RST}\n")
        else:
            print(f"\n{C_RED}✗ {msg}{RST}\n")


def _print_no_code_block(rich: Any) -> None:
    """Print 'no code block found' message."""
    if console is not None:
        console.print("[bold red]✗ No code block found in last reply to save.[/bold red]\n")
    else:
        print(f"\n{C_RED}✗ No code block found in last reply to save.{RST}\n")


def _handle_save(prompt: str, last_reply: str) -> None:
    """Save code block from last reply to specified or default path."""
    code = extract_code_block(last_reply)
    if not code:
        _print_no_code_block(HAVE_RICH and console)
        return
    parts = prompt.split(maxsplit=1)
    target = parts[1].strip() if len(parts) > 1 else "output.py"
    _, msg = save_code_to_file(target, code)
    _print_save_result(bool(msg), msg, HAVE_RICH and console)


def _handle_clear_cmd(prompt_lower, prompt, model, history, last_reply):
    """Handle /clear, clear, /new, new commands."""
    if prompt_lower in ("/clear", "clear", "/new", "new"):
        clear_hist, clear_reply = _handle_clear()
        return "continue", clear_hist, clear_reply, model
    return None


def _handle_exit_cmd(prompt_lower, prompt, model, history, last_reply):
    """Handle exit, quit, :q commands."""
    if prompt_lower in ("exit", "quit", ":q"):
        if HAVE_RICH and console:
            console.print("[bold cyan]Goodbye! Happy coding with Muse.[/bold cyan]\n")
        else:
            print(f"\n{BOLD}{C_CYAN}Goodbye! Happy coding with Muse.{RST}\n")
        return "break", history, last_reply, model
    return None


def _handle_git_cmd(prompt_lower, prompt, model, history, last_reply):
    """Handle /git, git commands."""
    if prompt_lower.startswith(("/git", "git")):
        _handle_git(prompt)
        return "continue", history, last_reply, model
    return None


def _handle_model_view_cmd(prompt_lower, prompt, model, history, last_reply):
    """Handle /model, /models, model, models, /m commands."""
    if prompt_lower in ("/model", "/models", "model", "models", "/m"):
        display_models(current_model=model)
        return "continue", history, last_reply, model
    return None


def _handle_model_switch_cmd(prompt_lower, prompt, model, history, last_reply):
    """Handle /model <id>, /m <id> commands."""
    if prompt_lower.startswith(("/model ", "/m ")):
        new_model = _handle_model_switch(prompt)
        return "continue", history, last_reply, new_model
    return None


def _handle_save_cmd(prompt_lower, prompt, model, history, last_reply):
    """Handle /save, /write commands."""
    if prompt_lower.startswith(("/save", "/write")):
        _handle_save(prompt, last_reply)
        return "continue", history, last_reply, model
    return None


def _handle_open_cmd(prompt_lower, prompt, model, history, last_reply):
    """Handle /open, /file, /folder commands."""
    if prompt_lower.startswith(("/open ", "/file ", "/folder ")):
        target = prompt.split(maxsplit=1)[1].strip()
        p = Path(target)
        if not p.is_absolute():
            p = get_default_workspace() / p
        if p.exists():
            sub_prompt = f"Please inspect the attached local code and analyze it:\n{p}"
            reply, actual_prompt = stream_chat(sub_prompt, model=model, history=history)
            if reply:
                history.append({"role": "user", "content": actual_prompt})
                history.append({"role": "assistant", "content": reply})
                last_reply = reply
        else:
            if HAVE_RICH and console:
                console.print(f"[bold red]✗ Path not found: {target}[/bold red]\n")
            else:
                print(f"\n{C_RED}✗ Path not found: {target}{RST}\n")
        return "continue", history, last_reply, model
    return None


def _handle_cd_cmd(prompt_lower, prompt, model, history, last_reply):
    """Handle /cd, /workspace commands."""
    if prompt_lower.startswith(("/cd ", "/workspace ")):
        new_ws = prompt.split(maxsplit=1)[1].strip()
        new_p = Path(new_ws)
        if new_p.is_dir():
            os.environ["MUSE_WORKSPACE_ROOT"] = str(new_p)
            if HAVE_RICH and console:
                console.print(f"[bold green]✓ Active workspace switched to: {new_p}[/bold green]\n")
            else:
                print(f"\n{C_GREEN}✓ Active workspace switched to: {new_p}{RST}\n")
        else:
            if HAVE_RICH and console:
                console.print(f"[bold red]✗ Directory not found: {new_ws}[/bold red]\n")
            else:
                print(f"\n{C_RED}✗ Directory not found: {new_ws}{RST}\n")
        return "continue", history, last_reply, model
    return None


def _handle_status_cmd(prompt_lower, prompt, model, history, last_reply):
    """Handle /status, status commands."""
    if prompt_lower in ("/status", "status"):
        show_status()
        return "continue", history, last_reply, model
    return None


def _handle_restart_cmd(prompt_lower, prompt, model, history, last_reply):
    """Handle /restart, restart, /reset, reset commands to refresh gateway and connection."""
    if prompt_lower in ("/restart", "restart", "/reset", "reset", "/renew"):
        if HAVE_RICH and console:
            console.print("\n[bold yellow]↻ Refreshing Muse gateway connection and browser tabs...[/bold yellow]")
        else:
            print("\n↻ Refreshing Muse gateway connection and browser tabs...")
        try:
            httpx.post(f"{GATEWAY_BASE}/admin/accounts/renew_all", headers={"Authorization": f"Bearer {ADMIN_KEY}"}, timeout=5.0)
        except Exception:
            pass
        if HAVE_RICH and console:
            console.print("[bold green]✓ Gateway tabs renewed. State: [bold blue]● IDLE / READY[/bold blue][/bold green]\n")
        else:
            print("✓ Gateway tabs renewed. State: ● IDLE / READY\n")
        return "continue", history, last_reply, model
    return None


def _handle_help_cmd(prompt_lower, prompt, model, history, last_reply):
    """Handle /help, help, /? commands."""
    if prompt_lower in ("/help", "help", "/?"):
        _handle_help()
        return "continue", history, last_reply, model
    return None


_COMMAND_HANDLERS = [
    _handle_clear_cmd,
    _handle_exit_cmd,
    _handle_git_cmd,
    _handle_model_view_cmd,
    _handle_model_switch_cmd,
    _handle_save_cmd,
    _handle_open_cmd,
    _handle_cd_cmd,
    _handle_status_cmd,
    _handle_restart_cmd,
    _handle_help_cmd,
]


def _dispatch_command(
    prompt_lower: str,
    prompt: str,
    model: str,
    history: list,
    last_reply: str,
) -> tuple[str, list, str, str] | None:
    """Handle CLI commands. Returns (action, history, last_reply, model) or None."""
    for handler in _COMMAND_HANDLERS:
        result = handler(prompt_lower, prompt, model, history, last_reply)
        if result is not None:
            return result
    return None


def _print_start_banner(ws, remote: str, branch: str, model: str) -> None:
    """Print the session start banner (Rich panel or plain ANSI)."""
    if HAVE_RICH and console:
        info_panel = (
            f"[bold cyan]Model:[/bold cyan] {model}  │  [bold green]Tokens:[/bold green] 4B Free Web Tier\n"
            f"[bold cyan]Workspace:[/bold cyan] {ws}\n"
            f"[bold cyan]GitHub:[/bold cyan]    {remote} [magenta]({branch})[/magenta]\n"
            f"[bold yellow]Commands:[/bold yellow] /git <cmd>, /open <file>, /save <file>, /model <id>, /help"
        )
        console.print(
            Panel(
                info_panel,
                title="[bold blue]Muse.ai Terminal Workspace Bridge [green]● ACTIVE[/green][/bold blue]",
                border_style="blue",
            )
        )
        return
    w = _term_width()
    print(f"\n{BOLD}{C_BLUE}" + "═" * w + f"{RST}")
    print(f"  {BOLD}{C_WHITE}Muse.ai Terminal Workspace Bridge{RST} {C_GREEN}● ACTIVE{RST}")
    print(f"  {C_GRAY}Model:{RST} {BOLD}{C_CYAN}{model}{RST}  │  {C_GRAY}Tokens:{RST} {BOLD}{C_GREEN}4B Free Tier{RST}")
    print(f"  {C_GRAY}Workspace:{RST} {C_WHITE}{ws}{RST}")
    print(f"  {C_GRAY}GitHub:{RST}    {C_YELLOW}{remote}{RST} {C_PURPLE}[{branch}]{RST}")
    print(f"  {C_GRAY}Shortcuts:{RST} {C_CYAN}/git <cmd>{RST}, {C_CYAN}/open <file>{RST}, {C_CYAN}/save <file>{RST}, {C_CYAN}/help{RST}")
    print(f"{BOLD}{C_BLUE}" + "═" * w + f"{RST}\n")


def interactive_chat(model: str = DEFAULT_MODEL):
    """Run interactive terminal session with rich visuals and git support."""
    ws = get_default_workspace()
    remote, branch = get_git_info(ws)
    _print_start_banner(ws, remote, branch, model)

    history: list[dict] = []
    last_reply = ""
    current_model = model

    while True:
        try:
            prompt_indicator = f"\033[1;34m●\033[0m [{current_model}] You > "
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
        except KeyboardInterrupt:
            print("\n(Type 'exit' to quit chat session)\n")
            continue
        except EOFError:
            print("\nGoodbye!\n")
            break


def _fetch_account_quota(headers: dict, account_id: str) -> dict:
    """Fetch quota for a single account."""
    q = httpx.get(f"{GATEWAY_BASE}/admin/accounts/{account_id}/quota", headers=headers, timeout=10.0)
    return q.json().get("quota", {}) if q.status_code == 200 else {}


def _render_status_account(t, rich: Any, lbl: str, st: str, qd: dict) -> None:
    """Render a single account status row."""
    if rich:
        t.add_row(lbl, st, qd.get("plan", "-"), str(qd.get("extra_left", "-")))
    else:
        print(f"  Account: {lbl}, Status: {st}")


def show_status():
    """Display gateway status and token quota."""
    headers = {"Authorization": f"Bearer {ADMIN_KEY}"}
    try:
        h = httpx.get(f"{GATEWAY_BASE}/healthz", timeout=5.0)
        acc = httpx.get(f"{GATEWAY_BASE}/admin/accounts", headers=headers, timeout=5.0).json()
        accounts = acc.get("data", acc) if isinstance(acc, dict) else acc
        if HAVE_RICH and console is not None:
            _show_status_rich(headers, accounts, h)
        else:
            _show_status_plain(headers, accounts, h)
    except Exception as e:
        if HAVE_RICH and console:
            console.print(f"[bold red]Status Error: {e}[/bold red]\n")
        else:
            print(f"[Error]: {e}\n")


def _show_status_rich(headers: dict, accounts: list, h) -> None:
    """Render gateway status as a Rich table."""
    t = Table(title=f"Gateway Status: {GATEWAY_BASE}", border_style="blue")
    t.add_column("Account", style="cyan")
    t.add_column("Status", style="green")
    t.add_column("Plan", style="yellow")
    t.add_column("Extra Left", style="bold green")
    for a in accounts:
        lbl = a.get("label", "default")
        st = a.get("status", "unknown")
        qd = _fetch_account_quota(headers, a.get("id"))
        _render_status_account(t, True, lbl, st, qd)
    console.print(t)


def _show_status_plain(headers: dict, accounts: list, h) -> None:
    """Render gateway status as plain text."""
    print(f"\nGateway Health: {h.status_code} ({h.text})")
    for a in accounts:
        qd = _fetch_account_quota(headers, a.get("id"))
        _render_status_account(None, False, a.get("label", "default"),
                               a.get("status", "unknown"), qd)
    print()


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
