"""Startup diagnostics for ChromiumProcess: profile-owner detection, port
checks, profile-location warnings and output tails — all with fake process
lists, no real browser."""

import os
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace

from muse2api.drivers.browser import chromium
from muse2api.drivers.browser.chromium import (
    ChromiumProcess,
    ProcessInfo,
    ProcessScan,
    _manual_holder_steps,
    devtools_active_port,
    is_chrome_process,
    parse_process_lines,
    port_in_use,
    profile_dir_warnings,
    profile_owners,
    profile_possible_holders,
    scan_processes,
    tail_lines,
)

PROFILE = Path("C:/muse2api/data/browser-profile")


def _chrome(pid: int, profile: str, extra: str = "") -> ProcessInfo:
    return ProcessInfo(
        pid, f'"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" '
        f"--user-data-dir={profile} {extra}".strip())


def test_profile_owners_matches_the_exact_profile():
    processes = [_chrome(4321, "C:\\muse2api\\data\\browser-profile")]
    assert [p.pid for p in profile_owners(PROFILE, processes)] == [4321]


def test_profile_owners_matches_quoted_and_case_insensitive_paths():
    processes = [_chrome(99, '"c:\\MUSE2API\\data\\browser-profile"')]
    assert [p.pid for p in profile_owners(PROFILE, processes)] == [99]


def test_profile_owners_ignores_other_profiles_and_plain_processes():
    processes = [
        _chrome(1, "C:\\Users\\dong\\AppData\\Local\\Google\\Chrome\\User Data"),
        ProcessInfo(2, "notepad.exe file.txt"),
        ProcessInfo(3, "chrome.exe --type=renderer"),  # no user-data-dir flag
    ]
    assert profile_owners(PROFILE, processes) == []


def test_profile_owners_reports_every_match_and_skips_our_own_pid():
    processes = [
        _chrome(10, "C:\\muse2api\\data\\browser-profile"),
        _chrome(11, "C:\\muse2api\\data\\browser-profile", "--type=gpu-process"),
        _chrome(os.getpid(), "C:\\muse2api\\data\\browser-profile"),
    ]
    assert [p.pid for p in profile_owners(PROFILE, processes)] == [10, 11]


def test_profile_owners_matches_a_relative_profile_against_an_absolute_one(
        tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    processes = [ProcessInfo(7, f"chrome --user-data-dir={tmp_path / 'browser-profile'}")]
    assert [p.pid for p in profile_owners(Path("browser-profile"), processes)] == [7]


def test_parse_process_lines_reads_pid_tab_command_line():
    lines = [
        "1234\t\"C:\\chrome.exe\" --user-data-dir=X",
        "garbage without a tab",
        "NaN\tsome command",
        "555\t",  # no command line (system process)
        "777\tC:\\Windows\\System32\\svchost.exe -k netsvcs",
    ]
    parsed = parse_process_lines(lines)
    assert [(p.pid, p.command_line) for p in parsed] == [
        (1234, '"C:\\chrome.exe" --user-data-dir=X'),
        (777, "C:\\Windows\\System32\\svchost.exe -k netsvcs"),
    ]


def test_devtools_active_port_reads_the_first_line(tmp_path):
    (tmp_path / "DevToolsActivePort").write_text(
        "9222\n/devtools/browser/abc-123\n", encoding="utf-8")
    assert devtools_active_port(tmp_path) == 9222


def test_devtools_active_port_missing_or_garbage_is_none(tmp_path):
    assert devtools_active_port(tmp_path) is None
    (tmp_path / "DevToolsActivePort").write_text("not-a-port\n", encoding="utf-8")
    assert devtools_active_port(tmp_path) is None


def test_port_in_use_detects_a_bound_socket():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        assert port_in_use(port) is True
    assert port_in_use(port) is False  # released with the socket


def test_profile_dir_warnings_flags_the_default_chrome_profile():
    default = Path.home() / ".config" / "google-chrome"
    warnings = profile_dir_warnings(default, data_dir=None)
    assert any("default Chrome profile" in w and "136" in w for w in warnings)


def test_profile_dir_warnings_flags_a_profile_outside_the_data_dir(tmp_path):
    warnings = profile_dir_warnings(tmp_path / "elsewhere" / "profile",
                                    data_dir=tmp_path / "data")
    assert any("not under the muse2api data" in w for w in warnings)


def test_profile_dir_warnings_quiet_for_the_normal_layout(tmp_path):
    data = tmp_path / "data"
    assert profile_dir_warnings(data / "browser-profile", data_dir=data) == []


def test_profile_owners_matches_a_backslash_relative_path_case_insensitively(
        tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    processes = [ProcessInfo(5, "chrome --user-data-dir=DATA\\BROWSER-PROFILE")]
    assert [p.pid for p in profile_owners(Path("data/browser-profile"), processes)] == [5]


def test_is_chrome_process():
    assert is_chrome_process(_chrome(1, "X"))
    assert is_chrome_process(ProcessInfo(2, "chrome --type=renderer"))
    assert is_chrome_process(ProcessInfo(3, "/usr/bin/chromium --headless"))
    assert not is_chrome_process(ProcessInfo(4, "python -m muse2api"))
    assert not is_chrome_process(ProcessInfo(5, "notepad.exe chrome.txt"))


def test_possible_holders_match_on_the_directory_name_only():
    processes = [
        _chrome(10, "D:\\backup\\browser-profile"),  # same name, different root
        _chrome(11, "C:\\muse2api\\data\\browser-profile"),  # the exact owner
        ProcessInfo(12, "python tool.py --watch browser-profile"),  # not Chrome
        _chrome(13, "C:\\other\\profile"),
    ]
    possible = profile_possible_holders(PROFILE, processes)
    assert [p.pid for p in possible] == [10]


def _fake_run(monkeypatch, *, stdout="", stderr="", returncode=0, exc=None):
    def run(argv, **kwargs):
        if exc is not None:
            raise exc
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)
    monkeypatch.setattr(chromium.subprocess, "run", run)


def test_scan_processes_reports_a_timeout_as_inconclusive(monkeypatch):
    _fake_run(monkeypatch, exc=subprocess.TimeoutExpired("powershell", 15))
    scan = scan_processes(platform="win32")
    assert scan.processes == []
    assert scan.error is not None and "timed out" in scan.error


def test_scan_processes_reports_a_powershell_error_with_its_text(monkeypatch):
    _fake_run(monkeypatch, returncode=1, stderr="Get-CimInstance : Access denied\n")
    scan = scan_processes(platform="win32")
    assert scan.processes == []
    assert scan.error is not None and "Access denied" in scan.error


def test_scan_processes_success_counts_chrome(monkeypatch):
    _fake_run(monkeypatch, stdout=(
        "100\t\"C:\\chrome\\chrome.exe\" --user-data-dir=X\n"
        "200\tpython -m muse2api\n"
        "300\t\"C:\\chrome\\chrome.exe\" --type=renderer\n"))
    scan = scan_processes(platform="win32")
    assert scan.error is None
    assert len(scan.processes) == 3
    assert scan.chrome_count == 2


def _failed_process(tmp_path, exit_code, scan, possible):
    proc = ChromiumProcess("chrome", 9222, tmp_path / "browser-profile",
                           data_dir=tmp_path)
    proc.proc = SimpleNamespace(returncode=exit_code)
    proc._scan = scan
    proc._possible = possible
    return proc


def test_failure_message_maps_exit_21_and_names_possible_holders(tmp_path):
    possible = [ProcessInfo(4242, "chrome --user-data-dir=D:\\backup\\browser-profile")]
    proc = _failed_process(tmp_path, 21, ProcessScan([], None), possible)
    message = proc._failure_message(["chrome"])
    assert "RESULT_CODE_PROFILE_IN_USE" in message
    assert "profile is in use by another Chrome process" in message
    assert "4242" in message
    assert "manual steps" not in message


def test_failure_message_exit_21_without_a_holder_prints_manual_steps(tmp_path):
    scan = ProcessScan([_chrome(1, "A"), _chrome(2, "B"), _chrome(3, "C")], None)
    proc = _failed_process(tmp_path, 21, scan, [])
    message = proc._failure_message(["chrome"])
    assert "no holder was identified" in message
    assert "3 chrome.exe" in message
    assert "lsof -i :18610" in message  # POSIX steps on this platform
    assert "browser-profile" in message


def test_failure_message_exit_21_with_an_inconclusive_scan_says_so(tmp_path):
    scan = ProcessScan([], "powershell process scan timed out after 15s")
    proc = _failed_process(tmp_path, 21, scan, [])
    message = proc._failure_message(["chrome"])
    assert "inconclusive" in message
    assert "timed out" in message
    assert "manual steps" in message


def test_failure_message_other_exit_codes_have_no_profile_section(tmp_path):
    proc = _failed_process(tmp_path, 3, ProcessScan([], None), [])
    message = proc._failure_message(["chrome"])
    assert "exit code: 3" in message
    assert "RESULT_CODE_PROFILE_IN_USE" not in message


def test_manual_holder_steps_windows_text(tmp_path):
    steps = _manual_holder_steps(tmp_path / "browser-profile", platform="win32")
    assert "netstat -ano | findstr :18610" in steps
    assert "Get-CimInstance Win32_Process" in steps
    assert "*browser-profile*" in steps
    assert "Resource Monitor" in steps and "Associated Handles" in steps


def test_tail_lines_returns_only_the_last_lines(tmp_path):
    log_file = tmp_path / "chromium-output.log"
    log_file.write_text("\n".join(f"line {i}" for i in range(30)), encoding="utf-8")
    tail = tail_lines(log_file, 5)
    assert tail.splitlines() == [f"line {i}" for i in range(25, 30)]
    assert tail_lines(tmp_path / "missing.log") == ""
