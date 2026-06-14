"""Regression tests for scripts/run_tests.sh environment selection."""

from pathlib import Path


def test_run_tests_sh_skips_virtualenvs_without_pytest() -> None:
    script = (Path(__file__).resolve().parents[2] / "scripts" / "run_tests.sh").read_text(
        encoding="utf-8"
    )

    assert "candidate/bin/python" in script
    assert "import pytest" in script
    assert "continue" in script


def test_run_tests_sh_isolates_hermes_home() -> None:
    script = (Path(__file__).resolve().parents[2] / "scripts" / "run_tests.sh").read_text(
        encoding="utf-8"
    )

    assert "TEST_HERMES_HOME=" in script
    assert 'HERMES_HOME="$TEST_HERMES_HOME"' in script
    assert "trap cleanup EXIT" in script
