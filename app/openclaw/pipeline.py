from datetime import datetime, timedelta, timezone

import http.client
import json
import logging
import os
import signal
import socket

from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

from . import claim_forms, claim_status, config, db, gmail_client, gmail_ingest, invoice_matching, llm, message_log, status_labels, telegram_bot, vet_detection
from .scheduler import scheduler

logger = logging.getLogger(__name__)


def _is_transient(exc: Exception) -> bool:
    """A network blip the next tick retries by itself, as opposed to something
    Justin has to fix. Decides WARNING vs ERROR — ERROR is reserved for
    "someone must act", so a dropped socket must not claim it."""
    if isinstance(exc, (http.client.IncompleteRead, socket.timeout, TimeoutError, ConnectionError)):
        return True
    if isinstance(exc, HttpError):
        return exc.resp.status in (429, 500, 502, 503, 504)
    return False

# Gmail auth-death alerting (ADR-0015; the mechanism was decided 2026-07-23 and
# these comments cited ADR-0011 by mistake — that ADR is about Petcover
# correlation). When the OAuth token dies
# every Gmail step fails silently in logs while Telegram still works — so make
# it loud there, but bounded: ≤5 alerts per rolling 24h, one recovery
# confirmation. State lives in ops_alerts so a container restart can't re-spam.
_GMAIL_AUTH_ALERT = "gmail_auth"
_MAX_AUTH_ALERTS_24H = 5
_GMAIL_AUTH_RECOVERY_MSG = "✅ Gmail access restored — the pipeline is reading mail again."
_POLLING_ALERT = "telegram_polling"


def _alert_rate_limited(kind: str, message: str, cap: int = _MAX_AUTH_ALERTS_24H, send_fn=None) -> bool:
    """Send an ops alert to Telegram at most `cap` times per rolling 24h.
    ops_alerts is the ledger, so a container restart can't reset the count and
    re-spam. Returns whether it actually sent."""
    send = send_fn or telegram_bot.send_message_sync
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=24)).isoformat()
    with db.get_connection() as conn:
        recent = conn.execute(
            "SELECT COUNT(*) FROM ops_alerts WHERE kind = ? AND sent_at >= ?", (kind, cutoff)
        ).fetchone()[0]
        if recent < cap:
            conn.execute("INSERT INTO ops_alerts (kind, sent_at) VALUES (?, ?)", (kind, now.isoformat()))
    if recent >= cap:
        logger.warning("%s alert cap (%s/24h) reached, staying quiet", kind, cap)
        return False
    send(message)
    return True


def _is_gmail_auth_failure(exc: Exception) -> bool:
    """A dead/absent OAuth token, distinct from a transient Gmail API error."""
    return isinstance(exc, RefreshError) or (
        isinstance(exc, RuntimeError) and "Gmail token" in str(exc)
    )


def _ensure_gmail_auth(send_fn=None) -> bool:
    """Probe Gmail credentials once per tick (refreshes the token as a side
    effect). On auth death: send a rate-limited Telegram alert naming the
    recovery command and return False so the tick skips Gmail-dependent work.
    On success after any alert: send one 'restored' confirmation and clear the
    alert state. Returns True when Gmail is usable."""
    send = send_fn or telegram_bot.send_message_sync
    try:
        gmail_client.build_service()
    except Exception as exc:  # noqa: BLE001 — non-auth errors re-raise below
        if not _is_gmail_auth_failure(exc):
            raise
        _alert_rate_limited(
            _GMAIL_AUTH_ALERT,
            "⚠ Gmail access has stopped working — the OAuth token needs re-authorizing.\n"
            "Run: python scripts/gmail_auth.py (opens a browser; click Allow).\n"
            "Claims processing is paused until then.",
            send_fn=send,
        )
        return False

    # Success — confirm recovery exactly once if we had been alerting.
    with db.get_connection() as conn:
        had_alerts = conn.execute(
            "SELECT COUNT(*) FROM ops_alerts WHERE kind = ?", (_GMAIL_AUTH_ALERT,)
        ).fetchone()[0]
        if had_alerts:
            conn.execute("DELETE FROM ops_alerts WHERE kind = ?", (_GMAIL_AUTH_ALERT,))
    if had_alerts:
        send(_GMAIL_AUTH_RECOVERY_MSG)
    return True

# marketing.au@ deliberately excluded — not claims-relevant (design.md).
PETCOVER_STATUS_SENDERS = ["claims.au@petcovergroup.com", "requiredinfo.au@petcovergroup.com", "accounts.au@petcovergroup.com"]

# A specific Gmail draft can't be deep-linked on mobile (the #drafts/<id>
# anchor is desktop-web only, and Gmail's app URL scheme has no open-draft-by-id
# path). So notifications are self-contained — the claim summary is IN the
# message — and the link just filters Drafts by subject as a best-effort jump.
DRAFT_SEARCH_LINK = "https://mail.google.com/mail/u/0/#search/in%3Adrafts+subject%3A%22Vet+claim%22"

# Statuses worth pushing to Justin's phone. Urgent = he has to act (blocked
# claim, insurer waiting on him); the rest are informational lifecycle updates.
NOTIFY_STATUSES = (
    "matched", "drafted", "info_requested", "suspended", "acknowledged",
    "approved", "below_excess", "settled", "declined",
)


def _owed_by(claim_id: int) -> str | None:
    """Who owes the document on this claim's most recent information request."""
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT detail FROM claim_status_events WHERE claim_id = ? AND event_type = 'info_requested' "
            "ORDER BY created_at DESC LIMIT 1",
            (claim_id,),
        ).fetchone()
    return json.loads(row["detail"] or "{}").get("owed_by") if row else None


def _latest_settlement_detail(claim_id: int) -> dict:
    """The dollar breakdown for a claim's settlement. Lives on the 'approved'
    event now (the newer letter template) or on 'settled' itself (the older
    PDF-attachment style) — the later dollar-less 'payment processed' settled
    email carries neither, so check both event types and take whichever has
    figures, most recent first."""
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT detail FROM claim_status_events WHERE claim_id = ? "
            "AND event_type IN ('approved', 'settled') ORDER BY created_at DESC",
            (claim_id,),
        ).fetchall()
    for row in rows:
        detail = json.loads(row["detail"] or "{}")
        if detail.get("paid_amount") is not None:
            return detail
    return json.loads(rows[0]["detail"] or "{}") if rows else {}


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
    reference (learned from their reply), that leads — it's what their emails
    cite. Claim #ids are always included: Justin acts on them (/mark, /pet,
    replies quote them back). Before a reference exists the derived group id
    leads, so a batch is sayable from `drafted` onwards, not only after Petcover
    answers."""
    pet = group[0]["pet_name"] or "your pet"
    ids = ", ".join(f"#{c['id']}" for c in group)
    ref = group[0]["petcover_reference"] or claim_status.submission_group_id(c["id"] for c in group)
    return f"{ref} ({pet} {ids})"


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
            lines.append(f"  • #{c['id']} {date} — {service} — ${float(amount):.2f}")
        else:
            lines.append(f"  • #{c['id']} {date} — {service}")
    count = len(group)
    gid = claim_status.submission_group_id(c["id"] for c in group)
    header = f"{pet}'s vet claim {gid} — ready to send ({count} item{'s' if count > 1 else ''}, ${total:.2f})"
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
    header = f"#{claim['id']} {pet} — {claim['txn_date']}, {claim['txn_merchant']}. What condition?"
    return "\n".join([header, *_invoice_lines(claim)])


def _summarize_matched_flag(claim, label: str) -> str:
    """Explain, in plain terms, why a matched claim is still blocked — so Justin
    can act from the message instead of decoding a raw flag string."""
    flag = claim["flag"] or ""
    who = label if claim["pet_name"] else f"Unassigned claim #{claim['id']}"
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
        lines += [f" • #{m['id']} ${abs(m['txn_amount']):.2f} ({m['txn_date']})" for m in group]
        return "\n".join(lines)
    if status == "matched":  # matched claims aren't batched (no draft yet) — group is one claim
        if _needs_condition(group[0]):
            return _summarize_needs_condition(group[0])
        return _summarize_matched_flag(group[0], label)
    if status == "drafted":
        return _summarize_drafted(group)
    if status == "info_requested":
        # Who owes the document changes what Justin does — chase the clinic, or
        # answer it himself. Same wording as the dashboard chip.
        return f"⚠ {label}: {status_labels.needs(group[0], _owed_by(group[0]['id']))}."
    if status == "suspended":
        return f"⚠ {label}: suspended by Petcover — action needed."
    if status == "acknowledged":
        return f"{label}: acknowledged by Petcover."
    if status == "below_excess":
        return f"{label}: under the fixed excess — not yet payable, invoice kept on file."
    if status == "approved":
        detail = _latest_settlement_detail(group[0]["id"])
        claimed, paid = detail.get("claimed_amount"), detail.get("paid_amount")
        base = f"{label}: approved by Petcover"
        base += f" — claimed ${claimed:.2f}, paid ${paid:.2f}." if claimed is not None and paid is not None else "."
        flag = group[0]["flag"]
        return f"⚠ {base}\n{flag}" if flag and "mismatch" in flag else base
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
_REVIEW_FLAG_MARKERS = ("isn't a per-visit itemised invoice", "invoice attachment unreadable", "settlement mismatch")


def _latest_settled_email_id(claim_id: int) -> str | None:
    """The email carrying the settlement figures — 'approved' for the newer
    letter template, 'settled' for the older PDF-attachment style."""
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT raw_email_id FROM claim_status_events WHERE claim_id = ? "
            "AND event_type IN ('approved', 'settled') AND raw_email_id IS NOT NULL "
            "ORDER BY created_at DESC LIMIT 1",
            (claim_id,),
        ).fetchone()
    return row["raw_email_id"] if row else None


def _review_pdf(group) -> tuple[str, bytes] | None:
    """The vet/Petcover document behind a needs-review flag. A settlement
    mismatch carries its approval/settlement email (the breakdown Justin will
    review from — a PDF for the older style, plain text for the newer, in
    which case the fallback below simply finds nothing and sends plain text);
    matched claims know their invoice email; unreadable-flagged pending claims
    only carry the subject in the flag — recovered via a Gmail subject search,
    best-effort."""
    lead = group[0]
    email_id = lead["matched_email_id"]
    if lead["flag"] and "settlement mismatch" in lead["flag"]:
        email_id = _latest_settled_email_id(lead["id"]) or email_id
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


def reconcile_sent_invoice_requests() -> dict:
    """Justin sends invoice-request drafts himself (CLAUDE.md: never auto-send)
    and is expected to click 'mark invoice-request sent' on the dashboard
    afterward — but real usage shows that click gets missed. Missing it keeps
    invoice_request_sent_at NULL. The search window no longer depends on it
    (wide arrival window is unconditional now), but the dashboard's
    request-sent state and the drafted-flag hygiene still do. Detected here
    via Gmail's own SENT/DRAFT labels on
    the stored message id — unambiguous, no Sent-folder text-matching needed.
    Runs every pipeline tick (every VET_CLAIM_PIPELINE_INTERVAL_MINUTES), so
    the daily-check ask is covered many times over.

    Returns {checked, confirmed_sent, stale_drafts} so the Telegram agent can
    report what actually changed — "go through my sent emails and update the
    status" is a real request this answers, and it needs an answer, not a
    silent sweep."""
    result = {"checked": 0, "confirmed_sent": [], "stale_drafts": []}
    with db.get_connection() as conn:
        # keyed on draft_id, not the flag — error/unreadable flags can overwrite
        # 'invoice_request_drafted' without meaning the draft went away
        rows = conn.execute(
            "SELECT id, draft_id FROM vet_claims WHERE status = 'pending_match' "
            "AND invoice_request_sent_at IS NULL AND draft_id IS NOT NULL"
        ).fetchall()
    if not rows:
        return result
    result["checked"] = len(rows)

    service = gmail_client.build_service()
    now = datetime.now(timezone.utc).isoformat()
    for row in rows:
        try:
            message = service.users().messages().get(userId="me", id=row["draft_id"], format="minimal").execute()
        except HttpError as exc:
            if exc.resp.status == 404:
                # The draft was deleted from Gmail — retrying forever just spams
                # the log every tick (confirmed live: claim 17, 10+/day). Distinct
                # from a transient failure: this can never resolve itself, so
                # clear it and tell Justin to send a fresh invoice request.
                with db.get_connection() as conn:
                    conn.execute(
                        "UPDATE vet_claims SET draft_id = NULL, "
                        "flag = 'invoice-request draft was deleted from Gmail — send a fresh one', "
                        "updated_at = ? WHERE id = ?",
                        (now, row["id"]),
                    )
                logger.warning("reconcile: draft %s for claim %s no longer exists — cleared", row["draft_id"], row["id"])
                result["stale_drafts"].append(row["id"])
            else:
                logger.warning("reconcile: couldn't fetch draft %s for claim %s: %s", row["draft_id"], row["id"], exc)
            continue
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
            result["confirmed_sent"].append(row["id"])
    return result


def nudge_stale_actions() -> dict:
    """Once-daily reminder of actions that have gone stale.

    notify_claim_states is a change-feed — it dedupes on (status, flag), so a
    claim that stays outstanding is announced once and never again. That's how
    two drafted claims sat unsent for three days in silence. This is the
    state-based counterpart: one message covering everything old, rather than
    re-notifying per claim."""
    from . import claim_card, claim_status, telegram_bot

    actions = claim_status.pending_actions()
    stale = [a for a in actions if a["actionable"] and a["age_days"] >= config.ACTION_NUDGE_DAYS]
    if not stale:
        return {"sent": False, "stale": 0}
    oldest = stale[0]
    caption = (
        f"{len(stale)} action(s) still waiting — oldest is #{oldest['claim_id']} "
        f"({oldest['age_days']}d). Send /actions for the cards."
    )
    telegram_bot.send_photo_sync(caption, claim_card.render_actions_summary(actions, shown=len(stale)))
    logger.info("nudge: reminded about %s stale actions", len(stale))
    return {"sent": True, "stale": len(stale)}


def _visit_line(invoice_matching, requested_date: str | None) -> str:
    """Which visit the requested date names, for the chase message.

    An invoice number is what a clinic can look up; a date makes them search. The
    visit is usually on a DIFFERENT claim from the one the letter is about (live:
    the request sits on claim #8, a 2 April charge, and the date is claim #6's
    invoice 1000229 — a later visit for the same condition). Says so explicitly,
    because the invoice identifies the visit and is NOT the document requested.

    Also says WHAT matched. An invoice's own date is not necessarily the date of
    the treatment on it, so "invoice 1000229 matched on its invoice date" is a
    weaker claim than pinpointing the consult — and it is the only claim the held
    documents support: no line item on file carries a date that differs from its
    invoice's own. Stating the weaker thing is the point (see `_invoice_dates`)."""
    if not requested_date:
        return "visit: no date stated in the letter"
    hits = invoice_matching.find_visit_by_date(requested_date)
    if not hits:
        return f"visit: {requested_date} — no invoice on file for that date"
    parts = []
    for h in hits:
        who = f"claim #{h['claim_id']}" if h["claim_id"] else "no claim on file"
        number = f"invoice {h['invoice_number']}" if h["invoice_number"] else "invoice number unknown"
        how = h.get("matched_on") or "invoice date"
        parts.append(f"{number} ({who}, matched on its {how})")
    return f"visit: {requested_date} — " + ", ".join(parts)


def nudge_unanswered_vet_requests(send_fn=None) -> dict:
    """Monday morning: every information request the vet owes and nobody answered.

    Separate from nudge_stale_actions on purpose. That one is a daily summary keyed
    on charge age and reports only the oldest item; a vet chase needs the clinic's
    address, the document's name, and the days left on the treatment-anchored
    deadline — and it needs a weekday, because a clinic chased on a Sunday does
    nothing. Two live requests sat a week producing no message at all.

    Silent when there is nothing outstanding: a weekly "nothing to do" is how a
    channel becomes one Justin stops reading."""
    from . import claim_status, invoice_matching, telegram_bot

    outstanding = claim_status.unanswered_vet_requests()
    if not outstanding:
        logger.info("vet nudge: nothing outstanding")
        return {"sent": False, "outstanding": 0}
    lines = [f"{len(outstanding)} vet info request(s) unanswered:"]
    for r in outstanding:
        document = r["requested_document"] or "document not stated in the letter"
        clinic = r["clinic"] or "clinic unknown"
        contact = f" ({r['clinic_email']})" if r["clinic_email"] else ""
        age = f"{r['days_outstanding']}d ago" if r["days_outstanding"] is not None else "date unknown"
        lines.append(
            f" • #{r['claim_id']} {r['pet_name'] or 'no pet'} — {clinic}{contact}\n"
            f"   needs: {document}\n"
            f"   {_visit_line(invoice_matching, r['requested_document_date'])}\n"
            f"   asked {age}, {r['days_left']}d until the 1-year claim deadline"
            f" (treated {r['treated_on']}{'' if r['treatment_date_known'] else ', assumed = charge date'})"
        )
    text = "\n".join(lines)
    (send_fn or telegram_bot.send_message_sync)(text)
    logger.info("vet nudge: reminded about %s unanswered request(s)", len(outstanding))
    return {"sent": True, "outstanding": len(outstanding), "text": text}


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


def _latest_event_id() -> int:
    with db.get_connection() as conn:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) AS max_id FROM claim_status_events").fetchone()
    return row["max_id"]


def _claims_touched_since(event_id: int) -> list[int]:
    """Which claims gained an event after `event_id`. Reads the append-only log
    rather than diffing statuses, so an event that records something without
    moving the status (unclassified, mismatch) is still reported."""
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT claim_id FROM claim_status_events "
            "WHERE id > ? AND claim_id IS NOT NULL ORDER BY claim_id",
            (event_id,),
        ).fetchall()
    return [r["claim_id"] for r in rows]


def poll_petcover_status(reread: bool = False, since: str | None = None) -> dict:
    """Polls Petcover's claims-relevant senders for status replies (ack, info
    request, suspended, settled, declined) and records them via claim_status.
    Raises on Gmail API failure — same retry-next-interval behavior as
    gmail_ingest.poll_once; unprocessed messages stay unmarked so they retry.

    Returns {checked, events, claims_changed} so an on-demand caller (the chat
    agent's poll_petcover_now) can say what actually changed instead of "done".
    The scheduled tick ignores the return value.

    `reread=True` ignores the `processed_emails` guard so a classification or
    extraction fix can be applied to mail already ingested — without it, every
    such fix only ever helps the *next* letter, and the four live
    misclassifications found 2026-07-27 would stay wrong permanently. This
    reverses `telegram-agent-reach`'s deliberate exclusion of force-reprocessing,
    which was correct while a re-read meant duplicate events: `process_reply` now
    skips any (email, claim, event) already logged, so a re-read records only
    what is genuinely new and cannot re-write a status or resurrect a dismissed
    settlement mismatch. Re-reads deliberately do NOT re-mark `processed_emails`
    (the row is already there) and stay bounded by `since`."""
    before_event_id = _latest_event_id()
    service = gmail_client.build_service()
    unprocessed = []
    for sender in PETCOVER_STATUS_SENDERS:
        page_token = None
        while True:
            response = service.users().messages().list(
                userId="me",
                q=f"from:{sender} after:{since or config.PETCOVER_STATUS_SINCE}",
                maxResults=100,
                pageToken=page_token,
            ).execute()
            for item in response.get("messages", []):
                if not reread and gmail_ingest._already_processed(item["id"]):
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
        # From: classifies the dedicated required-info channel; To:/Cc: says who
        # owes any requested document (claims.au@ sends both kinds, so the sender
        # cannot answer that).
        recipients = ", ".join(filter(None, (headers.get("To", ""), headers.get("Cc", ""))))
        claim_status.process_reply(message["id"], subject, body, headers.get("From", ""), recipients)
        if not reread:
            gmail_ingest._mark_processed(message["id"], None)

    return {
        "checked": len(unprocessed),
        "events": _latest_event_id() - before_event_id,
        "claims_changed": _claims_touched_since(before_event_id),
    }


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


def _watchdog_telegram_polling(exit_fn=None) -> bool:
    """The updater task is fire-and-forget: nothing awaits it, so when it dies
    (a host suspend killing the long poll is the observed case) inbound stops
    with no log line and taps vanish. Restarting the whole process is the honest
    fix — a fresh event loop, updater and Gmail service, no half-restarted
    state. compose has restart: unless-stopped, so SIGTERM means "come back".

    Sending still works while the updater is dead (separate HTTP call), so the
    alert genuinely reaches the phone before we go down. Returns whether it
    triggered a restart."""
    if telegram_bot.polling_alive() is not False:
        return False
    logger.error("Telegram polling is DOWN — inbound messages are being lost. Restarting the process.")
    try:
        _alert_rate_limited(
            _POLLING_ALERT,
            "⚠ Telegram polling had stopped — anything you sent may not have been received.\n"
            "Restarting now; re-send or re-tap whatever didn't take effect.",
        )
    except Exception:  # noqa: BLE001 — the restart matters more than the notification
        logger.exception("could not send the polling-down alert")
    (exit_fn or _sigterm_self)()
    return True


def _sigterm_self() -> None:
    os.kill(os.getpid(), signal.SIGTERM)


def compare_state_projection() -> list[dict]:
    """Shadow mode: fold every claim's events and compare to the stored column.

    WARNING, not ERROR — ADR-0015 reserves ERROR for "Justin must act", and he
    cannot fix a projection bug. Reads only, writes nothing; the whole point of
    the phase is that the two sources are compared rather than assumed equal.
    Before the backfill a non-zero count is expected."""
    try:
        disagreements = claim_status.state_projection_disagreements()
    except Exception:
        # This is diagnostic-only code that runs BEFORE the Gmail check and before
        # `notify_claim_states` — the sole push channel for the flags `apply_event`
        # writes. Unguarded, a malformed `detail` in the fold would kill the whole
        # tick and take delivery of its own refusals with it: an observer able to
        # break what it observes. WARNING, not ERROR: Justin cannot fix a fold bug.
        logger.warning("state projection: comparison failed, skipping it this tick", exc_info=True)
        return []
    if disagreements:
        logger.warning(
            "state projection: %d claim(s) disagree with the stored status: %s",
            len(disagreements),
            "; ".join(f"#{d['claim_id']} stored={d['stored']} projected={d['projected']}" for d in disagreements),
        )
    return disagreements


def run_once() -> None:
    _watchdog_telegram_polling()
    vet_detection.classify_unflagged()
    compare_state_projection()

    # Every remaining step reads or writes Gmail — if the token is dead, alert
    # (loudly, on Telegram) and skip them rather than fail silently in logs.
    if not _ensure_gmail_auth():
        return

    reconcile_sent_invoice_requests()

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
            # ERROR means Justin must act. A dropped Gmail connection is retried
            # by the next tick unaided, so it's a WARNING without a traceback —
            # it was previously logging a full stack trace and reading like a
            # crisis (real case: IncompleteRead on claim 5).
            if _is_transient(exc):
                logger.warning("matching: claim %s hit a transient error, retrying next tick: %s", claim["id"], exc)
            else:
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
    interval_minutes = config.VET_CLAIM_PIPELINE_INTERVAL_MINUTES
    scheduler.add_job(
        run_once,
        "interval",
        minutes=interval_minutes,
        id="vet-claim-pipeline",
        replace_existing=True,
        # Waking from sleep, APScheduler's default grace of 1s means the missed
        # run is SKIPPED — the pipeline then sat idle until the next interval.
        # coalesce collapses a backlog into one run instead of firing N times.
        coalesce=True,
        misfire_grace_time=interval_minutes * 60,
    )
    scheduler.add_job(
        nudge_stale_actions,
        "cron",
        hour=config.ACTION_NUDGE_HOUR,
        id="stale-action-nudge",
        replace_existing=True,
        coalesce=True,
        misfire_grace_time=3600,
    )
    # Weekly, not daily, and its own job: a vet chase happens on a weekday and
    # wants the clinic + document + deadline, which the daily summary has no room
    # for. Same coalesce/grace as above — a machine asleep at the fire time would
    # otherwise skip a whole week, not a day.
    scheduler.add_job(
        nudge_unanswered_vet_requests,
        "cron",
        day_of_week=config.VET_NUDGE_DAY,
        hour=config.ACTION_NUDGE_HOUR,
        id="vet-request-nudge",
        replace_existing=True,
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        message_log.expire_queue,
        "cron",
        hour=config.ACTION_NUDGE_HOUR,
        id="message-queue-expiry",
        replace_existing=True,
        coalesce=True,
        misfire_grace_time=3600,
    )
