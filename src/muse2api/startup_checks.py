"""Pre-flight checks run before the server (and its Chromium) starts."""

from __future__ import annotations

import re
import socket
import subprocess
import sys

_NETSTAT_LISTENING_RE = re.compile(
    r"^\s*TCP\s+\S+:(?P<port>\d+)\s+\S+\s+LISTENING\s+(?P<pid>\d+)", re.IGNORECASE)
_SS_PID_RE = re.compile(r"pid=(\d+)")


def parse_netstat_listeners(output: str, port: int) -> int | None:
    """PID listening on ``port`` from `netstat -ano` output, if shown."""
    for line in output.splitlines():
        match = _NETSTAT_LISTENING_RE.match(line)
        if match and int(match.group("port")) == port:
            return int(match.group("pid"))
    return None


def _run_quiet(argv: list[str], timeout: float = 10) -> str:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return done.stdout if done.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def listener_pid(port: int) -> int | None:
    """Best-effort PID of the process listening on ``port``; None if unknown."""
    if sys.platform == "win32":
        return parse_netstat_listeners(_run_quiet(["netstat", "-ano"]), port)
    ss_output = _run_quiet(["ss", "-ltnp"])
    for line in ss_output.splitlines():
        if f":{port} " in line or f":{port}\t" in line:
            match = _SS_PID_RE.search(line)
            if match:
                return int(match.group(1))
    lsof_output = _run_quiet(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"])
    for token in lsof_output.split():
        if token.isdigit():
            return int(token)
    return None


def _bindable(host: str, port: int) -> bool:
    try:
        with socket.socket() as sock:
            sock.bind((host, port))
    except OSError:
        return False
    return True


def gateway_port_problem(host: str, port: int) -> str | None:
    """A ready-to-print fatal message when the gateway port is taken, else None.

    A stale gateway from a previous run is the usual listener — and the usual
    parent of a stale Chromium holding the browser profile — so name its PID
    and fail before anything else starts.
    """
    probe_host = host if host not in ("", "0.0.0.0", "::") else "127.0.0.1"
    if _bindable(probe_host, port):
        return None
    pid = listener_pid(port)
    if pid is not None:
        stop = f"taskkill /F /PID {pid}" if sys.platform == "win32" else f"kill {pid}"
        return (f"muse2api cannot start: {probe_host}:{port} is already listening "
                f"(PID {pid}) — a stale gateway from a previous run is the usual "
                f"cause; stop it with {stop}, or set a different MUSE2API_PORT.")
    return (f"muse2api cannot start: {probe_host}:{port} is already in use "
            "(the listening process could not be identified); stop it or set a "
            "different MUSE2API_PORT.")
