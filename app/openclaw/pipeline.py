from datetime import datetime, timedelta, timezone

import json

from . import claim_forms, claim_status, config, db, gmail_client, gmail_ingest, invoice_matching, telegram_bot, vet_detection
from .scheduler import scheduler

# marketing.au@ deliberately excluded — not claims-relevant (design.md).
PETCOVER_STATUS_SENDERS = ["claims.au@petcovergroup.com", "requiredinfo.au@petcovergroup.com", "accounts.au@petcovergroup.com"]

# A specific Gmail draft can't be deep-linked on mobile (the #drafts/<id>
# anchor is desktop-web only, and Gmail's app URL scheme has no open-draft-by-id
# path). So notifications are self-contained — the claim summary is IN the
# message — and the link just filters Drafts by subject as a best-effort jump.
DRAFT_SEARCH_LINK = "https://mail.google.com/mail/u/0/#search/in%3Adrafts+subject%3A%22Vet+claim%22"

# Statuses worth pushing to Justin's phone. Urgent = he has to act (blocked
# claim, insurer waiting on him); the rest are informational lifecycle updates.
NOTIFY_STATUSES = ("matched", "drafted", "info_requested", "suspended", "acknowledged", "settled", "declined")


def _latest_settlement_detail(claim_id: int) -> dict:
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT detail FROM claim_status_events WHERE claim_id = ? AND event_type = 'settled' "
            "ORDER BY created_at DESC LIMIT 1",
            (claim_id,),
        ).fetchone()
    return json.loads(row["detail"] or "{}") if row else {}


def _batch_key(claim) -> str:
    """Claims sharing one draft are one submission (one Gmail draft, sent as a
    unit) — notify about them together, not once per claim."""
    return claim["draft_id"] or f"claim-{claim['id']}"


def _submission_label(group) -> str:
    """A submission's identifier for Justin. Once Petcover assigns a claim
    reference (learned from their reply), that IS the shared id across every
    claim in the batch — it's what their emails cite. Before that, label by
    pet. Internal claim ids are never shown — meaningless to Justin."""
    pet = group[0]["pet_name"] or "your pet"
    ref = group[0]["petcover_reference"]
    return f"{ref} ({pet})" if ref else pet


def _summarize_drafted(group) -> str:
    pet = group[0]["pet_name"] or "your pet"
    lines, total = [], 0.0
    for c in sorted(group, key=lambda r: r["txn_date"]):
        invoice = json.loads(c["invoice_data"]) if c["invoice_data"] else {}
        amount = invoice.get("amount")
        services = invoice.get("services")
        if isinstance(services, list):
            services = ", ".join(str(s) for s in services)
        # trim the parenthetical split-notes off the service text for brevity
        service = (services or c["condition_text"] or "claim").split(" (")[0].strip()
        date = invoice.get("date") or c["txn_date"]
        if amount is not None:
            total += float(amount)
            lines.append(f"  • {date} — {service} — ${float(amount):.2f}")
        else:
            lines.append(f"  • {date} — {service}")
    count = len(group)
    header = f"{pet}'s vet claim — ready to send ({count} item{'s' if count > 1 else ''}, ${total:.2f})"
    return "\n".join(
        [header, *lines, f'Open the Gmail app → Drafts (subject "Vet claim — {pet}"):', DRAFT_SEARCH_LINK]
    )


def _needs_condition(claim) -> bool:
    return claim["status"] == "matched" and bool(claim["flag"]) and "condition" in claim["flag"].lower()


def _summarize_needs_condition(claim) -> str:
    """Matched-but-blocked-on-condition: show what was claimed (the invoice line
    items) and why a condition is needed, so Justin can answer from the message."""
    pet = claim["pet_name"] or "your pet"
    invoice = json.loads(claim["invoice_data"]) if claim["invoice_data"] else {}
    items = invoice.get("services")
    if isinstance(items, list):
        items = ", ".join(str(s) for s in items)
    amount = invoice.get("amount")
    date = invoice.get("date") or claim["txn_date"]
    line = f"  • {date} — {items or 'vet visit'}"
    if amount is not None:
        line += f" — ${float(amount):.2f}"
    return "\n".join([
        f"{pet}'s vet claim needs a condition:",
        line,
        "Why: insurers need the illness/injury being claimed — it isn't in the invoice line items.",
        "Tap a past condition below, or tap ✏️ to type a new one.",
    ])


def _summarize_matched_flag(claim, label: str) -> str:
    """Explain, in plain terms, why a matched claim is still blocked — so Justin
    can act from the message instead of decoding a raw flag string."""
    flag = claim["flag"] or ""
    pet = claim["pet_name"]
    parts = []
    if "possible additional invoice" in flag:
        gap = flag.split("unexplained")[-1].strip() or "some amount"
        parts.append(
            f"The bank charge is {gap} more than the matched invoice covers — likely a second "
            "invoice for this payment not yet found, or a wrong match. Worth a look in Gmail."
        )
    if claim["pet_id"] is None:
        parts.append("No pet assigned yet — tap the pet below.")
    if not parts:
        parts.append(flag)
    who = label if pet else "Unassigned claim"
    return f"⚠ {who}: " + " ".join(parts)


def _summarize_group(group) -> str | None:
    status = group[0]["status"]
    label = _submission_label(group)
    if status == "matched":  # matched claims aren't batched (no draft yet) — group is one claim
        if _needs_condition(group[0]):
            return _summarize_needs_condition(group[0])
        return _summarize_matched_flag(group[0], label)
    if status == "drafted":
        return _summarize_drafted(group)
    if status == "info_requested":
        return f"⚠ {label}: Petcover requested more information — reply needed."
    if status == "suspended":
        return f"⚠ {label}: suspended by Petcover — action needed."
    if status == "acknowledged":
        return f"{label}: acknowledged by Petcover."
    if status == "declined":
        return f"{label}: declined by Petcover."
    if status == "settled":
        detail = _latest_settlement_detail(group[0]["id"])
        claimed, paid = detail.get("claimed_amount"), detail.get("paid_amount")
        if claimed is not None and paid is not None:
            return f"{label}: settled — claimed ${claimed:.2f}, paid ${paid:.2f}."
        return f"{label}: settled."
    return None


def notify_claim_states(send_fn=None) -> None:
    """Pushes a Telegram message when a claim enters a state Justin should hear
    about (blocked at matched, drafted, or any Petcover lifecycle status).
    Claims sharing one draft are summarized in a single message; a group is
    skipped when no member's (status, flag) changed since last notified.
    `send_fn` is overridable for tests (spy) — defaults to the real send."""
    send = send_fn or telegram_bot.send_message_sync
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT vc.*, p.name AS pet_name, bt.date AS txn_date, bt.amount AS txn_amount "
            "FROM vet_claims vc "
            "LEFT JOIN pets p ON p.id = vc.pet_id "
            "JOIN bank_transactions bt ON bt.id = vc.transaction_id "
            f"WHERE vc.status IN ({','.join('?' * len(NOTIFY_STATUSES))})",
            NOTIFY_STATUSES,
        ).fetchall()

    groups: dict[str, list] = {}
    for claim in rows:
        if claim["status"] == "matched" and not claim["flag"]:
            continue  # not actually blocked, nothing to tell Justin about
        groups.setdefault(_batch_key(claim), []).append(claim)

    for group in groups.values():
        changed = any(
            c["status"] != c["telegram_notified_status"] or c["flag"] != c["telegram_notified_flag"] for c in group
        )
        if not changed:
            continue
        text = _summarize_group(group)
        if text is None:
            continue
        # Attach the right inline controls: drafted → one-tap Mark-sent;
        # matched-needs-condition → past-condition pick-list + type-your-own.
        lead = group[0]
        if lead["status"] == "drafted":
            markup = telegram_bot.mark_sent_button(lead["id"])
        elif lead["status"] == "matched" and lead["pet_id"] is None:
            markup = telegram_bot.pet_keyboard(lead["id"])  # assign pet first
        elif _needs_condition(lead) and lead["pet_id"]:
            markup = telegram_bot.condition_keyboard(lead["id"], lead["pet_id"])
        else:
            markup = None
        send(text, markup)
        with db.get_connection() as conn:
            for c in group:
                conn.execute(
                    "UPDATE vet_claims SET telegram_notified_status = ?, telegram_notified_flag = ? WHERE id = ?",
                    (c["status"], c["flag"], c["id"]),
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
