"""Gateway-port pre-flight: netstat parsing and the fail-fast message."""

import socket

from muse2api import startup_checks
from muse2api.startup_checks import gateway_port_problem, parse_netstat_listeners

NETSTAT = """\
Active Connections

  Proto  Local Address          Foreign Address        State           PID
  TCP    127.0.0.1:18610        0.0.0.0:0              LISTENING       31337
  TCP    127.0.0.1:18610        127.0.0.1:51000        TIME_WAIT       0
  TCP    0.0.0.0:8080           0.0.0.0:0              LISTENING       4444
  UDP    0.0.0.0:18610          *:*                                    9999
"""


def test_parse_netstat_listeners_finds_the_listening_pid():
    assert parse_netstat_listeners(NETSTAT, 18610) == 31337
    assert parse_netstat_listeners(NETSTAT, 8080) == 4444
    assert parse_netstat_listeners(NETSTAT, 9999) is None


def test_gateway_port_problem_none_when_the_port_is_free():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        free_port = sock.getsockname()[1]
    assert gateway_port_problem("127.0.0.1", free_port) is None


def test_gateway_port_problem_names_the_listener_pid(monkeypatch):
    monkeypatch.setattr(startup_checks, "listener_pid", lambda port: 31337)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        busy_port = sock.getsockname()[1]
        problem = gateway_port_problem("127.0.0.1", busy_port)
    assert problem is not None
    assert str(busy_port) in problem
    assert "PID 31337" in problem
    assert "stale gateway" in problem


def test_gateway_port_problem_without_an_identifiable_pid(monkeypatch):
    monkeypatch.setattr(startup_checks, "listener_pid", lambda port: None)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        busy_port = sock.getsockname()[1]
        problem = gateway_port_problem("127.0.0.1", busy_port)
    assert problem is not None
    assert "already in use" in problem
    assert "PID" not in problem
