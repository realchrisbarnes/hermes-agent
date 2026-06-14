"""Tests for the source-tree ``./hermes`` launcher."""

from __future__ import annotations

import runpy
import sys
import types
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_launcher():
    return runpy.run_path(str(PROJECT_ROOT / "hermes"), run_name="hermes_launcher_test")


def test_launcher_delegates_to_argparse_entrypoint(monkeypatch):
    """``./hermes`` should use ``hermes_cli.main``, not the legacy Fire wrapper."""
    launcher_path = PROJECT_ROOT / "hermes"
    called = []

    fake_main_module = types.ModuleType("hermes_cli.main")

    def fake_main():
        called.append("hermes_cli.main")

    fake_main_module.main = fake_main
    monkeypatch.setitem(sys.modules, "hermes_cli.main", fake_main_module)

    fake_cli_module = types.ModuleType("cli")

    def legacy_cli_main(*args, **kwargs):
        raise AssertionError("launcher should not import cli.main")

    fake_cli_module.main = legacy_cli_main
    monkeypatch.setitem(sys.modules, "cli", fake_cli_module)

    fake_fire_module = types.ModuleType("fire")

    def legacy_fire(*args, **kwargs):
        raise AssertionError("launcher should not invoke fire.Fire")

    fake_fire_module.Fire = legacy_fire
    monkeypatch.setitem(sys.modules, "fire", fake_fire_module)

    monkeypatch.setattr(sys, "argv", [str(launcher_path), "gateway", "status"])

    runpy.run_path(str(launcher_path), run_name="__main__")

    assert called == ["hermes_cli.main"]


def test_launcher_reexecs_repo_venv_when_current_python_is_too_old(monkeypatch):
    launcher = _load_launcher()

    class Reexec(Exception):
        def __init__(self, path, args):
            self.path = path
            self.args = args

    class Probe:
        returncode = 0

    def fake_exists(path):
        return str(path).endswith("/.venv/bin/python")

    def fake_realpath(path):
        return str(path)

    def fake_execv(path, args):
        raise Reexec(path, args)

    monkeypatch.setattr(launcher["sys"], "version_info", (3, 9, 6))
    monkeypatch.setattr(launcher["sys"], "executable", "/usr/bin/python3")
    monkeypatch.setattr(launcher["sys"], "argv", ["./hermes", "doctor"])
    monkeypatch.setattr(launcher["os"].path, "exists", fake_exists)
    monkeypatch.setattr(launcher["os"].path, "realpath", fake_realpath)
    monkeypatch.setattr(launcher["subprocess"], "run", lambda *a, **k: Probe())
    monkeypatch.setattr(launcher["os"], "execv", fake_execv)

    with pytest.raises(Reexec) as exc:
        launcher["_reexec_supported_venv_python"]()

    assert exc.value.path.endswith("/.venv/bin/python")
    assert exc.value.args[-1] == "doctor"


def test_launcher_does_not_probe_when_current_python_is_supported(monkeypatch):
    launcher = _load_launcher()

    def fail_probe(*args, **kwargs):
        raise AssertionError("supported Python should not probe virtualenvs")

    monkeypatch.setattr(launcher["sys"], "version_info", (3, 11, 0))
    monkeypatch.setattr(launcher["subprocess"], "run", fail_probe)

    launcher["_reexec_supported_venv_python"]()
