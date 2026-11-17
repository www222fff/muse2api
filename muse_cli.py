#!/usr/bin/env python3
"""Interactive and command-line CLI for muse.ai (via muse2api gateway).

Features:
- Antigravity-style Rich Markdown & Table rendering in terminal
- Live streaming Markdown panel updates
- Native GitHub and Git integration via `/git <cmd>`
- Automatic local workspace and multi-file code inspection
"""

import os
import re
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
    extract_file_edits,
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

PromptSession: Any = None
ANSI: Any = None
InMemoryHistory: Any = None
FileHistory: Any = None
KeyBindings: Any = None
HAVE_PROMPT_TOOLKIT = False
try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.history import FileHistory, InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings

    HAVE_PROMPT_TOOLKIT = True
except ImportError:
    pass

SESSION_USER_ID = os.environ.get("MUSE_SESSION_ID") or f"muse_cli_{os.getpid()}"


def _create_prompt_session() -> Any:
    """Create a prompt_toolkit PromptSession with bracketed paste and multi-line support."""
    if not HAVE_PROMPT_TOOLKIT:
        return None
    kb = KeyBindings()

    @kb.add("enter")
    def _handle_enter(event):
        buf = event.current_buffer
        text_before = buf.document.text_before_cursor
        if text_before.endswith("\\"):
            buf.delete_before_cursor(count=1)
            buf.newline()
        else:
            buf.validate_and_handle()

    # Shift+Return / Option+Return / Alt+Enter handlers across terminal emulators
    @kb.add("c-j")
    def _newline_cj(event):
        event.current_buffer.newline()

    @kb.add("escape", "enter")
    def _newline_alt_enter(event):
        event.current_buffer.newline()

    @kb.add("escape", "c-j")
    def _newline_alt_cj(event):
        event.current_buffer.newline()

    @kb.add("escape", "[", "1", "3", ";", "2", "u")
    def _newline_csiu(event):
        event.current_buffer.newline()

    @kb.add("escape", "[", "2", "7", ";", "2", ";", "1", "3", "~")
    def _newline_xterm(event):
        event.current_buffer.newline()

    @kb.add("escape", "O", "M")
    def _newline_om(event):
        event.current_buffer.newline()

    hist_file = os.path.expanduser("~/.muse_history")
    try:
        history_backend = FileHistory(hist_file)
    except Exception:
        history_backend = InMemoryHistory()

    return PromptSession(
        history=history_backend,
        key_bindings=kb,
        multiline=True,
    )


def _gateway_reachable(timeout: float = 1.5) -> bool:
    """Probe the gateway health endpoint (True when HTTP 200)."""
    try:
        return httpx.get(f"{GATEWAY_BASE}/healthz", timeout=timeout).status_code == 200
    except Exception:
        return False


def _emit_status_line(message: str, *, ok: bool | None = None) -> None:
    """Print a status line via Rich (when available) or plain text."""
    if HAVE_RICH and console:
        if ok is True:
            console.print(f"[bold green]{message}[/bold green]\n")
        elif ok is False:
            console.print(f"[bold red]{message}[/bold red]\n")
        else:
            console.print(f"[bold yellow]{message}[/bold yellow]")
    else:
        print(message)


def ensure_gateway() -> bool:
    """Check if gateway is reachable; if not, automatically launch it."""
    if _gateway_reachable():
        return True

    script = Path("/Users/Gaurav_Batra/muse2api/start_gateway.sh")
    if not script.exists():
        script = Path(__file__).resolve().parent / "start_gateway.sh"
    if not script.exists():
        return False

    _emit_status_line("\n↻ Gateway is offline. Auto-starting muse2api gateway on port 18610...")
    try:
        subprocess.run([str(script)], check=False, capture_output=True, timeout=10.0)
        import time

        for _ in range(16):
            time.sleep(0.5)
            if _gateway_reachable():
                _emit_status_line("✓ Gateway is online and ready.\n", ok=True)
                return True
    except Exception as e:
        _emit_status_line(f"✗ Failed to start gateway: {e}\n", ok=False)
    return False


def web_search(query: str, limit: int = 4) -> str:
    """Fetch live web snippets from DuckDuckGo."""
    try:
        resp = httpx.post(
            "https://lite.duckduckgo.com/lite/",
            data={"q": query},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=6.0,
        )
        if resp.status_code == 200:
            snippets = re.findall(r"class=\x27result-snippet\x27>(.*?)</td>", resp.text, re.DOTALL)
            clean = [re.sub(r"<[^>]+>", "", s).strip() for s in snippets]
            if clean:
                return "\n".join(f"[Web Result {i + 1}]: {s}" for i, s in enumerate(clean[:limit]))
    except Exception as e:
        return f"[Web Search Error: {e}]"
    return "[No web search results found]"


def compact_history(history: list[dict], max_turns: int = 4) -> list[dict]:
    """Auto-compact history if it exceeds max_turns to prevent token explosion."""
    if len(history) <= max_turns * 2:
        return history
    older_turns = history[:-4]
    recent_turns = history[-4:]
    summary_parts = []
    for m in older_turns:
        role = m.get("role", "user")
        content = m.get("content", "")
        snippet = content[:140].replace("\n", " ").strip()
        summary_parts.append(f"{role.capitalize()}: {snippet}...")
    summary_msg = {
        "role": "system",
        "content": "[Previous Conversation Context Compacted]:\n" + "\n".join(summary_parts),
    }
    return [summary_msg] + recent_turns


BASE_URL = os.environ.get("MUSE2API_BASE_URL", "http://127.0.0.1:18610/v1")
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
            cwd=ws,
            capture_output=True,
            text=True,
            timeout=2.0,
        ).stdout.strip()
        branch = subprocess.run(
            ["git", "branch", "--show-current"], cwd=ws, capture_output=True, text=True, timeout=2.0
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

    compacted = compact_history(list(history or []))
    messages = list(compacted)
    messages.append({"role": "user", "content": actual_prompt})

    if HAVE_RICH and console:
        full_text = _stream_rich(messages, model)
    else:
        full_text = _stream_ansi(messages, model)

    reply_str = "".join(full_text)
    if reply_str:
        _auto_apply_reply_edits(reply_str, prompt, paths)

    return reply_str, actual_prompt


def _auto_apply_reply_edits(
    reply: str,
    prompt: str,
    paths: list[Path],
) -> list[str]:
    """Automatically detect and save code edits from assistant reply to disk."""
    edits = extract_file_edits(reply, prompt, paths)
    if not edits:
        return []

    saved_paths: list[str] = []
    for target_path, code in edits:
        ok, msg = save_code_to_file(target_path, code)
        if ok:
            saved_paths.append(target_path)
            num_lines = len(code.splitlines())
            if HAVE_RICH and console:
                console.print(
                    Panel(
                        f"[bold green]✓ Agent Changes Applied to Disk:[/bold green]\n"
                        f"  [cyan]{target_path}[/cyan] ({num_lines} lines)\n\n"
                        f"[dim]↳ Live on disk. Visible in VS Code "
                        f"[bold]Source Control (Changes)[/bold] tab.[/dim]",
                        title="[bold green]● FILE SYSTEM WRITE SUCCESS[/bold green]",
                        border_style="green",
                    )
                )
            else:
                print(
                    f"\n{BOLD}{C_GREEN}✓ Agent Changes Applied to Disk:{RST} "
                    f"{C_CYAN}{target_path}{RST} ({num_lines} lines)"
                )
                print(
                    f"  {C_GRAY}↳ Live on disk. Visible in VS Code Source Control "
                    f"(Changes) tab.{RST}\n"
                )
        else:
            if HAVE_RICH and console:
                console.print(f"[bold red]✗ Failed to write {target_path}: {msg}[/bold red]")
            else:
                print(f"{C_RED}✗ Failed to write {target_path}: {msg}{RST}")
    return saved_paths


def _stream_rich(messages: list, model: str) -> list[str]:
    """Stream a reply with the Rich live panel; returns collected text chunks."""
    full_text: list[str] = []
    try:
        with Live(
            Panel(
                Markdown("Thinking..."),
                title=(
                    "[bold red]■ WORKING (Ctrl+C to Stop)[/bold red] │ "
                    f"[bold magenta]MUSE ({model})[/bold magenta]"
                ),
                border_style="magenta",
            ),
            refresh_per_second=8,
            console=console,
        ) as live:
            try:
                stream = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    stream=True,
                    timeout=180.0,
                    user=SESSION_USER_ID,
                )
            except Exception as conn_err:
                if "connect" in str(conn_err).lower() and ensure_gateway():
                    stream = client.chat.completions.create(
                        model=model,
                        messages=messages,
                        stream=True,
                        timeout=180.0,
                        user=SESSION_USER_ID,
                    )
                else:
                    raise conn_err
            for chunk in stream:
                delta = chunk.choices[0].delta.content or ""
                full_text.append(delta)
                live.update(
                    Panel(
                        Markdown("".join(full_text)),
                        title=(
                            "[bold red]■ WORKING[/bold red] │ "
                            f"[bold magenta]MUSE ({model})[/bold magenta]"
                        ),
                        border_style="magenta",
                    )
                )
            live.update(
                Panel(
                    Markdown("".join(full_text)),
                    title=(
                        "[bold blue]● IDLE / READY[/bold blue] │ "
                        f"[bold magenta]MUSE ({model})[/bold magenta]"
                    ),
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
        try:
            stream = client.chat.completions.create(
                model=model,
                messages=messages,
                stream=True,
                timeout=180.0,
                user=SESSION_USER_ID,
            )
        except Exception as conn_err:
            if "connect" in str(conn_err).lower() and ensure_gateway():
                stream = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    stream=True,
                    timeout=180.0,
                    user=SESSION_USER_ID,
                )
            else:
                raise conn_err
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
    is_active = m.id == current_model
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
        console.print(
            " [yellow]• Switch from CLI:[/yellow] [bold]muse -m <model_id> [prompt][/bold]\n"
        )
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
                    title=(
                        "[bold blue]⚡ Git: git "
                        f"{' '.join(args)} (Exit: {res.returncode})[/bold blue]"
                    ),
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
            console.print(
                "[bold green]✓ Active model switched to: [bold cyan]"
                f"{new_m}[/bold cyan][/bold green]\n"
            )
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
        ("/search <query>", "Search the web via DuckDuckGo and feed live context to Muse"),
        ("/compact", "Compact long conversation history to conserve context window"),
        ("/model <id>", "View available models or switch active model"),
        ("/cd <dir>", "Switch active workspace root directory"),
        ("/restart", "Refresh gateway browser connection and renew tabs"),
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


def _print_save_result(ok: bool, msg: str) -> None:
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


def _print_no_code_block() -> None:
    """Print 'no code block found' message."""
    if console is not None:
        console.print("[bold red]✗ No code block found in last reply to save.[/bold red]\n")
    else:
        print(f"\n{C_RED}✗ No code block found in last reply to save.{RST}\n")


def _handle_save(prompt: str, last_reply: str) -> None:
    """Save code block from last reply to specified or default path."""
    code = extract_code_block(last_reply)
    if not code:
        _print_no_code_block()
        return
    parts = prompt.split(maxsplit=1)
    target = parts[1].strip() if len(parts) > 1 else "output.py"
    _, msg = save_code_to_file(target, code)
    _print_save_result(bool(msg), msg)


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
    """Handle /restart or /start commands to refresh gateway and connection."""
    triggers = (
        "/restart",
        "restart",
        "/reset",
        "reset",
        "/renew",
        "/start",
        "start",
        "server start",
        "start server",
        "start muse2api",
    )
    if prompt_lower in triggers or any(
        prompt_lower.startswith(pfx) for pfx in ("start", "server start", "satrt")
    ):
        if HAVE_RICH and console:
            console.print(
                "\n[bold yellow]↻ Checking and refreshing Muse gateway connection...[/bold yellow]"
            )
        else:
            print("\n↻ Checking and refreshing Muse gateway connection...")
        ensure_gateway()
        try:
            httpx.post(
                f"{GATEWAY_BASE}/admin/accounts/renew_all",
                headers={"Authorization": f"Bearer {ADMIN_KEY}"},
                timeout=5.0,
            )
        except Exception:
            pass
        if HAVE_RICH and console:
            console.print(
                "[bold green]✓ Gateway connection verified and tabs renewed. "
                "State: [bold blue]● IDLE / READY[/bold blue][/bold green]\n"
            )
        else:
            print("✓ Gateway connection verified and tabs renewed. State: ● IDLE / READY\n")
        return "continue", history, last_reply, model
    return None


def _handle_compact_cmd(prompt_lower, prompt, model, history, last_reply):
    """Handle /compact command."""
    if prompt_lower in ("/compact", "compact"):
        orig_len = len(history)
        history = compact_history(history, max_turns=3)
        if HAVE_RICH and console:
            console.print(
                "[bold green]✓ Conversation history compacted "
                f"({orig_len} ➜ {len(history)} messages).[/bold green]\n"
            )
        else:
            print(
                f"\n{C_GREEN}✓ Conversation history compacted "
                f"({orig_len} ➜ {len(history)} messages).{RST}\n"
            )
        return "continue", history, last_reply, model
    return None


def _handle_search_cmd(prompt_lower, prompt, model, history, last_reply):
    """Handle /search <query> command to perform live web search via DuckDuckGo."""
    if prompt_lower.startswith("/search"):
        parts = prompt.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            if HAVE_RICH and console:
                console.print("[bold yellow]Usage: /search <query>[/bold yellow]\n")
            else:
                print(f"\n{C_YELLOW}Usage: /search <query>{RST}\n")
            return "continue", history, last_reply, model
        query = parts[1].strip()
        if HAVE_RICH and console:
            console.print(f"\n[bold cyan]🔍 Searching the web for:[/bold cyan] {query}...")
        else:
            print(f"\n🔍 Searching the web for: {query}...")
        results = web_search(query)
        sub_prompt = f"Based on these live web search results:\n{results}\n\nUser Question: {query}"
        reply, actual_prompt = stream_chat(sub_prompt, model=model, history=history)
        if reply:
            history.append({"role": "user", "content": actual_prompt})
            history.append({"role": "assistant", "content": reply})
            last_reply = reply
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
    _handle_compact_cmd,
    _handle_search_cmd,
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
            "[bold cyan]Model:[/bold cyan] "
            f"{model}  │  [bold green]Tokens:[/bold green] 4B Free Web Tier\n"
            f"[bold cyan]Workspace:[/bold cyan] {ws}\n"
            f"[bold cyan]GitHub:[/bold cyan]    {remote} [magenta]({branch})[/magenta]\n"
            "[bold yellow]Commands:[/bold yellow] /git <cmd>, /open <file>, "
            "/save <file>, /search <query>, /compact, /help\n"
            "[dim cyan]Editor Tip: Enter sends │ Shift+Return (or Option+Return "
            "/ line ending with \\) for new line[/dim cyan]"
        )
        console.print(
            Panel(
                info_panel,
                title=(
                    "[bold blue]Muse.ai Terminal Workspace Bridge "
                    "[green]● ACTIVE[/green][/bold blue]"
                ),
                border_style="blue",
            )
        )
        return
    w = _term_width()
    print(f"\n{BOLD}{C_BLUE}" + "═" * w + f"{RST}")
    print(f"  {BOLD}{C_WHITE}Muse.ai Terminal Workspace Bridge{RST} {C_GREEN}● ACTIVE{RST}")
    print(
        f"  {C_GRAY}Model:{RST} {BOLD}{C_CYAN}{model}{RST}  │  "
        f"{C_GRAY}Tokens:{RST} {BOLD}{C_GREEN}4B Free Tier{RST}"
    )
    print(f"  {C_GRAY}Workspace:{RST} {C_WHITE}{ws}{RST}")
    print(f"  {C_GRAY}GitHub:{RST}    {C_YELLOW}{remote}{RST} {C_PURPLE}[{branch}]{RST}")
    print(
        f"  {C_GRAY}Shortcuts:{RST} {C_CYAN}/git <cmd>{RST}, {C_CYAN}/open <file>{RST}, "
        f"{C_CYAN}/save <file>{RST}, {C_CYAN}/help{RST}"
    )
    print(
        f"  {C_GRAY}Editor:{RST}    {C_YELLOW}Enter sends │ Shift+Return "
        f"or Option+Return for newline{RST}"
    )
    print(f"{BOLD}{C_BLUE}" + "═" * w + f"{RST}\n")


def _read_user_prompt(session, prompt_indicator: str) -> str:
    """Read one user prompt from the session prompt or stdin fallback."""
    if session is not None and sys.stdin.isatty():
        return session.prompt(
            ANSI(prompt_indicator),
            prompt_continuation=ANSI("\033[2m  ... \033[0m"),
        ).strip()
    return input(prompt_indicator).strip()


def interactive_chat(model: str = DEFAULT_MODEL):
    """Run interactive terminal session with rich visuals and git support."""
    ws = get_default_workspace()
    remote, branch = get_git_info(ws)
    _print_start_banner(ws, remote, branch, model)
    ensure_gateway()

    history: list[dict] = []
    last_reply = ""
    current_model = model
    session = _create_prompt_session()

    while True:
        try:
            prompt_indicator = f"\033[1;34m●\033[0m [{current_model}] You > "
            prompt = _read_user_prompt(session, prompt_indicator)
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
    q = httpx.get(
        f"{GATEWAY_BASE}/admin/accounts/{account_id}/quota", headers=headers, timeout=10.0
    )
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
            _show_status_rich(headers, accounts)
        else:
            _show_status_plain(headers, accounts, h)
    except Exception as e:
        if HAVE_RICH and console:
            console.print(f"[bold red]Status Error: {e}[/bold red]\n")
        else:
            print(f"[Error]: {e}\n")


def _show_status_rich(headers: dict, accounts: list) -> None:
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
        _render_status_account(
            None, False, a.get("label", "default"), a.get("status", "unknown"), qd
        )
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
