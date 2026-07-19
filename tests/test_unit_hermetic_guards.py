import os
import socket
import shutil
import subprocess
import sys
from unittest.mock import patch

import pytest


def test_clear_environment_patch_preserves_isolated_hermes_home(monkeypatch):
    isolated_home = os.environ["HERMES_HOME"]
    monkeypatch.setenv("SHOULD_BE_CLEARED", "yes")

    with patch.dict(os.environ, {}, clear=True):
        assert os.environ["HERMES_HOME"] == isolated_home
        assert "SHOULD_BE_CLEARED" not in os.environ

    assert os.environ["HERMES_HOME"] == isolated_home


@pytest.mark.parametrize("command", [
    [sys.executable, "-m", "pip", "install", "fixture"],
    [sys.executable, "-m", "ensurepip"],
    [sys.executable, "-m", "uv", "pip", "install", "fixture"],
    ["uv", "pip", "install", "fixture"],
    ["uv", "sync"],
    ["uv", "add", "fixture"],
    ["bash", "-lc", "uv add fixture"],
    ["env", "FIXTURE=1", "uv", "sync"],
    ["env", "-u", "PIP_INDEX_URL", "uv", "sync"],
    ["env", "-P", "/usr/local/bin", "uv", "sync"],
    ["sudo", "-u", "root", "uv", "sync"],
    ["pip", "install", "fixture"],
])
def test_package_managers_are_denied(command):
    with pytest.raises(RuntimeError, match="package manager denied"):
        subprocess.Popen(command)


def test_read_only_package_manager_version_probe_is_allowed():
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv not installed")
    result = subprocess.run([uv, "--version"], check=True, capture_output=True, text=True)
    assert result.stdout.startswith("uv ")


@pytest.mark.live_system_guard_bypass
def test_live_system_bypass_does_not_disable_package_guard():
    with pytest.raises(RuntimeError, match="package manager denied"):
        subprocess.Popen(["uv", "sync"])


@pytest.mark.integration
def test_integration_lane_can_run_bounded_package_tool_probe():
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv not installed")
    result = subprocess.run([uv, "--version"], check=True, capture_output=True, text=True)
    assert result.stdout.startswith("uv ")


def test_public_dns_and_direct_nonloopback_connections_are_denied():
    with pytest.raises(socket.gaierror, match="non-loopback DNS denied"):
        socket.getaddrinfo("example.com", 443)
    with pytest.raises(socket.gaierror, match="non-loopback DNS denied"):
        socket.gethostbyname("example.com")
    with pytest.raises(socket.gaierror, match="non-loopback DNS denied"):
        socket.gethostbyname_ex("example.com")
    with pytest.raises(OSError, match="non-loopback connection denied"):
        socket.create_connection(("203.0.113.1", 443), timeout=0.01)

    stream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    datagram = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        with pytest.raises(OSError, match="non-loopback connection denied"):
            stream.connect_ex(("203.0.113.1", 443))
        with pytest.raises(OSError, match="non-loopback connection denied"):
            datagram.sendto(b"fixture", ("203.0.113.1", 443))
    finally:
        stream.close()
        datagram.close()


def test_loopback_resolution_remains_available():
    assert socket.getaddrinfo("127.0.0.1", 0)
    stream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    datagram = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        assert isinstance(stream.connect_ex(("127.0.0.1", 9)), int)
        assert datagram.sendto(b"fixture", ("127.0.0.1", 9)) == 7
    finally:
        stream.close()
        datagram.close()
