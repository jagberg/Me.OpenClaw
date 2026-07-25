# ADR-0018: Host-side access to the live SQLite DB is read-only, always

**Date**: 2026-07-25
**Status**: accepted
**Deciders**: Claude (operational rule; Justin informed)

## Context

`app/data/openclaw.db` is bind-mounted into the container (`C:/code/Me.OpenClaw/app/data:/data`), so the Windows host and the container open the same file. ADR-0002 chose SQLite; the WAL decision that followed is recorded only in a `db.py` comment and in `README.md`'s Storage section: the default rollback journal made a host-side write block a container read and produce a `disk I/O error`, so `get_connection()` sets `PRAGMA journal_mode=WAL` and a 5s busy timeout.

That comment recorded one caveat — *"not all virtual filesystems support WAL, so re-check if the mount ever changes"* — and it framed the risk as **writes** colliding, and as a **mount change** being the thing that would break it. Both framings were incomplete, and on 2026-07-25 the incomplete part caused a total outage.

**What happened.** While verifying claim #7 against real data (per CLAUDE.md's "test hypotheses on the real DB first"), this agent queried the live DB from the host with plain `sqlite3.connect("data/openclaw.db")`. Every query was a `SELECT` — no write was intended or made. But a plain `connect()` opens read-write, and closing such a connection on a WAL database **checkpoints the WAL and deletes `openclaw.db-wal` and `openclaw.db-shm`**. Those deletions happened on the Windows side at 10:46.

From 10:46:56 onward, every `get_connection()` inside the container failed:

```
File "/app/openclaw/db.py", line 252, in get_connection
    conn.execute("PRAGMA journal_mode=WAL")
sqlite3.OperationalError: unable to open database file
```

Isolated inside the container, the cause is specific and strange:

```
touch /data/openclaw.db-wal   → cannot touch: No such file or directory   # dir is drwxrwxrwx, running as root
touch /data/.wtest            → OK                                        # same dir, writable
sqlite3 /data/_waltest.db     → journal_mode = wal                        # same dir, different name, works
sqlite3 /tmp/t.db             → journal_mode = wal                        # works
```

So: not permissions, not the mount as a whole, not WAL support. Docker Desktop's bind-mount layer held a stale cache entry for those two sidecar names — present-but-absent, so creating them returned `ENOENT`. **Why the cache behaves this way is not recorded here because it is not known** — this is the observed behaviour, not an explanation of gRPC-FUSE internals. Reproduced deterministically in the broken state; cleared by a container restart.

The failure was total and unusually well hidden — see the amendment to ADR-0015, which this incident prompted.

## Decision

**Host-side reads of the live DB use a read-only connection, without exception:**

```python
sqlite3.connect("file:data/openclaw.db?mode=ro", uri=True)
```

A `mode=ro` connection never creates or checkpoints the WAL sidecars, so it cannot reproduce this. The rule lives in `CLAUDE.md`'s working-style section — the same place that tells an agent to verify against real data — with the failure mode stated inline, because the instruction "verify against the real DB (read-only)" was already there and was *followed in intent* while still causing the outage. "Read-only" had to be made mechanical, not aspirational.

**Recovery, when it happens anyway:** `docker restart <container>`. Nothing shorter is known to clear the stale entry. Verify with `PRAGMA journal_mode` from inside the container rather than from the host — a host-side check both misses the problem and risks re-triggering it.

## Alternatives Considered

### Alternative 1: abandon WAL, go back to the rollback journal
- **Pros**: no sidecar files, so nothing to leave stale.
- **Cons**: reinstates the `disk I/O error` that WAL was adopted to fix — a host read during a container write would fail outright.
- **Why not**: trades a fault caused by a specific bad habit for a fault caused by normal concurrent use.

### Alternative 2: copy the DB to the scratchpad and query the copy
- **Pros**: physically cannot touch the live file; no rule to remember.
- **Cons**: an extra step per investigation, and a stale snapshot invites wrong conclusions about live state — the exact class of error this project keeps hitting.
- **Why not**: `mode=ro` gets the same safety against live data. Still the right choice for anything that *needs* to write while experimenting.

### Alternative 3: make the container tolerate missing sidecars (retry, or fall back to another journal mode)
- **Pros**: self-healing; no host-side discipline needed.
- **Cons**: `PRAGMA journal_mode=WAL` failing is not a transient error to retry — the stale entry persists until restart. A fallback to rollback journal would silently reintroduce Alternative 1's fault under exactly the conditions that make it likely.
- **Why not**: hides a total outage behind a degraded mode. Rejected on the same grounds as ADR-0015 Alternative 1 — a recovery path that can fail silently is not a fix.

### Alternative 4: enforce it in code (a `scripts/query_db.py` read-only helper)
- **Pros**: mechanical, not convention.
- **Cons**: only helps queries that go through it; an agent writing an ad-hoc one-liner bypasses it, which is precisely what happened.
- **Why not**: not rejected on merit — **unbuilt, and worth building** if this recurs. Recorded in `openspec/BACKLOG.md` rather than pretended into existence here.

## Consequences

### Positive
- The cheapest possible fix for a total-outage class: one connection-string change.
- The WAL trail is now in an ADR instead of only a code comment, so the next person finds it before repeating this.

### Negative
- **Convention, not enforcement.** Nothing prevents the next plain `connect()`. Alternative 4 is the enforcement story and is not built.
- Recovery needs a restart, which drops the in-flight pipeline tick (acceptable for the same reason ADR-0015 gives: `run_once` is idempotent).

### Risks
- The trigger is not limited to this agent or to sqlite: **any** host-side tool that opens the DB read-write — DB Browser for SQLite, a `sqlite3` CLI session, a backup script — can checkpoint and delete the sidecars on exit. `scripts/` has not been audited for this. Flagged, not fixed.
- The stale-cache mechanism is unexplained (above), so it is unknown whether other Docker Desktop versions, or virtiofs instead of gRPC-FUSE, behave the same. The mitigation does not depend on the mechanism, but the recovery step might.
