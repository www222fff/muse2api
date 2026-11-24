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
        if c and (Path(c).is_file() or shutil.which(c)):
            return c
    message = "Chromium/Chrome not found; install it or set MUSE2API_CHROMIUM_PATH"
    raise FileNotFoundError(message)


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    command_line: str


_USER_DATA_DIR_RE = re.compile(r"--user-data-dir=(?:\"([^\"]+)\"|(\S+))")


def _norm_path(raw: str) -> str:
    text = raw.strip().strip('"').replace("\\", "/").rstrip("/")
    if "\\" in raw or (len(text) > 1 and text[1] == ":"):
        text = text.casefold()  # Windows-style path: case-insensitive
    return text


def _norm_variants(raw: str) -> set[str]:
    variants = {_norm_path(raw)}
    path = Path(raw.strip().strip('"')).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    resolved = _norm_path(str(path))
    variants.add(resolved)
    # A holder must never be missed over case alone (worst case on a truly
    # case-sensitive filesystem: a false positive with a clear message).
    variants.add(resolved.casefold())
    return variants


def is_chrome_process(proc: ProcessInfo) -> bool:
    cmdline = proc.command_line.lower()
    tokens = cmdline.split()
    exe = tokens[0].strip('"').replace("\\", "/").rsplit("/", 1)[-1] if tokens else ""
    return (exe in ("chrome", "chromium", "chrome.exe", "chromium.exe",
                    "google-chrome", "google-chrome-stable")
            or "chrome.exe" in cmdline or "chromium.exe" in cmdline)


def profile_possible_holders(profile_dir: Path,
                             processes: list[ProcessInfo]) -> list[ProcessInfo]:
    """Chrome processes that are not exact owners but whose command line
    mentions the profile directory name — possible holders, worth naming
    when Chrome later reports the profile as in use."""
    name = profile_dir.name.casefold()
    if not name:
        return []
    owners = {p.pid for p in profile_owners(profile_dir, processes)}
    return [p for p in processes
            if p.pid not in owners and is_chrome_process(p)
            and name in p.command_line.replace("\\", "/").casefold()]


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


@dataclass
class ProcessScan:
    """Outcome of a process enumeration: the processes found, and — kept
    separate — the error when the scan itself failed or was inconclusive,
    so 'no holder found' is never confused with 'could not look'."""

    processes: list[ProcessInfo]
    error: str | None = None

    @property
    def chrome_count(self) -> int:
        return sum(1 for p in self.processes if is_chrome_process(p))


def _run_scan_command(argv: list[str], timeout: float = 15) -> tuple[str, str | None]:
    """Run an enumeration command; (stdout, error) — error is None on success."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "", f"{argv[0]} process scan timed out after {timeout:g}s"
    except OSError as exc:
        return "", f"{argv[0]} process scan could not run: {exc}"
    if done.returncode != 0:
        tail = (done.stderr or "").strip().splitlines()
        detail = tail[-1] if tail else f"exit code {done.returncode}"
        return "", f"{argv[0]} process scan failed: {detail}"
    return done.stdout, None


def scan_processes(platform: str | None = None) -> ProcessScan:
    """Snapshot running processes, reporting scan failure instead of hiding it."""
    platform = platform or sys.platform
    if platform == "win32":
        script = ("Get-CimInstance Win32_Process | "
                  "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")
        stdout, error = _run_scan_command(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script])
        return ProcessScan(parse_process_lines(stdout.splitlines()), error)
    if platform == "darwin":
        stdout, error = _run_scan_command(["ps", "-axo", "pid=,command="])
        out = []
        for line in stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and parts[0].isdigit():
                out.append(ProcessInfo(int(parts[0]), parts[1]))
        return ProcessScan(out, error)
    try:
        entries = list(Path("/proc").iterdir())
    except OSError as exc:
        return ProcessScan([], f"/proc process scan could not run: {exc}")
    out = []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        with contextlib.suppress(OSError):
            raw = (entry / "cmdline").read_bytes()
            if raw:
                text = raw.replace(b"\0", b" ").decode("utf-8", "replace")
                out.append(ProcessInfo(int(entry.name), text))
    return ProcessScan(out)


def list_processes() -> list[ProcessInfo]:
    return scan_processes().processes


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


_EXIT_CODE_MEANINGS = {21: "RESULT_CODE_PROFILE_IN_USE"}


def _manual_holder_steps(profile_dir: Path, platform: str | None = None) -> str:
    name = profile_dir.name
    if (platform or sys.platform) == "win32":
        return (
            "  manual steps to find the holder:\n"
            "    1. netstat -ano | findstr :18610 — a stale gateway listening on "
            "the gateway port (default 18610) is the usual parent of the stale "
            "Chrome; stop it with taskkill /F /PID <pid>.\n"
            "    2. PowerShell: Get-CimInstance Win32_Process | Where-Object "
            f"{{ $_.CommandLine -like '*{name}*' }} — lists processes whose "
            "command line uses the profile.\n"
            "    3. Resource Monitor (resmon.exe) → CPU tab → Associated Handles "
            f"→ search '{name}' — shows which process holds the profile folder."
        )
    return (
        "  manual steps to find the holder:\n"
        "    1. lsof -i :18610 — a stale gateway listening on the gateway port "
        "(default 18610) is the usual parent of the stale Chrome; stop it with "
        "kill <pid>.\n"
        f"    2. ps aux | grep {name} — lists processes using the profile.\n"
        f"    3. lsof +D {profile_dir} — shows which process holds the folder."
    )


class ChromiumProcess:
    def __init__(self, executable: str, port: int, profile_dir: Path, *,
                 headless: bool = True, proxy: str = "", data_dir: Path | None = None):
        self.executable = executable
        self.port = port
        self.profile_dir = profile_dir
        self.headless = headless
        self.proxy = proxy
        self.data_dir = data_dir
        self.output_log = profile_dir.parent / "chromium-output.log"
        self.proc: asyncio.subprocess.Process | None = None
        self._log_handle: TextIO | None = None
        self._scan: ProcessScan | None = None
        self._possible: list[ProcessInfo] = []

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

    def _chrome_args(self) -> list[str]:
        """Command-line arguments for the Chromium process."""
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
        return args

    def _clear_profile_locks(self) -> None:
        for lock in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            with contextlib.suppress(FileNotFoundError):
                (self.profile_dir / lock).unlink()

    def _preflight(self) -> None:
        """Fail fast, with the cause, when the profile or the debug port is taken."""
        owners = profile_owners(self.profile_dir, self._scan.processes if self._scan else [])
        if owners:
            pids = [p.pid for p in owners]
            active = devtools_active_port(self.profile_dir)
            detail = (f" (its DevToolsActivePort file names port {active})"
                      if active else "")
            message = (
                f"Chromium is already running with profile {self.profile_dir} "
                f"(PID {', '.join(map(str, pids))}){detail} but does not answer on "
                f"port {self.port} — a stale gateway/Chromium from a previous run "
                "holds the profile, so a new instance hands off to it and exits "
                f"without opening the debug port. {_stop_hint(pids)}, then start "
                "again.")
            raise RuntimeError(message)
        if port_in_use(self.port):
            message = (
                f"TCP port {self.port} is already in use by another process, so "
                "Chromium cannot open its DevTools endpoint there; free the port "
                "or set a different MUSE2API_CDP_PORT.")
            raise RuntimeError(message)

    async def _scan_holders(self) -> None:
        self._scan = await asyncio.to_thread(scan_processes)
        owners = profile_owners(self.profile_dir, self._scan.processes)
        self._possible = profile_possible_holders(self.profile_dir, self._scan.processes)
        if self._scan.error:
            log.warning("profile holder scan inconclusive: %s", self._scan.error)
        else:
            log.info("profile holder scan: %d processes, %d chrome.exe, "
                     "%d exact holder(s), %d possible",
                     len(self._scan.processes), self._scan.chrome_count,
                     len(owners), len(self._possible))
        if self._possible:
            log.warning("possible profile holders (command line mentions '%s'): "
                        "PID %s", self.profile_dir.name,
                        ", ".join(str(p.pid) for p in self._possible))

    async def start(self) -> str:
        """Start (or attach to an already running) Chromium; return the browser ws URL."""
        if url := await self.browser_ws_url():
            log.info("attached to existing Chromium on port %s", self.port)
            return url

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        for warning in profile_dir_warnings(self.profile_dir, self.data_dir):
            log.warning("%s", warning)
        await self._scan_holders()
        self._preflight()
        self._clear_profile_locks()

        args = self._chrome_args()
        log.info("launching Chromium: %s", subprocess.list2cmdline(args))
        self._log_handle = await asyncio.to_thread(
            self.output_log.open, "a", encoding="utf-8")
        self.proc = await asyncio.create_subprocess_exec(
            *args, stdout=self._log_handle, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        for _ in range(60):
            if url := await self.browser_ws_url():
                return url
            if self.proc.returncode is not None:
                break
            await asyncio.sleep(0.5)
        raise RuntimeError(self._failure_message(args))

    def _profile_in_use_section(self) -> str:
        lines = ["Chromium exited with code 21 (RESULT_CODE_PROFILE_IN_USE): "
                 "the profile is in use by another Chrome process."]
        if self._possible:
            lines.append("  possible holder PID(s): "
                         + ", ".join(str(p.pid) for p in self._possible))
            return "\n".join(lines) + "\n"
        scan = self._scan
        if scan is not None and scan.error:
            lines.append(
                "  no holder was identified — the pre-launch scan was "
                f"inconclusive ({scan.error}), so a holder may have been missed.")
        elif scan is not None:
            lines.append(
                "  no holder was identified — the pre-launch scan covered "
                f"{scan.chrome_count} chrome.exe process(es) and found none; "
                "the holder may have started after the scan or hold the profile "
                "without a matching command line.")
        else:
            lines.append("  no holder was identified — no pre-launch scan ran.")
        lines.append(_manual_holder_steps(self.profile_dir))
        return "\n".join(lines) + "\n"

    def _failure_message(self, args: list[str]) -> str:
        if self._log_handle:
            self._log_handle.flush()
        exit_code = self.proc.returncode if self.proc else None
        tail = tail_lines(self.output_log)
        if exit_code is None:
            code_text = "still running when the wait timed out"
        elif exit_code in _EXIT_CODE_MEANINGS:
            code_text = f"{exit_code} ({_EXIT_CODE_MEANINGS[exit_code]})"
        else:
            code_text = str(exit_code)
        head = self._profile_in_use_section() if exit_code == 21 else ""
        return (
            head
            + "Chromium did not expose a DevTools endpoint\n"
            f"  command: {subprocess.list2cmdline(args)}\n"
            f"  user-data-dir: {self.profile_dir}\n"
            f"  remote-debugging-port: {self.port}\n"
            f"  exit code: {code_text}\n"
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
