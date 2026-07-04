"""Shared fixtures for tests/cli/.

Fresh-process emulation for single-process runs: reset known
process-global registries at each test-file boundary so plain
``pytest tests/cli/`` matches the canonical per-file subprocess
runner (``scripts/run_tests_parallel.py``).  The load-bearing reset
for this directory is prompt_toolkit's module-default ``AppSession``:
once any file prints through ``cli._cprint``, the lazily-created
output stays pinned to that file's ``sys.stdout`` and every later
file's ``capsys`` sees nothing.  See ``tests/_global_state.py``.
"""

from tests._global_state import _fresh_process_globals  # noqa: F401
