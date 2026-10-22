"""Tests for the optional Kdenlive bridge.

The bridge is what lets the MCP reach a Kdenlive running on another machine --
notably Kdenlive on Windows while the MCP runs inside WSL. These tests drive a
real bridge over HTTP so the request shape and the authentication are checked
rather than assumed.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from kdenlive_mcp.bridge import BridgeClient
from kdenlive_mcp.errors import BridgeError

BRIDGE = Path(__file__).resolve().parents[1] / "bridge" / "kdenlive_bridge.py"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _start_bridge(port: int, token: str = "") -> subprocess.Popen:
    environment = os.environ.copy()
    environment["KDENLIVE_BRIDGE_TOKEN"] = token
    process = subprocess.Popen(  # noqa: S603
        [sys.executable, str(BRIDGE), "--port", str(port), "--host", "127.0.0.1"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=environment,
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return process
        except OSError:
            if process.poll() is not None:
                raise RuntimeError("the bridge exited during startup") from None
            time.sleep(0.2)
    process.kill()
    raise RuntimeError("the bridge did not start in time")


def _launch(token: str = "") -> tuple[subprocess.Popen, str]:
    """Start a bridge, retrying on a fresh port if the first one is taken.

    Finding a free port and then binding it is a race, and on a busy CI
    runner it loses often enough to matter.
    """
    last_error: Exception | None = None
    for _ in range(5):
        port = _free_port()
        try:
            process = _start_bridge(port, token)
        except RuntimeError as exc:
            last_error = exc
            continue
        return process, f"http://127.0.0.1:{port}"
    raise RuntimeError(f"could not start the bridge: {last_error}")


@pytest.fixture
def bridge():
    process, url = _launch()
    try:
        yield url
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


@pytest.fixture
def secure_bridge():
    process, url = _launch(token="s3cr3t")
    try:
        yield url
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def test_bridge_health(bridge):
    client = BridgeClient(bridge)
    info = client.health()
    assert info.reachable is True
    assert info.url == bridge


def test_bridge_lists_projects(bridge, tmp_path: Path):
    project = tmp_path / "demo.kdenlive"
    project.write_text("<mlt/>")
    client = BridgeClient(bridge)
    projects = client.list_projects(str(tmp_path))
    assert any(entry["name"] == "demo.kdenlive" for entry in projects)


def test_bridge_reads_and_writes_files(bridge, tmp_path: Path):
    target = tmp_path / "note.txt"
    client = BridgeClient(bridge)
    client.write_file(str(target), "hello")
    assert target.read_text() == "hello"
    assert client.read_file(str(target)) == "hello"


def test_bridge_translates_paths(bridge):
    client = BridgeClient(bridge)
    assert client.native_path("/tmp/x") == "/tmp/x"


def test_bridge_rejects_requests_without_a_token(secure_bridge):
    request = urllib.request.Request(f"{secure_bridge}/projects")  # noqa: S310
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(request, timeout=10)  # noqa: S310
    assert exc.value.code == 401


def test_client_sends_the_token(monkeypatch, secure_bridge):
    monkeypatch.setenv("KDENLIVE_MCP_BRIDGE_TOKEN", "s3cr3t")
    client = BridgeClient(secure_bridge)
    assert client.health().reachable is True


def test_client_without_the_token_cannot_use_the_bridge(monkeypatch, secure_bridge):
    monkeypatch.delenv("KDENLIVE_MCP_BRIDGE_TOKEN", raising=False)
    client = BridgeClient(secure_bridge)
    assert client.health().reachable is False


def test_bridge_render_reports_a_missing_project(bridge, tmp_path: Path):
    client = BridgeClient(bridge)
    with pytest.raises(BridgeError):
        client.render(str(tmp_path / "nope.kdenlive"), str(tmp_path / "out.mp4"))


def test_bridge_open_reports_a_missing_project(bridge, tmp_path: Path):
    client = BridgeClient(bridge)
    with pytest.raises(BridgeError):
        client.open_in_kdenlive(str(tmp_path / "nope.kdenlive"))


def test_health_payload_shape(bridge):
    with urllib.request.urlopen(f"{bridge}/health", timeout=10) as response:  # noqa: S310
        payload = json.loads(response.read())
    assert payload["ok"] is True
    assert "platform" in payload
    assert "python" in payload
