"""Startup diagnostics for ChromiumProcess: profile-owner detection, port
checks, profile-location warnings and output tails — all with fake process
lists, no real browser."""

import os
import socket
from pathlib import Path

from muse2api.drivers.browser.chromium import (
    ProcessInfo,
    devtools_active_port,
    parse_process_lines,
    port_in_use,
    profile_dir_warnings,
    profile_owners,
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


def test_tail_lines_returns_only_the_last_lines(tmp_path):
    log_file = tmp_path / "chromium-output.log"
    log_file.write_text("\n".join(f"line {i}" for i in range(30)), encoding="utf-8")
    tail = tail_lines(log_file, 5)
    assert tail.splitlines() == [f"line {i}" for i in range(25, 30)]
    assert tail_lines(tmp_path / "missing.log") == ""
