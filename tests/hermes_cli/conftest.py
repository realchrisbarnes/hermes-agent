"""Fixtures shared across hermes_cli kanban tests."""

from __future__ import annotations

import sys

import pytest

# Fresh-process emulation for single-process runs: reset known process-global
# state (module identity + registries) at each test-file boundary so plain
# ``pytest tests/hermes_cli/`` matches the per-file subprocess runner.
from tests._global_state import _fresh_process_globals  # noqa: F401


@pytest.fixture(autouse=True)
def _isolate_frozen_home_paths(monkeypatch):
    """Re-pin module-level paths frozen from HERMES_HOME at import time.

    Several modules compute a home-derived path *once* at import
    (``hermes_state.DEFAULT_DB_PATH``, ``cron.jobs.CRON_DIR`` / ``JOBS_FILE``,
    …). The first test in the session to import the module freezes that path to
    the home current then; every later test that uses the default then shares
    that one location, defeating the per-test HERMES_HOME isolation:

      * ``SessionDB()`` reads/writes one shared ``state.db`` — ``console_engine``'s
        ``sessions list`` shows another file's telegram sessions and buries its
        own past ``--limit``.
      * ``cron.jobs`` reads/writes one shared ``jobs.json`` — ``cron resume
        <name>`` errors when jobs from other files collide by name.

    Re-pinning them to the active home for each test restores the fresh-process
    behavior (where the default always resolves to the current home). Each block
    is a no-op when its module isn't imported — its later lazy import resolves
    the current home anyway.
    """
    from hermes_constants import get_hermes_home

    home = get_hermes_home()

    hermes_state = sys.modules.get("hermes_state")
    if hermes_state is not None:
        monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", home / "state.db", raising=False)

    cron_jobs = sys.modules.get("cron.jobs")
    if cron_jobs is not None:
        hermes_dir = home.resolve()
        cron_dir = hermes_dir / "cron"
        for _name, _path in (
            ("HERMES_DIR", hermes_dir),
            ("CRON_DIR", cron_dir),
            ("JOBS_FILE", cron_dir / "jobs.json"),
            ("OUTPUT_DIR", cron_dir / "output"),
            ("TICKER_HEARTBEAT_FILE", cron_dir / "ticker_heartbeat"),
            ("TICKER_SUCCESS_FILE", cron_dir / "ticker_last_success"),
        ):
            monkeypatch.setattr(cron_jobs, _name, _path, raising=False)

    cron_suggestions = sys.modules.get("cron.suggestions")
    if cron_suggestions is not None:
        monkeypatch.setattr(
            cron_suggestions, "CRON_DIR", home.resolve() / "cron", raising=False
        )
    yield


# Modules that some tests reload IN PLACE (``importlib.reload``) to re-run
# import-time logic. Reload rebinds the module's classes/functions to NEW
# objects in the SAME module dict, so a later file that imported a name by
# value at collection (``from hermes_cli.main import _UpdateOutputStream``)
# keeps the OLD object while functions reading the module dict produce the
# NEW one — an ``isinstance`` mismatch. Identity-restore can't help (same
# module object); we restore the module dict's original bindings instead.
_RELOAD_IN_PLACE_TARGETS = (
    "hermes_cli.main",
    "hermes_constants",
    "hermes_cli.config",
    "agent.curator",
)
_RELOADED_MODULE_DICT_SNAPSHOT: "dict[str, dict]" = {}


@pytest.fixture(autouse=True, scope="module")
def _restore_reloaded_module_dicts():
    """Restore reloaded-in-place module bindings at each test-FILE boundary.

    First call snapshots the collection-time bindings (before any fixture-
    driven reload has run); later calls re-apply them so each file starts with
    the original class/function objects, matching the per-file subprocess runner.
    Scoped to hermes_cli on purpose — the reloaders all live here, and this
    leaves the already-green directories' shared helper untouched.
    """
    if not _RELOADED_MODULE_DICT_SNAPSHOT:
        for _name in _RELOAD_IN_PLACE_TARGETS:
            _mod = sys.modules.get(_name)
            if _mod is not None:
                _RELOADED_MODULE_DICT_SNAPSHOT[_name] = dict(_mod.__dict__)
    else:
        for _name, _snap in _RELOADED_MODULE_DICT_SNAPSHOT.items():
            _mod = sys.modules.get(_name)
            if _mod is not None:
                _mod.__dict__.update(_snap)
    yield


@pytest.fixture
def all_assignees_spawnable(monkeypatch):
    """Pretend every assignee maps to a real Hermes profile.

    Most dispatcher tests use synthetic assignees ("alice", "bob") that
    don't correspond to actual profile directories on disk. Without this
    patch, the dispatcher's profile-exists guard (PR #20105) routes
    those tasks into ``skipped_nonspawnable`` instead of spawning, which
    would break tests that assert spawn behavior.
    """
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


@pytest.fixture(autouse=True)
def _suppress_concurrent_hermes_gate(request, monkeypatch):
    """Default ``_detect_concurrent_hermes_instances`` to ``[]`` for every test.

    The Windows update path now refuses to proceed when another
    ``hermes.exe`` is detected (issue #26670). On a developer's Windows
    machine running the test suite via ``hermes`` itself, this would
    flag the running agent as a concurrent instance and abort every
    ``cmd_update`` test. Tests that want to exercise the gate explicitly
    re-patch ``_detect_concurrent_hermes_instances`` with their own
    return value — autouse here gives a clean default without touching
    the rest of the suite.

    Tests that need to call the REAL function (e.g. unit tests for the
    helper itself) opt out with ``@pytest.mark.real_concurrent_gate``.
    """
    if request.node.get_closest_marker("real_concurrent_gate"):
        return
    try:
        from hermes_cli import main as _cli_main
    except Exception:
        return
    # raising=False: under pytest's per-test spawn isolation, a concurrent
    # xdist worker importing a module that transitively touches hermes_cli.main
    # can briefly expose a partially-initialized module object here — one where
    # _detect_concurrent_hermes_instances isn't defined yet. A bare setattr
    # would raise AttributeError and error the (unrelated) test. The attribute
    # always exists once main.py finishes importing, so a no-op when it's
    # transiently absent is the correct, race-free default.
    monkeypatch.setattr(
        _cli_main,
        "_detect_concurrent_hermes_instances",
        lambda *_a, **_k: [],
        raising=False,
    )
