#!/usr/bin/env python3
"""Interactive and command-line CLI for muse.ai (via muse2api gateway)."""

import os
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


def stream_chat(prompt: str, model: str = DEFAULT_MODEL, history: list | None = None) -> tuple[str, str]:
    paths = find_local_paths(prompt)
    if paths:
        ctx = build_context_for_paths(paths)
        for p in paths:
            print(f"  [Accessed local: {p}]")
        actual_prompt = f"{ctx}\n\n[User Instruction]: {prompt}"
    else:
        actual_prompt = prompt

    messages = list(history or [])
    messages.append({"role": "user", "content": actual_prompt})

    try:
        stream = client.chat.completions.create(
            model=model,
            messages=messages,
            stream=True,
            timeout=180.0
        )
        full_text = []
        for chunk in stream:
            delta = chunk.choices[0].delta.content or ""
            full_text.append(delta)
            sys.stdout.write(delta)
            sys.stdout.flush()
        print()
        return "".join(full_text), actual_prompt
    except Exception as e:
        print(f"\n[Error]: {e}")
        return "", actual_prompt


def display_models(current_model: str = ""):
    print("\n" + "=" * 65)
    print("  Available Models (muse.ai Web Gateway - 4B Free Tokens)")
    print("=" * 65)
    try:
        items = client.models.list().data
    except Exception:
        items = []

    for m in items:
        star = " * " if m.id == current_model else "   "
        act = " (Active)" if m.id == current_model else ""
        print(f"{star}{m.id:<28} {getattr(m, 'kind', 'chat')}{act}")

    print("-" * 65)
    print("To switch model in chat:  /model <model_id>")
    print("To switch model from CLI: muse -m <model_id> [prompt]\n")


def _handle_exit(prompt_lower: str) -> bool:
    """Return True if prompt is an exit command."""
    return prompt_lower in ("exit", "quit", ":q")


def _handle_clear() -> tuple[list, str]:
    """Clear conversation history; return new history and empty last_reply."""
    ws = get_default_workspace()
    print(f"[Local Workspace Access Enabled - Connected to {ws}]")
    print("[Conversation history cleared]\n")
    return [], ""


def _handle_model_view(current_model: str) -> None:
    """Display available models."""
    display_models(current_model=current_model)


def _handle_model_switch(prompt: str) -> str:
    """Switch active model from /model <id> or /m <id> command; return new model."""
    new_m = prompt.split(maxsplit=1)[1].strip()
    if new_m:
        print(f"[Active model switched to: {new_m}]\n")
    return new_m


def _handle_status() -> None:
    """Check gateway status."""
    show_status()


def _handle_help() -> None:
    """Display help text."""
    print("\nCommands:")
    print("  /open <path>  - Open and attach local file or directory to context")
    print("  /save <path>  - Save code block from last reply to disk")
    print("  /model <id>   - View or switch active model")
    print("  /cd <dir>     - Switch active workspace root directory")
    print("  /status       - Check gateway status and token quota")
    print("  clear         - Clear conversation history")
    print("  exit          - Exit chat\n")


def _handle_save(last_reply: str) -> str:
    """Save code block from last reply; return (possibly unchanged) last_reply."""
    code = extract_code_block(last_reply)
    if code:
        # Use a default target if not specified; save_code_to_file handles it
        _, msg = save_code_to_file("output.py", code)
        print(f"[{msg}]\n")
    else:
        print("[No code block found in last reply to save]\n")
    return last_reply


def _handle_chat_message(
    prompt: str,
    model: str,
    history: list,
    last_reply: str,
) -> tuple[list, str]:
    """Send a user message, stream the reply, and update history. Returns (history, last_reply)."""
    print("\nMuse > ", end="", flush=True)
    reply, actual_prompt = stream_chat(prompt, model=model, history=history)
    if reply:
        history.append({"role": "user", "content": actual_prompt})
        history.append({"role": "assistant", "content": reply})
        last_reply = reply
    print()
    return history, last_reply


def _handle_open(prompt: str, model: str, history: list,
                 last_reply: str) -> tuple[str, list, str]:
    """Attach a local path and feed an inspection prompt to the model."""
    target = prompt.split(maxsplit=1)[1].strip()
    p = Path(target)
    if not p.is_absolute():
        p = get_default_workspace() / p
    if p.exists():
        print(f"[Attached local path: {p}]")
        sub_prompt = f"Please inspect the attached local code and analyze it:\n{p}"
        new_history, new_reply = _handle_chat_message(sub_prompt, model, history, last_reply)
        return "continue", new_history, new_reply
    print(f"[Path not found: {target}]\n")
    return "continue", history, last_reply


def _handle_cd(prompt: str, history: list, last_reply: str) -> tuple[str, list, str]:
    """Switch the active workspace directory."""
    new_ws = prompt.split(maxsplit=1)[1].strip()
    new_p = Path(new_ws)
    if new_p.is_dir():
        os.environ["MUSE_WORKSPACE_ROOT"] = str(new_p)
        print(f"[Active workspace switched to: {new_p}]\n")
    else:
        print(f"[Directory not found: {new_ws}]\n")
    return "continue", history, last_reply


def _dispatch_command(prompt_lower: str, prompt: str, model: str,
                     history: list, last_reply: str) -> tuple[str, list, str] | None:
    """Handle a CLI command. Returns None when the prompt was not a command.

    Returns (action, history, last_reply) where action ∈ {"break", "continue", "chat"}.
    """
    if prompt_lower in ("/clear", "clear", "/new", "new"):
        clear_history, clear_reply = _handle_clear()
        return "continue", clear_history, clear_reply
    if _handle_exit(prompt_lower):
        print("Goodbye!")
        return "break", history, last_reply
    if prompt_lower in ("/model", "/models", "model", "models", "/m"):
        _handle_model_view(model)
        return "continue", history, last_reply
    if prompt_lower.startswith(("/model ", "/m ")):
        return "continue", history, _handle_model_switch(prompt)
    if prompt_lower.startswith(("/save", "/write")):
        return "continue", history, _handle_save(last_reply)
    if prompt_lower.startswith(("/open ", "/file ", "/folder ")):
        return _handle_open(prompt, model, history, last_reply)
    if prompt_lower.startswith(("/cd ", "/workspace ")):
        return _handle_cd(prompt, history, last_reply)
    if prompt_lower in ("/status", "status"):
        _handle_status()
        return "continue", history, last_reply
    if prompt_lower in ("/help", "help", "/?"):
        _handle_help()
        return "continue", history, last_reply
    return None


def interactive_chat(model: str = DEFAULT_MODEL):
    """Run an interactive chat session with the selected model."""
    ws = get_default_workspace()
    print("=" * 65)
    print(f"  muse.ai Interactive Chat (model: {model})")
    print(f"  Local file access: Enabled ({ws})")
    print("  Commands: /open <path>, /save <file>, /model <id>, /cd <dir>, exit")
    print("=" * 65 + "\n")
    print(f"  muse.ai Interactive Chat (model: {model})")
    print("  Local file access: Enabled (/Volumes/Drive D/...)")
    print("  Commands: /model, /save <file>, /file <path>, exit")
    print("=" * 60 + "\n")

    history = []
    last_reply = ""
    while True:
        try:
            prompt = input(f"[{model}] You > ").strip()
            if not prompt:
                continue
            prompt_lower = prompt.lower()

            dispatched = _dispatch_command(prompt_lower, prompt, model, history, last_reply)
            if dispatched is not None:
                action, history, last_reply = dispatched
                if action == "break":
                    break
                continue

            history, last_reply = _handle_chat_message(prompt, model, history, last_reply)
        except (KeyboardInterrupt, EOFError):
            print("\nGoodbye!")
            break


def show_status():
    print(f"\nChecking gateway status at {GATEWAY_BASE}...")
    headers = {"Authorization": f"Bearer {ADMIN_KEY}"}
    try:
        h = httpx.get(f"{GATEWAY_BASE}/healthz", timeout=5.0)
        print(f"Gateway Health: {h.status_code} ({h.text})")
        acc = httpx.get(f"{GATEWAY_BASE}/admin/accounts", headers=headers, timeout=5.0).json()
        accounts = acc.get("data", acc) if isinstance(acc, dict) else acc
        for a in accounts:
            print(f"  - Account: {a.get('label')}, Status: {a.get('status')}")
            q = httpx.get(f"{GATEWAY_BASE}/admin/accounts/{a.get('id')}/quota", headers=headers, timeout=10.0)
            if q.status_code == 200:
                qd = q.json().get("quota", {})
                print(f"    Plan: {qd.get('plan')}, Left: {qd.get('extra_left')}")
        print()
    except Exception as e:
        print(f"[Error]: {e}\n")


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
