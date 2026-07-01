#!/usr/bin/env python
"""Reabsorb archived memory overflow back into active memory.

The bounded memory tool durably archives any over-cap write to
``<MEMORY|USER>.md.overflow.md`` instead of dropping it (no silent loss). This
job folds those archived entries back into active memory whenever there is room
(after dedup), rescanning each for threats first, and never deletes a
non-duplicate active entry. Safe to run repeatedly; a no-op when there is no
overflow. Run on demand or from a daily tick.

Usage: python scripts/memory_consolidate.py   [--json]
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.memory_tool import MemoryStore  # noqa: E402

DEFAULT_MEMORY_LIMIT = 3200
DEFAULT_USER_LIMIT = 3600


def _caps() -> tuple[int, int]:
    """Read the live caps from config.yaml so consolidation respects the exact
    same limits the running agent enforces (over-filling would just get rejected
    on the next add)."""
    cfg = Path(os.path.expanduser("~/.hermes/config.yaml"))
    try:
        import yaml
        mem = (yaml.safe_load(cfg.read_text()) or {}).get("memory", {}) or {}
        return (int(mem.get("memory_char_limit", DEFAULT_MEMORY_LIMIT)),
                int(mem.get("user_char_limit", DEFAULT_USER_LIMIT)))
    except Exception:
        return (DEFAULT_MEMORY_LIMIT, DEFAULT_USER_LIMIT)


def main() -> int:
    mem_limit, user_limit = _caps()
    store = MemoryStore(memory_char_limit=mem_limit, user_char_limit=user_limit)
    store.load_from_disk()
    report = {"memory": store.consolidate("memory"),
              "user": store.consolidate("user"),
              "caps": {"memory": mem_limit, "user": user_limit}}
    if "--json" in sys.argv:
        print(json.dumps(report, indent=2))
    else:
        for tgt in ("memory", "user"):
            r = report[tgt]
            print(f"{tgt}: reabsorbed={r.get('reabsorbed')} "
                  f"remaining_overflow={r.get('remaining_overflow')} "
                  f"usage={r.get('usage', 'n/a')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
