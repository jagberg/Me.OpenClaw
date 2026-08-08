#!/usr/bin/env python3
"""Read the live DB from the host, read-only, without being able to get it wrong.

ADR-0018 Alternative 4, built after the rule it enforces failed twice by
convention. A plain `sqlite3.connect()` opens read-write; closing it checkpoints
the WAL and deletes `openclaw.db-wal` / `-shm` from the Windows side, after which
Docker Desktop's bind-mount cache holds those names as present-but-absent and
every `get_connection()` in the container fails `PRAGMA journal_mode=WAL` with
"unable to open database file". Total outage, 51 minutes, 2026-07-25.

Two things this script does that a remembered convention does not:

  - it hard-codes the live path, so nobody points it at a worktree's stale copy
    (every worktree has one, and they read as plausible rather than as an error);
  - it opens `file:...?mode=ro` with `uri=True`, which never touches the sidecars.

**A helper alone is not the control**, and ADR-0018 says so itself: an ad-hoc
one-liner bypasses it. The `PreToolUse` hook in `.claude/settings.json` is what
makes the rule mechanical; this is the paved path the hook points at.

Stdlib only, deliberately — it must run under any python on the host, including
one with no virtualenv.

Usage:
    python scripts/query_db.py "SELECT id, status FROM vet_claims ORDER BY id"
    python scripts/query_db.py --tables
"""

import argparse
import sqlite3
import sys

# The MAIN checkout's data dir, which is the one compose binds into both
# containers. Not a relative path: run from a worktree, `./app/data/openclaw.db`
# is that worktree's own stale copy.
LIVE_DB = "C:/Code/Me.OpenClaw/app/data/openclaw.db"


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("sql", nargs="?", help="a SELECT to run against the live DB")
    ap.add_argument("--tables", action="store_true", help="list tables and row counts")
    args = ap.parse_args()

    if not args.sql and not args.tables:
        ap.print_help()
        return 2

    try:
        conn = connect()
    except sqlite3.OperationalError as exc:
        print(f"cannot open {LIVE_DB} read-only: {exc}", file=sys.stderr)
        return 1

    try:
        if args.tables:
            names = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
            ]
            for name in names:
                count = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]  # noqa: S608
                print(f"{name:28} {count:>7}")
            return 0

        # Refused rather than trusted. `mode=ro` already makes a write fail, but
        # failing at the sqlite layer with "attempt to write a readonly database"
        # reads like a bug in this script; saying so here names the actual rule.
        if args.sql.lstrip().split(" ", 1)[0].upper() not in (
            "SELECT",
            "WITH",
            "PRAGMA",
            "EXPLAIN",
        ):
            print("read-only: SELECT / WITH / PRAGMA / EXPLAIN only (ADR-0018)", file=sys.stderr)
            return 2

        # A mistyped column is the common case, not an exceptional one, and a
        # traceback here reads as "the helper is broken" — which is how a helper
        # stops being used and the one-liner it replaced comes back.
        try:
            rows = conn.execute(args.sql).fetchall()
        except sqlite3.Error as exc:
            print(f"{exc}\n(hint: python scripts/query_db.py --tables)", file=sys.stderr)
            return 1
        if not rows:
            print("(no rows)")
            return 0
        print(" | ".join(rows[0].keys()))
        for row in rows:
            print(" | ".join("" if v is None else str(v) for v in row))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
