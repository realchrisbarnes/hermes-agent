"""Fresh-process emulation for single-process directory runs.

``scripts/run_tests_parallel.py`` gives every test FILE its own
interpreter, so process-global state (module-level dicts, caches,
ContextVars, and the identity of imported modules themselves) starts
fresh for each file.  A plain ``pytest tests/<dir>/`` run shares ONE
process across all files, and state legitimately written by one file's
tests shadows the fixtures of later files.

``reset_cross_file_globals()`` restores that state to fresh-process
condition at each test-FILE boundary.  Directory conftests wire it in by
importing the module-scoped autouse ``_fresh_process_globals`` fixture
below, so direct single-process runs match the canonical per-file
subprocess runner.

Two classes of cross-file leakage are handled:

1. **Module-identity leakage.**  A file that does
   ``del sys.modules["agent.foo"]`` + re-import (to exercise import-time
   behavior) leaves a NEW module object in ``sys.modules``.  Any later
   test file that bound a name from that module at collection time
   (``from agent.model_metadata import get_model_context_length``) keeps
   referencing the OLD object, so its
   ``@patch("agent.model_metadata.fetch_model_metadata")`` targets the
   NEW object and silently misses.  ``_restore_first_party_modules()``
   re-points first-party ``sys.modules`` entries back to the objects
   present at collection time — the same objects later files closed over.

2. **Value leakage.**  Module-level registries / TTL caches / ContextVars
   accumulate values on the (now identity-stable) module object.  The
   targeted resets below restore them.

Module-scoped on purpose: intra-file ordering stays the test author's
responsibility (matching the per-file subprocess runner); only
cross-FILE leakage is neutralized.

Only touch modules that are already imported: an unimported module has
no leaked state, and importing it here would change what "fresh process"
means for the file under test.
"""

import importlib
import sys
from unittest import mock as _mock

import pytest


# First-party top-level package roots whose module identity must stay
# stable across files.  ``del sys.modules[...]``-and-reimport patterns in
# the suite target exactly these (see the grep in the module docstring).
_FIRST_PARTY_ROOTS = frozenset(
    {
        "run_agent",
        "agent",
        "tools",
        "gateway",
        "cli",
        "plugins",
        "hermes_cli",
        "hermes_state",
        "hermes_constants",
        "hermes_logging",
        "hermes_agent",
    }
)

# Populated on the first call (post-collection, pre-execution, so no
# fixture-driven pollution has run yet): {module_name: module_object}.
_FIRST_PARTY_SNAPSHOT: "dict[str, object]" = {}


def _is_first_party(name: str) -> bool:
    root = name.split(".", 1)[0]
    return root in _FIRST_PARTY_ROOTS or root.startswith("hermes")


def _restore_first_party_modules() -> None:
    """Undo ``del sys.modules[...]`` + reimport identity leaks.

    First call captures the collection-time module objects; later calls
    re-point any first-party ``sys.modules`` entry whose identity changed
    (or went missing) back to that captured object.  Modules imported for
    the first time *after* the snapshot (lazy, execution-time imports)
    were never bound by a collected file, so identity drift there can't
    break a ``@patch`` target — they are left untouched.

    The parent-package attribute is restored alongside ``sys.modules``:
    ``import agent.model_metadata as mm`` binds ``mm`` to the ATTRIBUTE
    ``agent.model_metadata`` (set by the import machinery on the ``agent``
    package), NOT to ``sys.modules["agent.model_metadata"]``.  A reimport
    updates both; restoring only ``sys.modules`` leaves the two disagreeing,
    which makes ``importlib.reload(mm)`` raise "module ... not in sys.modules".
    """
    if not _FIRST_PARTY_SNAPSHOT:
        for name, mod in list(sys.modules.items()):
            if _is_first_party(name):
                _FIRST_PARTY_SNAPSHOT[name] = mod
        return

    for name, mod in _FIRST_PARTY_SNAPSHOT.items():
        if sys.modules.get(name) is not mod:
            sys.modules[name] = mod
        if "." in name:
            parent_name, _, attr = name.rpartition(".")
            parent = sys.modules.get(parent_name)
            if parent is not None and getattr(parent, attr, mod) is not mod:
                setattr(parent, attr, mod)


def reset_cross_file_globals() -> None:
    """Restore process-global state to fresh-process condition.

    Runs the module-identity restore first, then value resets, so the
    value resets act on the canonical (collection-time) module objects.

    Known value leaks and why they matter:

    * ``tools.file_tools`` — a leaked ``_file_ops_cache["default"]`` beats
      ``TERMINAL_CWD`` in ``_resolve_base_dir``.
    * ``tools.terminal_tool`` — active environments / env overrides /
      sudo-password scopes persist across files.
    * ``gateway.session_context`` — session ContextVars pinned by
      ``clear_session_vars`` suppress ``monkeypatch.setenv`` forever
      (see ``get_session_env``).
    * ``agent.runtime_cwd`` — session cwd ContextVar pinned the same way.
    * ``prompt_toolkit.application.current`` — the module-default
      ``AppSession`` lazily creates and caches its ``output`` bound to
      whatever ``sys.stdout`` existed at first use.  The first file to
      print through prompt_toolkit (``cli._cprint`` et al.) pins every
      later file's output to that stale stream, bypassing ``capsys``.
    """
    _restore_first_party_modules()

    file_tools = sys.modules.get("tools.file_tools")
    if file_tools is not None:
        with file_tools._file_ops_lock:
            file_tools._file_ops_cache.clear()
            file_tools._last_known_cwd.clear()

    terminal_tool = sys.modules.get("tools.terminal_tool")
    if terminal_tool is not None:
        with terminal_tool._env_lock:
            terminal_tool._active_environments.clear()
        terminal_tool._task_env_overrides.clear()
        with terminal_tool._sudo_password_cache_lock:
            terminal_tool._sudo_password_cache.clear()

    session_context = sys.modules.get("gateway.session_context")
    if session_context is not None:
        for var in session_context._VAR_MAP.values():
            var.set(session_context._UNSET)
        session_context._SESSION_ASYNC_DELIVERY.set(session_context._UNSET)
        session_context._session_context_engaged = False

    runtime_cwd = sys.modules.get("agent.runtime_cwd")
    if runtime_cwd is not None:
        runtime_cwd._SESSION_CWD.set(runtime_cwd._UNSET)

    ptk_current = sys.modules.get("prompt_toolkit.application.current")
    if ptk_current is not None:
        ptk_current._current_app_session.set(ptk_current.AppSession())

    # ``cli`` reloaded under prompt_toolkit MagicMock stubs (the
    # ``importlib.reload(cli)``-inside-``patch.dict(sys.modules, ...)``
    # pattern in test_cli_init / test_cli_extension_hooks et al.) mutates
    # ``cli`` in place, so identity restore can't help — its attributes
    # stay MagicMocks and ``_cprint`` becomes a silent no-op for every
    # later file.  Reload once more now that real prompt_toolkit is back
    # in sys.modules (patch.dict restored it on context exit).
    cli_mod = sys.modules.get("cli")
    if cli_mod is not None and isinstance(
        getattr(cli_mod, "_pt_print", None), _mock.NonCallableMock
    ):
        importlib.reload(cli_mod)

    # In-memory TTL caches for model metadata: one file's (mocked or
    # real) fetch otherwise satisfies every later file's lookup for the
    # cache TTL (1h), defeating their monkeypatched registries.
    models_dev = sys.modules.get("agent.models_dev")
    if models_dev is not None:
        models_dev._models_dev_cache = {}
        models_dev._models_dev_cache_time = 0.0

    model_metadata = sys.modules.get("agent.model_metadata")
    if model_metadata is not None:
        model_metadata._model_metadata_cache = {}
        model_metadata._model_metadata_cache_time = 0.0
        model_metadata._novita_metadata_cache = {}
        model_metadata._novita_metadata_cache_time = 0.0
        model_metadata._endpoint_model_metadata_cache = {}
        model_metadata._endpoint_model_metadata_cache_time = {}

    skill_utils = sys.modules.get("agent.skill_utils")
    if skill_utils is not None:
        skill_utils._ENV_DETECT_CACHE.clear()
        skill_utils._RAW_CONFIG_CACHE.clear()
        skill_utils._EXTERNAL_DIRS_CACHE.clear()

    prompt_builder = sys.modules.get("agent.prompt_builder")
    if prompt_builder is not None:
        prompt_builder._BACKEND_PROBE_CACHE.clear()
        with prompt_builder._SKILLS_PROMPT_CACHE_LOCK:
            prompt_builder._SKILLS_PROMPT_CACHE.clear()

    # Runtime main-provider/model override pins auxiliary-task provider
    # resolution (title generation, oneshot) to whatever the last file
    # configured; fresh processes start with no override.
    auxiliary_client = sys.modules.get("agent.auxiliary_client")
    if auxiliary_client is not None:
        auxiliary_client._RUNTIME_MAIN_PROVIDER = ""
        auxiliary_client._RUNTIME_MAIN_MODEL = ""

    # Degraded-fallback banner state: a file that simulates a primary
    # rate-limit leaves a real-clock "until" deadline that prefixes the
    # "⚡ Heads up ..." notice onto every later file's replies.
    gateway_run = sys.modules.get("gateway.run")
    if gateway_run is not None:
        gateway_run._DEGRADED_FALLBACK["until"] = 0.0
        gateway_run._DEGRADED_FALLBACK["retry_seconds"] = 0

    # Dashboard auth gate on the shared FastAPI ``app.state``. Auth tests set
    # ``auth_required`` / ``bound_host`` / ``bound_port`` on the module-level
    # ``web_server.app`` (a single shared object every TestClient wraps) to
    # simulate a public/gated bind. ``start_server`` sets them; a fresh import
    # leaves them unset, so ``getattr(app.state, "auth_required", False)`` is
    # False (ephemeral-token mode). A test that flips the gate on and doesn't
    # restore it makes every later file's request 401 "no_cookie". Clear them
    # so each file starts in the fresh (ephemeral) state.
    web_server = sys.modules.get("hermes_cli.web_server")
    if web_server is not None:
        app_state = getattr(getattr(web_server, "app", None), "state", None)
        if app_state is not None:
            for _attr in ("auth_required", "bound_host", "bound_port"):
                # Starlette's ``State.__delattr__`` raises ``KeyError`` (not
                # ``AttributeError``) when the attribute was never set.
                try:
                    delattr(app_state, _attr)
                except (AttributeError, KeyError):
                    pass


@pytest.fixture(autouse=True, scope="module")
def _fresh_process_globals():
    """Start every test FILE with fresh-process globals (see module docstring).

    Import this fixture from a directory's ``conftest.py`` to opt the
    directory into fresh-process emulation for single-process runs::

        from tests._global_state import _fresh_process_globals  # noqa: F401
    """
    reset_cross_file_globals()
    yield
