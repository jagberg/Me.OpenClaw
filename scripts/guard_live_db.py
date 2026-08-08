#!/usr/bin/env python3
"""Reject a host-side command that opens the live DB without `mode=ro`.

ADR-0018 Alternative 4. The ADR left this unbuilt with "worth building if this
recurs". It recurred: four read-write opens of the live DB in one session on
2026-07-27, with the rule in `CLAUDE.md` and the ADR itself read later the same
session. The ADR's own sentence is the whole argument — *"Nothing prevents the
next plain `connect()`."* A helper script does not either, because the observed
failure mode is an inline `sqlite3.connect(<live path>)` in an ad-hoc one-liner,
which routes around any helper.

Why a hook and not application code: the command never reaches application code.
It is a fresh interpreter opening a file. Only something between the model and
the shell can see it.

Wired as a `PreToolUse` hook on Bash and PowerShell in `.claude/settings.json`.
Exit 2 blocks the call and returns stderr to the model; exit 0 allows it.

Deliberately narrow, because a guard that fires on legitimate work gets disabled,
and a disabled guard is worse than none:

  - only the live DB and the phantom, never a worktree's own stale copy and never
    the bare filename;
  - `docker exec` / `docker compose exec` always pass. In-container is where a
    deliberate WRITE belongs, and blocking it would push writes back to the host,
    which is the opposite of the point. This is the gap ADR-0018 leaves open:
    it covers reads and says nothing about sanctioned writes.
"""

import json
import re
import sys

# Paths that are the LIVE database, in the spellings a command plausibly uses.
# The bare filename is deliberately absent: `openclaw.db` alone matches every
# worktree's stale copy and every mention in prose, and a guard with false
# positives gets turned off.
LIVE_PATTERNS = (
    r"Me\.OpenClaw[/\\]app[/\\]data[/\\]openclaw\.db",  # the main checkout's real DB
    r"C:[/\\]+data[/\\]+openclaw\.db",  # the phantom
    r"(?<![\w.])/data/openclaw\.db",  # the container path, leaking to the host
)

# Running inside the container is the sanctioned path for a real write.
CONTAINER_PREFIXES = ("docker exec", "docker compose exec", "docker-compose exec")

MESSAGE = """BLOCKED by ADR-0018: this opens the live DB without `mode=ro`.

A plain sqlite3.connect() opens read-write. Closing it checkpoints the WAL and
deletes openclaw.db-wal / -shm from the Windows side; Docker Desktop's bind-mount
cache then holds those names as present-but-absent, and every get_connection() in
the container fails with "unable to open database file". That is a total outage --
scheduler and Telegram both -- and it took 51 minutes to clear on 2026-07-25.

To READ:   python scripts/query_db.py "SELECT ..."
           or sqlite3.connect("file:<path>?mode=ro", uri=True)
To WRITE:  run it inside the app container (docker exec), with a backup first.

If this command really is read-only, add `?mode=ro` and it will pass."""


def is_blocked(command: str) -> bool:
    """True when the command touches the live DB and is not demonstrably read-only."""
    if not command:
        return False
    lowered = command.lower()
    if any(prefix in lowered for prefix in CONTAINER_PREFIXES):
        return False
    if "mode=ro" in lowered:
        return False
    return any(re.search(p, command, re.IGNORECASE) for p in LIVE_PATTERNS)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        # A guard that crashes must not block every command. Fail open on a
        # payload we cannot parse, loudly enough to be noticed.
        print("guard_live_db: unreadable hook payload, allowing", file=sys.stderr)
        return 0

    command = (payload.get("tool_input") or {}).get("command", "")
    if is_blocked(command):
        print(MESSAGE, file=sys.stderr)
        return 2
    return 0


def _self_check() -> None:
    """`python scripts/guard_live_db.py --self-check` -- both directions.

    A guard tested only on the failing case is untested: the reason this one can
    survive is that it stays quiet on legitimate work.
    """
    blocked = [
        "python -c \"import sqlite3; sqlite3.connect('C:/Code/Me.OpenClaw/app/data/openclaw.db')\"",
        r"sqlite3.connect('C:\Code\Me.OpenClaw\app\data\openclaw.db')",
        "python -c \"sqlite3.connect('C:/data/openclaw.db')\"",
        "sqlite3 /data/openclaw.db 'UPDATE vet_claims SET status=1'",
    ]
    allowed = [
        "python -c \"sqlite3.connect('file:C:/Code/Me.OpenClaw/app/data/openclaw.db?mode=ro', uri=True)\"",
        'python scripts/query_db.py "SELECT id FROM vet_claims"',
        "docker exec meopenclaw-app-1 python -c \"import sqlite3; sqlite3.connect('/data/openclaw.db')\"",
        "sqlite3 ./app/data/openclaw.db .tables",  # a worktree's own stale copy
        "git status",
        "",
    ]
    for cmd in blocked:
        assert is_blocked(cmd), f"should have blocked: {cmd}"
    for cmd in allowed:
        assert not is_blocked(cmd), f"should have allowed: {cmd}"
    print(f"guard_live_db self-check OK ({len(blocked)} blocked, {len(allowed)} allowed)")


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        _self_check()
    else:
        raise SystemExit(main())
