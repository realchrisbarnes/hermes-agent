"""Circuit breaker for repeated identical outer-loop errors.

2026-07-01: a MagicMock that leaked into a live conversation turn made
``extract_reasoning`` raise the same TypeError on every retry; the outer loop
re-called the API 89 times in under a second (bounded only by max_iterations,
no backoff). Deterministic errors never heal by retrying, so after
``OUTER_ERROR_CIRCUIT_LIMIT`` consecutive identical exceptions the turn must
abort with an honest final response instead of burning the retry budget.

These tests pin the pure decision helper; the wiring lives in the outer
``except`` block of ``run_conversation``.
"""

from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent.conversation_loop import OUTER_ERROR_CIRCUIT_LIMIT, _outer_error_circuit


def _drive(sigs):
    """Feed signatures through the helper; return (final_count, tripped_at)."""
    last, count, tripped_at = None, 0, None
    for i, sig in enumerate(sigs):
        last, count, should_break = _outer_error_circuit(sig, last, count)
        if should_break and tripped_at is None:
            tripped_at = i
    return count, tripped_at


def test_identical_errors_trip_at_limit():
    sig = "TypeError: sequence item 0: expected str instance, MagicMock found"
    count, tripped_at = _drive([sig] * 10)
    assert tripped_at == OUTER_ERROR_CIRCUIT_LIMIT - 1, (
        "breaker must trip on the Nth consecutive identical error, "
        "not retry to max_iterations"
    )


def test_distinct_errors_never_trip():
    sigs = [f"TimeoutError: request {i} timed out after {i}s" for i in range(10)]
    count, tripped_at = _drive(sigs)
    assert tripped_at is None, "distinct transient errors must keep the retry loop alive"
    assert count == 1


def test_signature_change_resets_count():
    a = "TypeError: bad join"
    b = "ConnectionError: reset by peer"
    count, tripped_at = _drive([a, a, b, a, a])
    assert tripped_at is None
    _, _, should_break = _outer_error_circuit(a, a, OUTER_ERROR_CIRCUIT_LIMIT - 1)
    assert should_break


def test_limit_is_small_but_tolerant():
    # 2 identical errors can still be coincidence (e.g. two provider blips
    # with byte-identical messages); 3 is deterministic. Anything above 5
    # would defeat the point.
    assert 3 <= OUTER_ERROR_CIRCUIT_LIMIT <= 5
