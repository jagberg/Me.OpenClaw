from datetime import datetime, timedelta, timezone

import json

from . import claim_forms, claim_status, config, db, gmail_client, gmail_ingest, invoice_matching, telegram_bot, vet_detection
from .scheduler import scheduler

# marketing.au@ deliberately excluded — not claims-relevant (design.md).
PETCOVER_STATUS_SENDERS = ["claims.au@petcovergroup.com", "requiredinfo.au@petcovergroup.com", "accounts.au@petcovergroup.com"]

DRAFT_LINK = "https://mail.google.com/mail/u/0/#drafts/{draft_id}"

# Statuses worth pushing to Justin's phone. Urgent = he has to act (blocked
# claim, insurer waiting on him); the rest are informational lifecycle updates.
NOTIFY_URGENT = ("matched", "info_requested", "suspended")
NOTIFY_INFO = ("drafted", "acknowledged", "settled", "declined")


def _latest_settlement_detail(claim_id: int) -> dict:
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT detail FROM claim_status_events WHERE claim_id = ? AND event_type = 'settled' "
            "ORDER BY created_at DESC LIMIT 1",
            (claim_id,),
        ).fetchone()
    return json.loads(row["detail"] or "{}") if row else {}


def _notification_text(claim) -> str | None:
    status = claim["status"]
    if status == "matched":
        return f"Claim #{claim['id']}: matched, needs input — {claim['flag']}"
    if status == "drafted":
        return f"Claim #{claim['id']}: drafted, ready to review — {DRAFT_LINK.format(draft_id=claim['draft_id'])}"
    if status == "info_requested":
        return f"Claim #{claim['id']}: Petcover requested more information — reply needed."
    if status == "suspended":
        return f"Claim #{claim['id']}: suspended by Petcover — action needed."
    if status == "acknowledged":
        return f"Claim #{claim['id']}: acknowledged by Petcover."
    if status == "declined":
        return f"Claim #{claim['id']}: declined by Petcover."
    if status == "settled":
        detail = _latest_settlement_detail(claim["id"])
        claimed, paid = detail.get("claimed_amount"), detail.get("paid_amount")
        if claimed is not None and paid is not None:
            return f"Claim #{claim['id']}: settled — claimed ${claimed:.2f}, paid ${paid:.2f}."
        return f"Claim #{claim['id']}: settled."
    return None


def notify_claim_states(send_fn=None) -> None:
    """Pushes a Telegram message when a claim enters a state Justin should
    hear about (blocked at matched, drafted, or any Petcover lifecycle status),
    and skips claims still sitting in the same (status, flag) as last notified.
    `send_fn` is overridable for tests (spy) — defaults to the real send."""
    send = send_fn or telegram_bot.send_message_sync
    statuses = NOTIFY_URGENT + NOTIFY_INFO
    with db.get_connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM vet_claims WHERE status IN ({','.join('?' * len(statuses))})", statuses
        ).fetchall()
    for claim in rows:
        if claim["status"] == "matched" and not claim["flag"]:
            continue  # not actually blocked, nothing to tell Justin about
        if claim["status"] == claim["telegram_notified_status"] and claim["flag"] == claim["telegram_notified_flag"]:
            continue
        text = _notification_text(claim)
        if text is None:
            continue
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


def poll_petcover_status() -> None:
    """Polls Petcover's claims-relevant senders for status replies (ack, info
    request, suspended, settled, declined) and records them via claim_status.
    Raises on Gmail API failure — same retry-next-interval behavior as
    gmail_ingest.poll_once; unprocessed messages stay unmarked so they retry."""
    service = gmail_client.build_service()
    unprocessed = []
    for sender in PETCOVER_STATUS_SENDERS:
        page_token = None
        while True:
            response = service.users().messages().list(
                userId="me",
                q=f"from:{sender} after:{config.PETCOVER_STATUS_SINCE}",
                maxResults=100,
                pageToken=page_token,
            ).execute()
            for item in response.get("messages", []):
                if gmail_ingest._already_processed(item["id"]):
                    continue
                message = service.users().messages().get(userId="me", id=item["id"], format="full").execute()
                unprocessed.append(message)
            page_token = response.get("nextPageToken")
            if not page_token:
                break

    # Oldest first: Gmail lists newest-first, and processing a settlement
    # before the acknowledgement it follows would leave the claim's status
    # regressed to the older event.
    unprocessed.sort(key=lambda m: int(m.get("internalDate", 0)))
    for message in unprocessed:
        headers = {h["name"]: h["value"] for h in message.get("payload", {}).get("headers", [])}
        subject = headers.get("Subject", "")
        body = gmail_client.full_message_text(service, message)
        claim_status.process_reply(message["id"], subject, body)
        gmail_ingest._mark_processed(message["id"], None)


def run_once() -> None:
    vet_detection.classify_unflagged()

    for claim in _pending_claims():
        if not invoice_matching.match_claim(claim):
            _maybe_draft_invoice_request(claim)

    with db.get_connection() as conn:
        matched_ids = [r["id"] for r in conn.execute("SELECT id FROM vet_claims WHERE status = 'matched'")]
    for claim_id in matched_ids:
        claim_forms.process_claim(claim_id)

    # Poll before notifying so status changes from fresh Petcover replies
    # push to Telegram in the same tick, not the next one.
    poll_petcover_status()
    notify_claim_states()


def start() -> None:
    scheduler.add_job(
        run_once,
        "interval",
        minutes=config.VET_CLAIM_PIPELINE_INTERVAL_MINUTES,
        id="vet-claim-pipeline",
        replace_existing=True,
    )
