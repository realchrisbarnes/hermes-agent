"""disk-cleanup plugin — read-only ephemeral-file inventory.

Wires three behaviours:

1. ``post_tool_call`` hook — inspects ``write_file`` and ``terminal``
   tool results for newly-created paths matching test/temp patterns
   under ``HERMES_HOME`` and tracks them silently.  Zero agent
   compliance required.

2. ``on_session_end`` hook — when files were tracked during the turn,
   records a policy-paused review summary without deleting anything.

3. ``/disk-cleanup`` slash command — manual ``status``, ``dry-run``,
   ``quick``, ``deep``, ``track``, ``forget``.

Replaces PR #12212's skill-plus-script design: the agent no longer
needs to remember to run commands.
"""

from __future__ import annotations

import logging
import re
import shlex
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Set

from . import disk_cleanup as dg

logger = logging.getLogger(__name__)


# Per-task set of "test files newly tracked this turn".  Keyed by task_id
# (or session_id as fallback) so on_session_end can decide whether to run
# cleanup.  Guarded by a lock — post_tool_call can fire concurrently on
# parallel tool calls.
_recent_test_tracks: Dict[str, Set[str]] = {}
_lock = threading.Lock()


# Tool-call result shapes we can parse
_WRITE_FILE_PATH_KEY = "path"
_TERMINAL_PATH_REGEX = re.compile(r"(?:^|\s)(/[^\s'\"`]+|\~/[^\s'\"`]+)")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tracker_key(task_id: str, session_id: str) -> str:
    return task_id or session_id or "default"


def _record_track(task_id: str, session_id: str, path: Path, category: str) -> None:
    """Record that we tracked *path* as *category* during this turn."""
    if category != "test":
        return
    key = _tracker_key(task_id, session_id)
    with _lock:
        _recent_test_tracks.setdefault(key, set()).add(str(path))


def _drain(task_id: str, session_id: str) -> Set[str]:
    """Pop the set of test paths tracked during this turn."""
    key = _tracker_key(task_id, session_id)
    with _lock:
        return _recent_test_tracks.pop(key, set())


def _attempt_track(path_str: str, task_id: str, session_id: str) -> None:
    """Best-effort auto-track. Never raises."""
    try:
        p = Path(path_str).expanduser()
    except Exception:
        return
    if not p.exists():
        return
    category = dg.guess_category(p)
    if category is None:
        return
    newly = dg.track(str(p), category, silent=True)
    if newly:
        _record_track(task_id, session_id, p, category)


def _extract_paths_from_write_file(args: Dict[str, Any]) -> Set[str]:
    path = args.get(_WRITE_FILE_PATH_KEY)
    return {path} if isinstance(path, str) and path else set()


def _extract_paths_from_patch(args: Dict[str, Any]) -> Set[str]:
    # The patch tool creates new files via the `mode="patch"` path too, but
    # most of its use is editing existing files — we only care about new
    # ephemeral creations, so treat patch conservatively and only pick up
    # the single-file `path` arg.  Track-then-cleanup is idempotent, so
    # re-tracking an already-tracked file is a no-op (dedup in track()).
    path = args.get("path")
    return {path} if isinstance(path, str) and path else set()


def _extract_paths_from_terminal(args: Dict[str, Any], result: str) -> Set[str]:
    """Best-effort: pull candidate filesystem paths from a terminal command
    and its output, then let ``guess_category`` / ``is_safe_path`` filter.
    """
    paths: Set[str] = set()
    cmd = args.get("command") or ""
    if isinstance(cmd, str) and cmd:
        # Tokenise the command — catches `touch /tmp/hermes-x/test_foo.py`
        try:
            for tok in shlex.split(cmd, posix=True):
                if tok.startswith(("/", "~")):
                    paths.add(tok)
        except ValueError:
            pass
    # Only scan the result text if it's a reasonable size (avoid 50KB dumps).
    if isinstance(result, str) and len(result) < 4096:
        for match in _TERMINAL_PATH_REGEX.findall(result):
            paths.add(match)
    return paths


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------

def _on_post_tool_call(
    tool_name: str = "",
    args: Optional[Dict[str, Any]] = None,
    result: Any = None,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    **_: Any,
) -> None:
    """Auto-track ephemeral files created by recent tool calls."""
    if not isinstance(args, dict):
        return

    candidates: Set[str] = set()
    if tool_name == "write_file":
        candidates = _extract_paths_from_write_file(args)
    elif tool_name == "patch":
        candidates = _extract_paths_from_patch(args)
    elif tool_name == "terminal":
        candidates = _extract_paths_from_terminal(args, result if isinstance(result, str) else "")
    else:
        return

    for path_str in candidates:
        _attempt_track(path_str, task_id, session_id)


def _on_session_end(
    session_id: str = "",
    completed: bool = True,
    interrupted: bool = False,
    **_: Any,
) -> None:
    """Record a read-only review summary when files were tracked."""
    # Drain both task-level and session-level buckets.  In practice only one
    # is populated per turn; the other is empty.
    drained_session = _drain("", session_id)
    # Also drain any task-scoped buckets that happen to exist.  This is a
    # cheap sweep: if an agent spawned subagents (each with their own
    # task_id) they'll have recorded into separate buckets; we want to
    # summarize them all at session end.
    with _lock:
        task_buckets = list(_recent_test_tracks.keys())
    for key in task_buckets:
        if key and key != session_id:
            _recent_test_tracks.pop(key, None)

    if not drained_session and not task_buckets:
        return

    try:
        summary = dg.quick()
    except Exception as exc:
        logger.debug("disk-cleanup review summary failed: %s", exc)
        return

    dg._log(
        f"SESSION_REVIEW: apply={summary['apply_status']} "
        f"candidates={summary['review_candidates']} deleted=0 dirs=0 freed=0"
    )


# ---------------------------------------------------------------------------
# Slash command
# ---------------------------------------------------------------------------

_HELP_TEXT = """\
/disk-cleanup — read-only ephemeral-file inventory

Subcommands:
  status                     Per-category breakdown + top-10 largest
  dry-run                    Preview review candidates without deleting
  quick                      Report BLOCKED_PENDING_HARDENING; no deletion
  deep                       Report review candidates; no deletion
  track <path> <category>    Manually add a path to tracking
  forget <path>              Stop tracking a path (does not delete)

Categories: temp | test | research | download | chrome-profile | cron-output | other

All operations are scoped to HERMES_HOME and /tmp/hermes-*.
Files may be tracked on write_file / terminal, but cleanup apply is policy-paused.
"""


def _fmt_summary(summary: Dict[str, Any]) -> str:
    if summary.get("apply_status") == dg.CLEANUP_APPLY_STATUS:
        return (
            f"[disk-cleanup] {dg.CLEANUP_APPLY_STATUS}: no files or directories "
            f"were deleted; {summary.get('review_candidates', 0)} review candidate(s)."
        )
    base = (
        f"[disk-cleanup] Cleaned {summary['deleted']} files + "
        f"{summary['empty_dirs']} empty dirs, freed {dg.fmt_size(summary['freed'])}."
    )
    if summary.get("errors"):
        base += f"\n  {len(summary['errors'])} error(s); see cleanup.log."
    return base


def _handle_slash(raw_args: str) -> Optional[str]:
    argv = raw_args.strip().split()
    if not argv or argv[0] in {"help", "-h", "--help"}:
        return _HELP_TEXT

    sub = argv[0]

    if sub == "status":
        return dg.format_status(dg.status())

    if sub == "dry-run":
        auto, prompt = dg.dry_run()
        auto_size = sum(i["size"] for i in auto)
        prompt_size = sum(i["size"] for i in prompt)
        lines = [
            "Dry-run preview (nothing deleted):",
            f"  Auto-delete : {len(auto)} files ({dg.fmt_size(auto_size)})",
        ]
        for item in auto:
            lines.append(f"    [{item['category']}] {item['path']}")
        lines.append(
            f"  Needs prompt: {len(prompt)} files ({dg.fmt_size(prompt_size)})"
        )
        for item in prompt:
            lines.append(f"    [{item['category']}] {item['path']}")
        lines.append(
            f"\n  Total potential: {dg.fmt_size(auto_size + prompt_size)}"
        )
        return "\n".join(lines)

    if sub == "quick":
        return _fmt_summary(dg.quick())

    if sub == "deep":
        # In-session deep can't prompt the user interactively — show what
        # quick cleaned plus the items that WOULD need confirmation.
        quick_summary = dg.quick()
        _auto, prompt_items = dg.dry_run()
        lines = [_fmt_summary(quick_summary)]
        if prompt_items:
            size = sum(i["size"] for i in prompt_items)
            lines.append(
                f"\n{len(prompt_items)} item(s) need confirmation "
                f"({dg.fmt_size(size)}):"
            )
            for item in prompt_items:
                lines.append(f"  [{item['category']}] {item['path']}")
            lines.append(
                "\nCleanup remains policy-paused. Review only; do not delete "
                "through this plugin."
            )
        return "\n".join(lines)

    if sub == "track":
        if len(argv) < 3:
            return "Usage: /disk-cleanup track <path> <category>"
        path_arg = argv[1]
        category = argv[2]
        if category not in dg.ALLOWED_CATEGORIES:
            return (
                f"Unknown category '{category}'. "
                f"Allowed: {sorted(dg.ALLOWED_CATEGORIES)}"
            )
        if dg.track(path_arg, category, silent=True):
            return f"Tracked {path_arg} as '{category}'."
        return (
            f"Not tracked (already present, missing, or outside HERMES_HOME): "
            f"{path_arg}"
        )

    if sub == "forget":
        if len(argv) < 2:
            return "Usage: /disk-cleanup forget <path>"
        n = dg.forget(argv[1])
        return (
            f"Removed {n} tracking entr{'y' if n == 1 else 'ies'} for {argv[1]}."
            if n else f"Not found in tracking: {argv[1]}"
        )

    return f"Unknown subcommand: {sub}\n\n{_HELP_TEXT}"


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_command(
        "disk-cleanup",
        handler=_handle_slash,
        description="Track and review ephemeral Hermes session files; cleanup is policy-paused.",
    )
