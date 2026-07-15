# disk-cleanup (read-only)

Tracks and inventories ephemeral files created during Hermes Agent sessions
— test scripts, temp outputs, cron logs, and stale chrome profiles.
Scoped strictly to `$HERMES_HOME` and `/tmp/hermes-*`.

Originally contributed by [@LVT382009](https://github.com/LVT382009) as a
skill in PR #12212.  Ported to the plugin system so the behaviour runs
automatically via `post_tool_call` and `on_session_end` hooks — the agent
never needs to remember to call a tool.

## How it works

| Hook | Behaviour |
|---|---|
| `post_tool_call` | When `write_file` / `terminal` / `patch` creates a file matching `test_*`, `tmp_*`, or `*.test.*` inside `HERMES_HOME`, track it silently as `test` / `temp` / `cron-output`. |
| `on_session_end` | If files were tracked, record a policy-paused review summary; delete nothing. |

Historical review thresholds (not deletion authorization):

| Category | Review threshold | Cleanup status |
|---|---|---|
| `test` | every session end | Blocked |
| `temp` | >7 days since tracked | Blocked |
| `cron-output` | >14 days since tracked | Blocked |
| empty dirs under HERMES_HOME | always | Blocked |
| `research` | >30 days, beyond 10 newest | Blocked |
| `chrome-profile` | >14 days since tracked | Blocked |
| files >500 MB | always review | Blocked |

## Slash command

```
/disk-cleanup status                     # breakdown + top-10 largest
/disk-cleanup dry-run                    # preview without deleting
/disk-cleanup quick                      # BLOCKED_PENDING_HARDENING; no deletion
/disk-cleanup deep                       # review candidates only; no deletion
/disk-cleanup track <path> <category>    # manual tracking
/disk-cleanup forget <path>              # stop tracking
```

## Safety

- Cleanup apply is `BLOCKED_PENDING_HARDENING`; approval alone cannot enable it

- `is_safe_path()` rejects anything outside `HERMES_HOME` or `/tmp/hermes-*`
- Windows mounts (`/mnt/c` etc.) are rejected
- The state directory `$HERMES_HOME/disk-cleanup/` is itself excluded
- `$HERMES_HOME/logs/`, `memories/`, `sessions/`, `skills/`, `plugins/`,
  and config files are never tracked
- Backup/restore is scoped to `tracked.json` — the plugin never touches
  agent logs
- Atomic writes: `.tmp` → backup → rename
