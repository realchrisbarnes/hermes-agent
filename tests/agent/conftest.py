"""Shared fixtures for tests/agent/.

Fresh-process emulation for single-process runs: reset known
process-global registries at each test-file boundary so plain
``pytest tests/agent/`` matches the canonical per-file subprocess
runner (``scripts/run_tests_parallel.py``).  See
``tests/_global_state.py`` for the registry list and rationale.
"""

from tests._global_state import _fresh_process_globals  # noqa: F401
