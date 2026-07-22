from datetime import datetime, timedelta, timezone

import json
import logging

from . import claim_forms, claim_status, config, db, gmail_client, gmail_ingest, invoice_matching, llm, telegram_bot, vet_detection
from .scheduler import scheduler

logger = logging.getLogger(__name__)

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
    unit) — notify about them together, not once per claim. Flagged pending
    claims group by (merchant, flag) instead: six claims blocked on the same
    unreadable vet attachment are one problem, one message."""
    if claim["status"] == "pending_match":
        return f"pending:{claim['txn_merchant']}:{claim['flag']}"
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


def _invoice_lines(claim) -> list[str]:
    """The invoice line items, itemised if the extraction split them, else the
    services string broken on commas."""
    invoice = json.loads(claim["invoice_data"]) if claim["invoice_data"] else {}
    items = invoice.get("items")
    if isinstance(items, list) and items:
        out = []
        for it in items:
            amt = it.get("amount")
            desc = it.get("description", "item")
            out.append(f"  • {desc} — ${float(amt):.2f}" if amt is not None else f"  • {desc}")
        return out
    services = invoice.get("services")
    if isinstance(services, list):
        services = ", ".join(str(s) for s in services)
    return [f"  • {s.strip()}" for s in services.split(",")] if services else []


def _summarize_needs_condition(claim) -> str:
    pet = claim["pet_name"] or "your pet"
    header = f"{pet} — {claim['txn_date']}, {claim['txn_merchant']}. What condition?"
    return "\n".join([header, *_invoice_lines(claim)])


def _summarize_matched_flag(claim, label: str) -> str:
    """Explain, in plain terms, why a matched claim is still blocked — so Justin
    can act from the message instead of decoding a raw flag string."""
    flag = claim["flag"] or ""
    who = label if claim["pet_name"] else "Unassigned claim"
    lines = [f"⚠ {who} — {claim['txn_date']}, {claim['txn_merchant']}", *_invoice_lines(claim)]
    if "possible additional invoice" in flag:
        gap = flag.split("unexplained")[-1].strip() or "some amount"
        lines.append(
            f"Bank charge is {gap} more than the matched invoice — likely the wrong invoice. "
            "Tap below to reject it and re-search."
        )
    elif "condition" not in flag.lower():
        lines.append(flag)
    if claim["pet_id"] is None and "possible additional invoice" not in flag:
        lines.append("Which pet?")
    return "\n".join(lines)


def _summarize_group(group) -> str | None:
    status = group[0]["status"]
    label = _submission_label(group)
    if status == "pending_match":  # flagged-but-unmatched: surface the flag verbatim
        c = group[0]
        lines = [f"⚠ {c['txn_merchant']} — {c['flag']}", "Affected charges:"]
        lines += [f" • ${abs(m['txn_amount']):.2f} ({m['txn_date']})" for m in group]
        return "\n".join(lines)
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


def notify_split_proposals(send_fn=None) -> None:
    """Pushes the one invoice / several charges picker: shows the invoice and
    each covered charge, with a button per claim — Justin picks which claim
    carries the invoice (see invoice_matching.resolve_split_proposal). Sent
    once per proposal (notified_at)."""
    send = send_fn or telegram_bot.send_message_sync
    with db.get_connection() as conn:
        proposals = conn.execute(
            "SELECT * FROM split_proposals WHERE status = 'open' AND notified_at IS NULL"
        ).fetchall()
    for proposal in proposals:
        claim_ids = json.loads(proposal["claim_ids"])
        invoice = json.loads(proposal["invoice_json"])
        with db.get_connection() as conn:
            claims = [
                dict(r)
                for r in conn.execute(
                    f"SELECT vet_claims.id, bank_transactions.amount, bank_transactions.date, "
                    f"bank_transactions.merchant FROM vet_claims "
                    f"JOIN bank_transactions ON bank_transactions.id = vet_claims.transaction_id "
                    f"WHERE vet_claims.id IN ({','.join('?' * len(claim_ids))}) ORDER BY vet_claims.id",
                    claim_ids,
                )
            ]
        if len(claims) != len(claim_ids):
            continue
        total = float(invoice["amount"])
        combined = sum(abs(c["amount"]) for c in claims)
        primary = max(claims, key=lambda c: (abs(c["amount"]), -c["id"]))
        others = [c for c in claims if c["id"] != primary["id"]]
        lines = [
            f"🔀 One invoice paid over {len(claims)} charges — {claims[0]['merchant']}",
            f"Invoice {invoice.get('date') or '(no date)'} for ${total:.2f}:",
            *[f" • #{c['id']} — ${abs(c['amount']):.2f} ({c['date']})" for c in claims],
            f"Charges together: ${combined:.2f}.",
        ]
        if invoice.get("payments_confirmed"):
            lines.append("The invoice's own payment records list both charge amounts.")
        lines.append(
            f"Merge? #{primary['id']} will carry the invoice; "
            f"#{', #'.join(str(c['id']) for c in others)} closes as its other payment. "
            "(Petcover sees the invoice, not the bank charges — no split needed.)"
        )
        text = "\n".join(lines)
        markup = telegram_bot.merge_bill_keyboard(proposal["id"])
        # attach the invoice pages themselves so the merge can be reviewed in place
        document = None
        if send_fn is None:
            try:
                document = claim_forms.invoice_segment_pdf(proposal["email_id"], total)
            except Exception as exc:
                logger.warning("merge-proposal pdf fetch failed (proposal %s): %s", proposal["id"], exc)
        if document:
            telegram_bot.send_document_sync(text, document[1], document[0], markup)
        else:
            send(text, markup)
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE split_proposals SET notified_at = ? WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), proposal["id"]),
            )


# Flags whose alert should carry the offending PDF so Justin can review it
# from the message itself.
_REVIEW_FLAG_MARKERS = ("isn't a per-visit itemised invoice", "invoice attachment unreadable")


def _review_pdf(group) -> tuple[str, bytes] | None:
    """The vet document behind a needs-review flag. Matched claims know their
    email; unreadable-flagged pending claims only carry the subject in the flag
    — recovered via a Gmail subject search, best-effort."""
    lead = group[0]
    email_id = lead["matched_email_id"]
    if not email_id and lead["flag"] and "unreadable — " in lead["flag"]:
        subject = lead["flag"].split("unreadable — ", 1)[1]
        service = gmail_client.build_service()
        messages = service.users().messages().list(
            userId="me", q=f'subject:"{subject}" has:attachment', maxResults=1
        ).execute().get("messages", [])
        email_id = messages[0]["id"] if messages else None
    if not email_id:
        return None
    attachments = claim_forms.email_pdf_attachments(email_id)
    return attachments[0] if attachments else None


def notify_claim_states(send_fn=None) -> None:
    """Pushes a Telegram message when a claim enters a state Justin should hear
    about (blocked at matched, drafted, or any Petcover lifecycle status).
    Claims sharing one draft are summarized in a single message; a group is
    skipped when no member's (status, flag) changed since last notified.
    `send_fn` is overridable for tests (spy) — defaults to the real send."""
    send = send_fn or telegram_bot.send_message_sync
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT vc.*, p.name AS pet_name, bt.date AS txn_date, bt.amount AS txn_amount, "
            "bt.merchant AS txn_merchant "
            "FROM vet_claims vc "
            "LEFT JOIN pets p ON p.id = vc.pet_id "
            "JOIN bank_transactions bt ON bt.id = vc.transaction_id "
            f"WHERE vc.status IN ({','.join('?' * len(NOTIFY_STATUSES))}) "
            # pending claims with an actionable flag (unreadable attachment,
            # manual-match, merge pending) push too — transient LLM-outage and
            # drafted-request flags are noise, not actions
            "OR (vc.status = 'pending_match' AND vc.flag IS NOT NULL "
            "AND vc.flag != 'invoice_request_drafted' "
            "AND vc.flag NOT LIKE 'invoice extraction unavailable%' "
            "AND vc.flag NOT LIKE 'invoice matching error%')",
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
        suspicious = lead["flag"] and "possible additional invoice" in lead["flag"]
        if lead["status"] == "drafted":
            markup = telegram_bot.mark_sent_button(lead["id"])
        elif lead["status"] == "matched" and suspicious:
            markup = telegram_bot.wrong_invoice_button(lead["id"])  # bad match — fix it first
        elif lead["status"] == "matched" and lead["pet_id"] is None:
            markup = telegram_bot.pet_keyboard(lead["id"])  # assign pet first
        elif _needs_condition(lead) and lead["pet_id"]:
            multi = len(_invoice_lines(lead)) > 1
            markup = telegram_bot.condition_keyboard(lead["id"], lead["pet_id"], multi_item=multi)
        else:
            markup = None
        # Review alerts carry the offending PDF itself. Only when using the
        # real sender — a test send_fn spy stays a plain text call.
        document = None
        if send_fn is None and lead["flag"] and any(m in lead["flag"] for m in _REVIEW_FLAG_MARKERS):
            try:
                document = _review_pdf(group)
            except Exception as exc:
                logger.warning("review-pdf fetch failed for claim %s: %s", lead["id"], exc)
        if document:
            telegram_bot.send_document_sync(text, document[1], document[0], markup)
        else:
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


def _reconcile_sent_invoice_requests() -> None:
    """Justin sends invoice-request drafts himself (CLAUDE.md: never auto-send)
    and is expected to click 'mark invoice-request sent' on the dashboard
    afterward — but real usage shows that click gets missed. Missing it keeps
    invoice_request_sent_at NULL. The search window no longer depends on it
    (wide arrival window is unconditional now), but the dashboard's
    request-sent state and the drafted-flag hygiene still do. Detected here
    via Gmail's own SENT/DRAFT labels on
    the stored message id — unambiguous, no Sent-folder text-matching needed.
    Runs every pipeline tick (every VET_CLAIM_PIPELINE_INTERVAL_MINUTES), so
    the daily-check ask is covered many times over."""
    with db.get_connection() as conn:
        # keyed on draft_id, not the flag — error/unreadable flags can overwrite
        # 'invoice_request_drafted' without meaning the draft went away
        rows = conn.execute(
            "SELECT id, draft_id FROM vet_claims WHERE status = 'pending_match' "
            "AND invoice_request_sent_at IS NULL AND draft_id IS NOT NULL"
        ).fetchall()
    if not rows:
        return

    service = gmail_client.build_service()
    now = datetime.now(timezone.utc).isoformat()
    for row in rows:
        try:
            message = service.users().messages().get(userId="me", id=row["draft_id"], format="minimal").execute()
        except Exception as exc:
            # Can't confirm either way this cycle — retry next tick. Not silent:
            # a persistent failure (auth expiry, bad id) stays visible in logs.
            logger.warning("reconcile: couldn't fetch draft %s for claim %s: %s", row["draft_id"], row["id"], exc)
            continue
        labels = message.get("labelIds", [])
        if "SENT" in labels and "DRAFT" not in labels:
            with db.get_connection() as conn:
                # only clear the drafted marker — flag may hold other state
                # (e.g. unreadable-attachment) that must survive reconciling
                conn.execute(
                    "UPDATE vet_claims SET invoice_request_sent_at = ?, "
                    "flag = CASE WHEN flag = 'invoice_request_drafted' THEN NULL ELSE flag END, "
                    "updated_at = ? WHERE id = ?",
                    (now, now, row["id"]),
                )


def _maybe_draft_invoice_request(claim) -> None:
    if claim["invoice_request_sent_at"] or claim["draft_id"]:
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


# flags run_once writes on match failure — cleared before the next attempt so
# a recovered claim doesn't carry a stale error
_TRANSIENT_MATCH_FLAGS = ("invoice extraction unavailable", "invoice matching error")


def _matched_claims():
    with db.get_connection() as conn:
        return conn.execute(
            "SELECT vet_claims.*, bank_transactions.merchant AS txn_merchant, "
            "bank_transactions.date AS txn_date, bank_transactions.amount AS txn_amount, "
            "pets.claim_process_defined AS pet_process_defined "
            "FROM vet_claims "
            "JOIN bank_transactions ON bank_transactions.id = vet_claims.transaction_id "
            "LEFT JOIN pets ON pets.id = vet_claims.pet_id "
            "WHERE vet_claims.status = 'matched'"
        ).fetchall()


def _draft_matched_claims() -> None:
    """matched → drafted. First auto-extract each claim's per-visit invoice
    pages from its matched email (claim_forms.ensure_invoice_file — the step
    that used to be manual), then draft: fully-ready claims are bundled per
    pet into batches of ≤4 (the Petcover form's row limit) sharing one form +
    one Gmail draft; anything not ready goes through process_claim so its
    per-field flagging (pet/condition/invoice) still runs."""
    for claim in _matched_claims():
        try:
            claim_forms.ensure_invoice_file(claim)
        except Exception as exc:  # Gmail hiccup — retry next tick, keep the tick alive
            logger.warning("ensure_invoice_file: claim %s: %s", claim["id"], exc)

    ready_by_pet: dict[int, list] = {}
    not_ready = []
    for claim in _matched_claims():  # re-read: paths/pets may have just been set
        if (
            claim["pet_id"]
            and claim["pet_process_defined"]
            and claim["condition_text"]
            and claim["invoice_file_path"]
        ):
            ready_by_pet.setdefault(claim["pet_id"], []).append(claim)
        else:
            not_ready.append(claim)

    for claims in ready_by_pet.values():
        claims.sort(key=lambda c: (c["txn_date"], c["id"]))
        for i in range(0, len(claims), 4):
            claim_forms.process_claim_batch([c["id"] for c in claims[i : i + 4]])
    for claim in not_ready:
        claim_forms.process_claim(claim["id"])


def run_once() -> None:
    vet_detection.classify_unflagged()
    _reconcile_sent_invoice_requests()

    # One claim's failure must never starve the rest of the tick (confirmed
    # live: an extraction 429 on the first pending claim blocked Petcover
    # status polling for days). LLM outage is global, so stop *matching* only;
    # everything downstream still runs.
    for claim in _pending_claims():
        if (claim["flag"] or "").startswith(_TRANSIENT_MATCH_FLAGS):
            invoice_matching._flag_claim(claim["id"], None)
        try:
            matched = invoice_matching.match_claim(claim)
        except llm.LLMUnavailableError as exc:
            logger.warning("matching: LLM unavailable, skipping remaining matching this tick: %s", exc)
            invoice_matching._flag_claim(claim["id"], f"invoice extraction unavailable — {str(exc)[:120]}")
            break
        except Exception as exc:
            logger.exception("matching: claim %s failed", claim["id"])
            invoice_matching._flag_claim(claim["id"], f"invoice matching error — {str(exc)[:120]}")
            continue
        if not matched:
            _maybe_draft_invoice_request(claim)

    _draft_matched_claims()

    # Poll before notifying so status changes from fresh Petcover replies
    # push to Telegram in the same tick, not the next one.
    poll_petcover_status()
    notify_claim_states()
    notify_split_proposals()


def start() -> None:
    scheduler.add_job(
        run_once,
        "interval",
        minutes=config.VET_CLAIM_PIPELINE_INTERVAL_MINUTES,
        id="vet-claim-pipeline",
        replace_existing=True,
    )
