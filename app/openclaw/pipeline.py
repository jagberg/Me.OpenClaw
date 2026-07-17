from datetime import datetime, timedelta, timezone

from . import claim_forms, config, db, invoice_matching, telegram_bot, vet_detection
from .scheduler import scheduler

DRAFT_LINK = "https://mail.google.com/mail/u/0/#drafts/{draft_id}"


def notify_claim_states(send_fn=None) -> None:
    """Pushes a Telegram message for claims newly stuck at `matched` (missing a
    required field) or newly `drafted`, and skips claims still sitting in the
    same state as last notified. `send_fn` is overridable for tests (spy) —
    defaults to the real Telegram send."""
    send = send_fn or telegram_bot.send_message_sync
    with db.get_connection() as conn:
        rows = conn.execute("SELECT * FROM vet_claims WHERE status IN ('matched', 'drafted')").fetchall()
    for claim in rows:
        if claim["status"] == "matched" and not claim["flag"]:
            continue  # not actually blocked, nothing to tell Justin about
        if claim["status"] == claim["telegram_notified_status"] and claim["flag"] == claim["telegram_notified_flag"]:
            continue
        if claim["status"] == "matched":
            text = f"Claim #{claim['id']}: matched, needs input — {claim['flag']}"
        else:
            text = f"Claim #{claim['id']}: drafted, ready to review — {DRAFT_LINK.format(draft_id=claim['draft_id'])}"
        send(text)
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE vet_claims SET telegram_notified_status = ?, telegram_notified_flag = ? WHERE id = ?",
                (claim["status"], claim["flag"], claim["id"]),
            )


def _pending_claims():
    with db.get_connection() as conn:
        return conn.execute(
            "SELECT vet_claims.*, bank_transactions.date AS txn_date, "
            "bank_transactions.amount AS txn_amount, bank_transactions.merchant AS txn_merchant "
            "FROM vet_claims JOIN bank_transactions "
            "ON bank_transactions.id = vet_claims.transaction_id "
            "WHERE vet_claims.status = 'pending_match'"
        ).fetchall()


def _maybe_draft_invoice_request(claim) -> None:
    if claim["invoice_request_sent_at"] or claim["flag"] == "invoice_request_drafted":
        return  # already sent (rolling recheck handles it), or already drafted awaiting Justin
    txn_date = datetime.fromisoformat(claim["txn_date"]).replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - txn_date < timedelta(days=config.INVOICE_MATCH_WINDOW_DAYS):
        return

    draft_message_id = invoice_matching.draft_invoice_request(claim)
    if draft_message_id is None:
        flag = "no vet email on file — cannot draft invoice request, add merchant contact manually"
    else:
        flag = "invoice_request_drafted"
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE vet_claims SET flag = ?, draft_id = ?, updated_at = ? WHERE id = ?",
            (flag, draft_message_id, datetime.now(timezone.utc).isoformat(), claim["id"]),
        )


def run_once() -> None:
    vet_detection.classify_unflagged()

    for claim in _pending_claims():
        if not invoice_matching.match_claim(claim):
            _maybe_draft_invoice_request(claim)

    with db.get_connection() as conn:
        matched_ids = [r["id"] for r in conn.execute("SELECT id FROM vet_claims WHERE status = 'matched'")]
    for claim_id in matched_ids:
        claim_forms.process_claim(claim_id)

    notify_claim_states()


def start() -> None:
    scheduler.add_job(
        run_once,
        "interval",
        minutes=config.VET_CLAIM_PIPELINE_INTERVAL_MINUTES,
        id="vet-claim-pipeline",
        replace_existing=True,
    )
