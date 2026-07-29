"""Give every claim whose transitions predate the event log one synthetic event.

Nineteen of the twenty-two live claims sit at a status their own event log cannot
reach: the six writers that moved them to matched/drafted/sent appended nothing,
so their logs hold reply events only (or, for eleven of them, no events at all).
The projection therefore folds them to `pending_match`, and Phase 2 cannot give
the projection authority while that is true.

This appends ONE `state_backfilled` event per disagreeing claim, carrying the
status the claim is actually at, timestamped at the claim's own `updated_at` so
it sorts before any later real event. It is deliberately shallow: a backfilled
claim's timeline says "backfilled", not a fabricated matched/drafted/sent history
we do not have. Inventing four events per claim would put dates on the record
that nothing supports.

Claims whose projection already agrees are left completely alone — their history
is genuine.

    # dry run, prints the per-claim diff and writes nothing
    python scripts/backfill_claim_state_events.py

    # inside the container, after a backup (ADR-0018: never write from the host)
    docker exec meopenclaw-telegram-claimquery-app-1 \
        python scripts/backfill_claim_state_events.py --apply

Preconditions asserted before any write, in the shape the 2026-07-28 repair
established: the claim still holds the status we measured, the projection still
disagrees, and no `state_backfilled` event exists for it already (so a second run
is a no-op rather than a second synthetic history).
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openclaw import claim_status, db  # noqa: E402

REASON = "transition predates the event log"


def plan() -> list[dict]:
    """One row per claim needing a backfill. Reads only."""
    rows = []
    with db.get_connection() as conn:
        claims = conn.execute("SELECT id, status, updated_at FROM vet_claims ORDER BY id").fetchall()
        already = {
            r["claim_id"]
            for r in conn.execute(
                "SELECT DISTINCT claim_id FROM claim_status_events WHERE event_type = 'state_backfilled'"
            )
        }
    projected = claim_status.project_all()
    for claim in claims:
        if projected.get(claim["id"]) == claim["status"]:
            continue  # genuine history — do not touch it
        rows.append({
            "claim_id": claim["id"],
            "status": claim["status"],
            "projected": projected.get(claim["id"]),
            "updated_at": claim["updated_at"],
            "already_backfilled": claim["id"] in already,
        })
    return rows


def apply(rows: list[dict]) -> int:
    written = 0
    with db.get_connection() as conn:
        for row in rows:
            if row["already_backfilled"]:
                print(f"  skip #{row['claim_id']}: already carries a state_backfilled event")
                continue
            # Re-read under the write connection: the plan was computed earlier and
            # a tick may have moved the claim since.
            current = conn.execute(
                "SELECT status FROM vet_claims WHERE id = ?", (row["claim_id"],)
            ).fetchone()
            if current is None or current["status"] != row["status"]:
                print(f"  REFUSED #{row['claim_id']}: status moved since the plan "
                      f"({row['status']} -> {current['status'] if current else 'gone'})")
                continue
            conn.execute(
                "INSERT INTO claim_status_events (claim_id, event_type, raw_email_id, detail, created_at) "
                "VALUES (?, 'state_backfilled', NULL, ?, ?)",
                (row["claim_id"],
                 json.dumps({"backfilled": True, "reason": REASON, "status": row["status"]}),
                 row["updated_at"]),
            )
            written += 1
    return written


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write the events (container-side only)")
    args = parser.parse_args()

    rows = plan()
    print(f"DATABASE_PATH={os.environ.get('DATABASE_PATH', '(config default)')}")
    print(f"{len(rows)} claim(s) need a backfill\n")
    for row in rows:
        note = "  [already backfilled]" if row["already_backfilled"] else ""
        print(f"  #{row['claim_id']:<3} stored={row['status']:<15} projected={row['projected']:<15}"
              f" at={row['updated_at']}{note}")

    if not args.apply:
        # ASCII only: the Windows console is cp1252 and an em-dash prints as a
        # replacement character, which is how a clean run reads like a failure.
        print("\nDRY RUN - nothing written. Re-run with --apply inside the container, after a backup.")
        return 0

    # `state_backfilled` is stateless, so the events change no status; the point is
    # that the fold can now reach where the claim already is.
    written = apply(rows)
    remaining = claim_status.state_projection_disagreements()
    print(f"\nwrote {written} event(s); disagreements now: {len(remaining)}")
    for row in remaining:
        print(f"  STILL DISAGREES #{row['claim_id']} stored={row['stored']} projected={row['projected']}")
    return 0 if not remaining else 1


if __name__ == "__main__":
    raise SystemExit(main())
