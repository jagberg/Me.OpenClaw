"""Durable record of every Telegram message, in and out.

Three jobs, one table (`telegram_messages`):

1. **Training data** — full raw payload plus the `app_version` that handled it,
   so behaviour can be attributed to a specific deploy.
2. **Audit trail** — "did my tap register?" is answerable by a query instead of
   by diffing claim state and guessing.
3. **Replay queue** — a row is written *before* handlers run, so a crash
   mid-handler leaves `processed_at IS NULL` and the update is re-run at
   startup. After `MESSAGE_QUEUE_TTL_HOURS` it stops being replay-eligible
   (Telegram only retains updates ~24h anyway) but the row itself is kept
   forever — it's the dataset.

Replay is at-least-once, which is safe only because the mutations it can
re-trigger are guarded: `claim_status.mark_sent` refuses anything not
`drafted`, `dismiss_mismatch` is idempotent, condition/pet setters are
last-write-wins.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from . import config, db

logger = logging.getLogger(__name__)

_SUMMARY_LIMIT = 200
ABANDONED = f"abandoned — unprocessed for over {config.MESSAGE_QUEUE_TTL_HOURS}h"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _describe(update) -> tuple[str, str]:
    """(kind, summary) for a human skimming the log or a query filtering it."""
    if getattr(update, "callback_query", None) is not None:
        return "tap", (update.callback_query.data or "")[:_SUMMARY_LIMIT]
    message = getattr(update, "message", None)
    if message is not None:
        text = message.text or ""
        if text.startswith("/"):
            return "command", text[:_SUMMARY_LIMIT]
        if text:
            return "text", text[:_SUMMARY_LIMIT]
        return "non_text", f"<{'document' if message.document else 'photo' if message.photo else 'other'}>"
    return "other", ""


def record_inbound(update) -> int | None:
    """Write the arrival row. Returns the update_id to settle later, or None if
    this update has been seen before (Telegram redelivery, or a replay) — the
    caller still processes it; only the log row is deduped."""
    update_id = getattr(update, "update_id", None)
    if update_id is None:
        return None
    kind, summary = _describe(update)
    try:
        payload = json.dumps(update.to_dict(), default=str)
    except Exception:  # noqa: BLE001 — a payload we can't serialise must not block the handler
        logger.warning("could not serialise update %s for the log", update_id)
        payload = "{}"
    with db.get_connection() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO telegram_messages "
            "(update_id, direction, kind, summary, payload, app_version, received_at) "
            "VALUES (?, 'in', ?, ?, ?, ?, ?)",
            (update_id, kind, summary, payload, config.APP_VERSION, _now()),
        )
        if cur.rowcount == 0:
            return None
    return update_id


def record_outbound(kind: str, summary: str, payload: dict) -> None:
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO telegram_messages "
            "(direction, kind, summary, payload, app_version, received_at) "
            "VALUES ('out', ?, ?, ?, ?, ?)",
            (kind, (summary or "")[:_SUMMARY_LIMIT], json.dumps(payload, default=str),
             config.APP_VERSION, _now()),
        )


def mark_processed(update_id: int | None) -> None:
    """`error IS NULL` matters: PTB's error handler runs *inside*
    process_update, so a failed update reaches this call looking successful.
    Refusing to settle a row that already carries an error is what keeps it in
    the replay queue."""
    if update_id is None:
        return
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE telegram_messages SET processed_at = ? "
            "WHERE update_id = ? AND processed_at IS NULL AND error IS NULL",
            (_now(), update_id),
        )


def mark_failed(update_id: int | None, error: str) -> None:
    """Record why an update failed and leave processed_at NULL so it replays."""
    if update_id is None:
        return
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE telegram_messages SET error = ? WHERE update_id = ?",
            (str(error)[:_SUMMARY_LIMIT], update_id),
        )


def pending() -> list:
    """Inbound updates still owed, newest last, inside the replay window."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=config.MESSAGE_QUEUE_TTL_HOURS)).isoformat()
    with db.get_connection() as conn:
        return conn.execute(
            "SELECT update_id, payload, summary FROM telegram_messages "
            "WHERE direction = 'in' AND processed_at IS NULL AND received_at >= ? "
            "ORDER BY update_id",
            (cutoff,),
        ).fetchall()


async def replay_pending(application) -> int:
    """Re-run updates that arrived but never finished — the sleep/crash case.
    Runs after polling starts, so anything Telegram itself redelivers is deduped
    by update_id rather than double-logged."""
    from telegram import Update  # local import: keeps this module importable without PTB

    rows = pending()
    if not rows:
        return 0
    logger.warning("replaying %d unprocessed Telegram update(s): %s", len(rows),
                   ", ".join(r["summary"] or "?" for r in rows))
    replayed = 0
    for row in rows:
        try:
            update = Update.de_json(json.loads(row["payload"]), application.bot)
        except Exception:  # noqa: BLE001 — one bad row must not block the rest
            logger.exception("could not rebuild update %s for replay", row["update_id"])
            # Settle first, then annotate: mark_processed refuses rows that
            # already carry an error, and a payload that won't parse will never
            # parse — retrying it every startup is a loop, not resilience.
            mark_processed(row["update_id"])
            mark_failed(row["update_id"], "unreplayable payload")
            continue
        await application.process_update(update)
        replayed += 1
    return replayed


def expire_queue() -> int:
    """Stop replaying updates older than the window. The row stays — it's
    training data; only its queue membership expires."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=config.MESSAGE_QUEUE_TTL_HOURS)).isoformat()
    with db.get_connection() as conn:
        cur = conn.execute(
            "UPDATE telegram_messages SET processed_at = ?, error = COALESCE(error, ?) "
            "WHERE direction = 'in' AND processed_at IS NULL AND received_at < ?",
            (_now(), ABANDONED, cutoff),
        )
        count = cur.rowcount
    if count:
        logger.warning("%d Telegram update(s) expired unprocessed — never acted on", count)
    return count


def iter_jsonl():
    """The whole stream as JSONL, oldest first, for reinforcement learning.
    `payload` is re-inlined as an object rather than a JSON string so a
    consumer doesn't have to double-decode."""
    with db.get_connection() as conn:
        for row in conn.execute(
            "SELECT update_id, direction, kind, summary, payload, app_version, "
            "received_at, processed_at, error FROM telegram_messages ORDER BY id"
        ):
            record = dict(row)
            try:
                record["payload"] = json.loads(record["payload"])
            except (TypeError, ValueError):
                pass
            yield json.dumps(record, default=str) + "\n"


def stats() -> dict:
    """For /health: is anything stuck, and when did we last hear from Justin."""
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT (SELECT COUNT(*) FROM telegram_messages WHERE direction='in' AND processed_at IS NULL) AS queued, "
            "(SELECT MAX(received_at) FROM telegram_messages WHERE direction='in') AS last_inbound_at, "
            "(SELECT COUNT(*) FROM telegram_messages) AS total"
        ).fetchone()
    return dict(row)
