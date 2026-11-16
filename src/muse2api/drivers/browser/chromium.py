"""Chromium process management."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

import httpx

log = logging.getLogger(__name__)

_CANDIDATES = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "/usr/bin/chromium",
    "/snap/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)


def find_chromium(configured: str = "") -> str:
    for c in ((configured,) if configured else ()) + _CANDIDATES:
        if c and (os.path.isfile(c) or shutil.which(c)):
            return c
    raise FileNotFoundError(
        "Chromium/Chrome not found; install it or set MUSE2API_CHROMIUM_PATH"
    )


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    command_line: str


_USER_DATA_DIR_RE = re.compile(r"--user-data-dir=(?:\"([^\"]+)\"|(\S+))")


def _norm_path(raw: str) -> str:
    text = raw.strip().strip('"').replace("\\", "/").rstrip("/")
    if len(text) > 1 and text[1] == ":":  # Windows drive path: case-insensitive
        text = text.casefold()
    return text


def _norm_variants(raw: str) -> set[str]:
    variants = {_norm_path(raw)}
    path = Path(raw.strip().strip('"')).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    variants.add(_norm_path(str(path)))
    return variants


def profile_owners(profile_dir: Path, processes: list[ProcessInfo]) -> list[ProcessInfo]:
    """Running processes whose command line points Chromium at ``profile_dir``."""
    targets = _norm_variants(str(profile_dir))
    owners = []
    for proc in processes:
        if proc.pid == os.getpid():
            continue
        match = _USER_DATA_DIR_RE.search(proc.command_line)
        if match and _norm_variants(match.group(1) or match.group(2)) & targets:
            owners.append(proc)
    return owners


def parse_process_lines(lines: list[str]) -> list[ProcessInfo]:
    """Parse '<pid>\\t<command line>' lines (the Windows enumeration format)."""
    out = []
    for line in lines:
        pid_text, sep, cmdline = line.partition("\t")
        if not sep or not cmdline.strip():
            continue
        with contextlib.suppress(ValueError):
            out.append(ProcessInfo(int(pid_text.strip()), cmdline.strip()))
    return out


def list_processes() -> list[ProcessInfo]:
    """Best-effort snapshot of running processes; [] when enumeration fails."""
    try:
        if sys.platform == "win32":
            script = ("Get-CimInstance Win32_Process | "
                      "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")
            done = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, timeout=15)
            return parse_process_lines(done.stdout.splitlines())
        if sys.platform == "darwin":
            done = subprocess.run(["ps", "-axo", "pid=,command="],
                                  capture_output=True, text=True, timeout=15)
            out = []
            for line in done.stdout.splitlines():
                parts = line.strip().split(None, 1)
                if len(parts) == 2 and parts[0].isdigit():
                    out.append(ProcessInfo(int(parts[0]), parts[1]))
            return out
        out = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            with contextlib.suppress(OSError):
                raw = (entry / "cmdline").read_bytes()
                if raw:
                    text = raw.replace(b"\0", b" ").decode("utf-8", "replace")
                    out.append(ProcessInfo(int(entry.name), text))
        return out
    except Exception:
        return []


def devtools_active_port(profile_dir: Path) -> int | None:
    """Port named by the profile's DevToolsActivePort file, if it has one."""
    try:
        first = (profile_dir / "DevToolsActivePort").read_text(
            encoding="utf-8").splitlines()[0]
        return int(first.strip())
    except (OSError, ValueError, IndexError):
        return None


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.socket() as sock:
            sock.bind((host, port))
    except OSError:
        return True
    return False


def _default_profile_dirs() -> list[Path]:
    home = Path.home()
    local = Path(os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local")))
    return [
        local / "Google" / "Chrome" / "User Data",
        local / "Chromium" / "User Data",
        home / "Library" / "Application Support" / "Google" / "Chrome",
        home / "Library" / "Application Support" / "Chromium",
        home / ".config" / "google-chrome",
        home / ".config" / "chromium",
    ]


def profile_dir_warnings(profile_dir: Path, data_dir: Path | None) -> list[str]:
    """Warnings for profile locations that break or endanger the DevTools port."""
    warnings = []
    target = _norm_path(str(profile_dir.resolve()))
    for default in _default_profile_dirs():
        base = _norm_path(str(default))
        if target == base or target.startswith(base + "/"):
            warnings.append(
                f"browser profile dir {profile_dir} is a default Chrome profile; "
                "Chrome 136+ ignores --remote-debugging-port there, so the DevTools "
                "endpoint will never appear. Point MUSE2API_BROWSER_PROFILE_DIR at "
                "a muse2api-owned directory under the data dir.")
            break
    if data_dir is not None:
        root = _norm_path(str(data_dir.resolve()))
        if target != root and not target.startswith(root + "/"):
            warnings.append(
                f"browser profile dir {profile_dir} is not under the muse2api data "
                f"dir {data_dir}; a Chromium from another install may share it.")
    return warnings


def tail_lines(path: Path, count: int = 20) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-count:])


def _stop_hint(pids: list[int]) -> str:
    if sys.platform == "win32":
        return "stop it with: " + "; ".join(f"taskkill /F /PID {pid}" for pid in pids)
    return "stop it with: " + "; ".join(f"kill {pid}" for pid in pids)


class ChromiumProcess:
    def __init__(self, executable: str, port: int, profile_dir: Path, headless: bool = True,
                 proxy: str = "", data_dir: Path | None = None):
        self.executable = executable
        self.port = port
        self.profile_dir = profile_dir
        self.headless = headless
        self.proxy = proxy
        self.data_dir = data_dir
        self.output_log = profile_dir.parent / "chromium-output.log"
        self.proc: asyncio.subprocess.Process | None = None
        self._log_handle: TextIO | None = None

    @property
    def http_base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def browser_ws_url(self) -> str | None:
        try:
            # Local DevTools port must not follow http_proxy/https_proxy.
            async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
                r = await client.get(f"{self.http_base}/json/version")
                return r.json().get("webSocketDebuggerUrl")
        except (httpx.HTTPError, ValueError):
            return None

    async def start(self) -> str:
        """Start (or attach to an already running) Chromium; return the browser ws URL."""
        if url := await self.browser_ws_url():
            log.info("attached to existing Chromium on port %s", self.port)
            return url

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        for warning in profile_dir_warnings(self.profile_dir, self.data_dir):
            log.warning("%s", warning)

        owners = profile_owners(
            self.profile_dir, await asyncio.to_thread(list_processes))
        if owners:
            pids = [p.pid for p in owners]
            active = devtools_active_port(self.profile_dir)
            detail = (f" (its DevToolsActivePort file names port {active})"
                      if active else "")
            raise RuntimeError(
                f"Chromium is already running with profile {self.profile_dir} "
                f"(PID {', '.join(map(str, pids))}){detail} but does not answer on "
                f"port {self.port} — a stale gateway/Chromium from a previous run "
                "holds the profile, so a new instance hands off to it and exits "
                f"without opening the debug port. {_stop_hint(pids)}, then start "
                "again.")
        if port_in_use(self.port):
            raise RuntimeError(
                f"TCP port {self.port} is already in use by another process, so "
                "Chromium cannot open its DevTools endpoint there; free the port "
                "or set a different MUSE2API_CDP_PORT.")

        for lock in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            with contextlib.suppress(FileNotFoundError):
                (self.profile_dir / lock).unlink()

        args = [
            self.executable,
            f"--remote-debugging-port={self.port}",
            "--remote-allow-origins=*",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-dev-shm-usage",
            "--disable-background-networking",
            "--autoplay-policy=no-user-gesture-required",
            "--window-size=1440,2200",
            "about:blank",
        ]
        if self.proxy:
            args.insert(1, f"--proxy-server={self.proxy}")
        if self.headless:
            args[1:1] = ["--headless=new", "--disable-gpu"]
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            args.insert(1, "--no-sandbox")

        log.info("launching Chromium: %s", subprocess.list2cmdline(args))
        self._log_handle = await asyncio.to_thread(
            self.output_log.open, "a", encoding="utf-8")
        self.proc = await asyncio.create_subprocess_exec(
            *args, stdout=self._log_handle, stderr=asyncio.subprocess.STDOUT
        )
        for _ in range(60):
            if url := await self.browser_ws_url():
                return url
            if self.proc.returncode is not None:
                break
            await asyncio.sleep(0.5)
        raise RuntimeError(self._failure_message(args))

    def _failure_message(self, args: list[str]) -> str:
        if self._log_handle:
            self._log_handle.flush()
        exit_code = self.proc.returncode if self.proc else None
        tail = tail_lines(self.output_log)
        return (
            "Chromium did not expose a DevTools endpoint\n"
            f"  command: {subprocess.list2cmdline(args)}\n"
            f"  user-data-dir: {self.profile_dir}\n"
            f"  remote-debugging-port: {self.port}\n"
            f"  exit code: {exit_code if exit_code is not None else 'still running when the wait timed out'}\n"
            f"  chromium output (last lines of {self.output_log}):\n"
            f"{tail or '(no output captured)'}"
        )

    async def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 10)
            except asyncio.TimeoutError:
                self.proc.kill()
        self.proc = None
        if self._log_handle:
            self._log_handle.close()
            self._log_handle = None
