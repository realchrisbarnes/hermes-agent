"""Shared fixtures for tests/tools/ web-provider tests.

Per-file subprocess isolation means each test file gets a fresh interpreter,
so module-level state (like the web-search-provider registry) is empty when
a file starts.  The ``web_registry_populated`` fixture registers all bundled
providers before each test and resets the registry afterwards — tests that
depend on the registry being populated should use it explicitly or via
``@pytest.mark.usefixtures("web_registry_populated")``.

The ``_fresh_process_globals`` module-scoped janitor below emulates that
per-file isolation when the whole directory runs in ONE process (plain
``pytest tests/tools/``), so direct runs match the canonical runner.
"""

import sys
from unittest.mock import patch

import pytest


def _reset_cross_file_globals() -> None:
    """Restore the known process-global registries to fresh-process state.

    ``scripts/run_tests_parallel.py`` gives every test file its own
    interpreter, so these module-level dicts/ContextVars start empty per
    file.  A plain ``pytest tests/tools/`` run shares one process across
    all files, and state legitimately written by one file's tests (live
    cwd tracking, session identity, sudo-password scopes) shadows the
    env-var fixtures of later files — e.g. a leaked
    ``_file_ops_cache["default"]`` beats ``TERMINAL_CWD`` in
    ``_resolve_base_dir``, and session ContextVars pinned by
    ``clear_session_vars`` suppress ``monkeypatch.setenv`` forever (see
    ``get_session_env``).  Only touch modules that are already imported:
    an unimported module has no leaked state, and importing it here would
    change what "fresh process" means for the file under test.
    """
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


@pytest.fixture(autouse=True, scope="module")
def _fresh_process_globals():
    """Start every test FILE with fresh-process globals (see above).

    Module-scoped on purpose: intra-file ordering stays the test author's
    responsibility (matching the per-file subprocess runner); only
    cross-FILE leakage is neutralized.
    """
    _reset_cross_file_globals()
    yield


def register_all_web_providers():
    """Register all bundled web-search providers into the global registry.

    This is the single source of truth for the provider list used by
    test classes that need the registry populated for dispatch checks.
    """
    from agent.web_search_registry import register_provider, _reset_for_tests
    from plugins.web.brave_free.provider import BraveFreeWebSearchProvider
    from plugins.web.ddgs.provider import DDGSWebSearchProvider
    from plugins.web.exa.provider import ExaWebSearchProvider
    from plugins.web.firecrawl.provider import FirecrawlWebSearchProvider
    from plugins.web.parallel.provider import ParallelWebSearchProvider
    from plugins.web.searxng.provider import SearXNGWebSearchProvider
    from plugins.web.tavily.provider import TavilyWebSearchProvider
    from plugins.web.xai.provider import XAIWebSearchProvider

    _reset_for_tests()
    for cls in (
        BraveFreeWebSearchProvider,
        DDGSWebSearchProvider,
        ExaWebSearchProvider,
        FirecrawlWebSearchProvider,
        ParallelWebSearchProvider,
        SearXNGWebSearchProvider,
        TavilyWebSearchProvider,
        XAIWebSearchProvider,
    ):
        register_provider(cls())


@pytest.fixture
def web_registry_populated():
    """Populate the web-search-provider registry for one test, then reset."""
    register_all_web_providers()
    yield
    from agent.web_search_registry import _reset_for_tests
    _reset_for_tests()


@pytest.fixture
def disable_lazy_stt_install():
    """Disarm the runtime lazy-install probe so static ``_HAS_FASTER_WHISPER``
    patches accurately simulate 'faster-whisper not installed'.

    Without this, ``_try_lazy_install_stt()`` calls
    ``importlib.util.find_spec("faster_whisper")``, which returns truthy
    whenever the package is installed in the dev / CI environment —
    defeating the test's ``_HAS_FASTER_WHISPER=False`` patch.

    Opt in at module scope with
    ``pytestmark = pytest.mark.usefixtures("disable_lazy_stt_install")``.
    """
    with patch("tools.transcription_tools._try_lazy_install_stt", return_value=False):
        yield
