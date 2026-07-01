"""setup_logging must never open the LIVE ~/.hermes/logs from inside pytest.

2026-07-01: a test run (model=test/model, session 20260701_023616) wrote 89
TypeError tracebacks into the real ~/.hermes/logs/errors.log, which feeds the
HQ ops log digest and flipped the dashboard to DEGRADED. Per-test HERMES_HOME
isolation exists in conftest, but any harness that initializes logging while
resolving the real home (import-time init, direct script invocation, a
subprocess) bypasses it. This fences at the sink: under pytest
(PYTEST_CURRENT_TEST set), a resolved home equal to the real ~/.hermes is
diverted to a scratch dir; explicit non-live homes are untouched.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import hermes_logging


def test_live_home_is_diverted_under_pytest():
    # PYTEST_CURRENT_TEST is set for us by pytest itself.
    live_home = Path.home() / ".hermes"
    log_dir = hermes_logging.setup_logging(hermes_home=live_home, force=True)
    assert log_dir != live_home / "logs", (
        "pytest-context setup_logging wrote handlers into the LIVE ops logs; "
        "test noise will pollute the HQ log digest again"
    )
    assert "hermes-pytest-logs" in str(log_dir)


def test_explicit_scratch_home_is_untouched(tmp_path):
    log_dir = hermes_logging.setup_logging(hermes_home=tmp_path, force=True)
    assert log_dir == tmp_path / "logs"
