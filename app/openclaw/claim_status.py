import json
import re
from datetime import datetime, timezone

from . import db

# "Automatic reply: ..." fires instantly on submission, before the real
# Acknowledgement Letter (1-2 business days later per its own boilerplate) —
# noise, not a status event. Distinct from "unclassified" (a real reply we
# couldn't classify) so it never shows up needing manual review.
IGNORE_KEYWORDS = ["automatic reply"]

# Ordered: first match wins. Checked against subject first, then body as a
# fallback for subjects that don't carry a clean keyword (confirmed real
# patterns from the 201-email survey + one full dry-run lifecycle).
SUBJECT_KEYWORDS = [
    ("acknowledged", ["acknowledgement letter"]),
    ("suspended", ["suspended"]),
    (
        "info_requested",
        [
            "request for information",
            "request for invoice",
            "request for consult note",
            "request for completed claim form",
            "request for itemized invoice",
            "request for cf",
        ],
    ),
    ("settled", ["settlement eft", "claim settlement"]),
    ("declined", ["declined"]),
]

# Petcover's claim-reference format changed 2024->2026 (GABR-#### / ELD-##-####
# old, DC1-##-#### new) — both confirmed in real emails, extracted via the
# context phrase that precedes them rather than a bare pattern, since a bare
# "GABR-0305"-shaped regex would also match inside the policy number
# (GABR-0306-DC1-00000001R).
REFERENCE_CONTEXT_PATTERNS = [
    r"Claim Number\s+([A-Za-z0-9-]+)",
    r"Claim Reference[:\s]+([A-Za-z0-9-]+)",
    r"Petcover Claim\s+([A-Za-z0-9-]+)",
]

# Petcover's own emails have used a nickname inconsistent with our records at
# least once (real: "Ari" for Aari) — checked in addition to the exact name.
PET_NICKNAMES = {"Aari": ["Ari"]}

# Statuses meaning "submitted to Petcover, reply expected" — fallback
# correlation only considers these. Deliberately NOT date-windowed: a claim's
# transaction can be a year older than the submission (real case: Aug 2025
# invoices submitted Jul 2026), so txn-date proximity would reject genuine
# matches.
CORRELATABLE_STATUSES = ("sent", "acknowledged", "info_requested", "suspended", "settled", "declined")


def _match_keywords(text: str) -> str | None:
    lowered = text.lower()
    if any(kw in lowered for kw in IGNORE_KEYWORDS):
        return "ignore"
    for event_type, keywords in SUBJECT_KEYWORDS:
        if any(kw in lowered for kw in keywords):
            return event_type
    return None


def classify(subject: str, body: str) -> str:
    return _match_keywords(subject) or _match_keywords(body) or "unclassified"


def extract_reference(text: str) -> str | None:
    for pattern in REFERENCE_CONTEXT_PATTERNS:
        match = re.search(pattern, text)
        if match:
            return match.group(1).rstrip(".,")
    return None


def extract_settlement_amounts(text: str) -> dict:
    """Settlement $ breakdown lives only in the PDF attachment, not the email
    body (confirmed via dry-run) — call with the PDF-extracted text."""
    result = {}
    claimed = re.search(r"Amount Claimed\s*\$?([\d,]+\.\d{2})", text)
    payable = re.search(r"Total Payable\s*:?\s*\$?([\d,]+\.\d{2})", text)
    if claimed:
        result["claimed_amount"] = float(claimed.group(1).replace(",", ""))
    if payable:
        result["paid_amount"] = float(payable.group(1).replace(",", ""))
    return result


def _mentions_pet(text: str, pet_name: str) -> bool:
    candidates = [pet_name] + PET_NICKNAMES.get(pet_name, [])
    return any(re.search(rf"\b{re.escape(c)}\b", text, re.IGNORECASE) for c in candidates)


def find_claims_by_reference(reference: str) -> list:
    """A batch submission (up to 4 invoices, one claim document) is several
    vet_claims rows sharing one Petcover reference — events apply to all."""
    with db.get_connection() as conn:
        return conn.execute(
            "SELECT * FROM vet_claims WHERE petcover_reference = ?", (reference,)
        ).fetchall()


def find_claims_by_pet(body: str) -> tuple[list, bool]:
    """Fallback correlation for the first event on a claim, before a reference
    is known. Returns (claims, ambiguous). Several matches sharing one
    draft_id are ONE submission (a claim batch), not an ambiguity; matches
    spanning different submissions are ambiguous and nothing is picked —
    caller must not guess."""
    with db.get_connection() as conn:
        candidates = conn.execute(
            "SELECT vet_claims.*, pets.name AS pet_name "
            "FROM vet_claims JOIN pets ON pets.id = vet_claims.pet_id "
            "WHERE vet_claims.petcover_reference IS NULL "
            f"AND vet_claims.status IN ({','.join('?' * len(CORRELATABLE_STATUSES))})",
            CORRELATABLE_STATUSES,
        ).fetchall()
    matches = [c for c in candidates if _mentions_pet(body, c["pet_name"])]
    if not matches:
        return [], False
    draft_ids = {c["draft_id"] for c in matches}
    if len(matches) == 1 or (len(draft_ids) == 1 and None not in draft_ids):
        return matches, False
    return [], True


def _record_event(claim_id: int | None, event_type: str, email_id: str | None, detail: dict) -> int:
    with db.get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO claim_status_events (claim_id, event_type, raw_email_id, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (claim_id, event_type, email_id, json.dumps(detail), datetime.now(timezone.utc).isoformat()),
        )
        return cur.lastrowid


def process_reply(email_id: str, subject: str, body: str) -> None:
    """Classifies one Petcover reply, correlates it to the claim(s) of one
    submission, and records the event per claim. Never guesses a claim to
    attach an ambiguous reply to."""
    event_type = classify(subject, body)
    if event_type == "ignore":
        return

    reference = extract_reference(subject) or extract_reference(body)
    claims = find_claims_by_reference(reference) if reference else []
    ambiguous = False
    if not claims:
        # Reference may be present in the text but not yet learned on any
        # claim row (first event on a claim) — fall back regardless of
        # whether a reference string was extracted, not only when absent.
        claims, ambiguous = find_claims_by_pet(body)

    detail = {"subject": subject}
    if event_type == "settled":
        detail.update(extract_settlement_amounts(body))

    if not claims:
        flag = "needs manual link — ambiguous pet match" if ambiguous else "needs manual link — no claim matched"
        _record_event(None, event_type, email_id, {**detail, "flag": flag})
        return

    now = datetime.now(timezone.utc).isoformat()
    for claim in claims:
        _record_event(claim["id"], event_type, email_id, detail)
        with db.get_connection() as conn:
            # "unclassified" is a review queue entry, not a lifecycle stage —
            # writing it to status would regress e.g. an acknowledged claim.
            updates = ["updated_at = ?"] if event_type == "unclassified" else ["status = ?", "updated_at = ?"]
            params = [now] if event_type == "unclassified" else [event_type, now]
            if reference and not claim["petcover_reference"]:
                updates.append("petcover_reference = ?")
                params.append(reference)
            if event_type == "acknowledged" and not reference and not claim["petcover_reference"]:
                # spec: never guess or discard — flag visibly instead
                updates.append("flag = ?")
                params.append("unclassified — reference format not recognized")
            conn.execute(f"UPDATE vet_claims SET {', '.join(updates)} WHERE id = ?", (*params, claim["id"]))


def link_event(event_id: int, claim_id: int) -> bool:
    """Manually attaches an unlinked event to a claim (the dashboard's answer
    to 'needs manual link'). Link only — deliberately does NOT rewrite the
    claim's status: a late-linked old email must not regress a settled claim.
    Returns False when the event or claim doesn't exist or is already linked."""
    with db.get_connection() as conn:
        event = conn.execute("SELECT * FROM claim_status_events WHERE id = ?", (event_id,)).fetchone()
        claim = conn.execute("SELECT 1 FROM vet_claims WHERE id = ?", (claim_id,)).fetchone()
        if event is None or event["claim_id"] is not None or claim is None:
            return False
        conn.execute("UPDATE claim_status_events SET claim_id = ? WHERE id = ?", (claim_id, event_id))
    return True


def mark_sent(claim_id: int) -> dict:
    """Advances drafted->sent, which is what starts Petcover reply polling for
    the claim. A batch submission is several claims sharing one draft — sending
    that one email sends them all, so one action advances the whole group.
    Shared by the dashboard route and the Telegram /sent command."""
    now = datetime.now(timezone.utc).isoformat()
    with db.get_connection() as conn:
        claim = conn.execute("SELECT status, draft_id FROM vet_claims WHERE id = ?", (claim_id,)).fetchone()
        if claim is None:
            return {"ok": False, "message": f"No claim #{claim_id} found."}
        if claim["status"] != "drafted":
            return {"ok": False, "message": f"Claim #{claim_id} isn't drafted (status: {claim['status']})."}
        if claim["draft_id"]:
            cur = conn.execute(
                "UPDATE vet_claims SET status = 'sent', updated_at = ? WHERE draft_id = ? AND status = 'drafted'",
                (now, claim["draft_id"]),
            )
        else:
            cur = conn.execute(
                "UPDATE vet_claims SET status = 'sent', updated_at = ? WHERE id = ? AND status = 'drafted'",
                (now, claim_id),
            )
        count = cur.rowcount
    suffix = f" ({count} claims in this submission)" if count > 1 else ""
    return {"ok": True, "message": f"Claim #{claim_id} marked sent{suffix} — Petcover replies now tracked."}


def confirm_resolved(claim_id: int) -> None:
    _record_event(claim_id, "confirmed_resolved", None, {})


def dashboard_lists() -> dict:
    """Event-domain rollups for the dashboard: needs_action (info_requested/
    suspended not yet confirmed resolved — later events, even settled, don't
    clear it), settled reconciliation (our claimable vs Petcover's paid), and
    the manual-review queue (uncorrelated events + unclassified replies)."""
    with db.get_connection() as conn:
        events = conn.execute("SELECT * FROM claim_status_events ORDER BY created_at").fetchall()
        claims_by_id = {
            r["id"]: r
            for r in conn.execute(
                "SELECT vet_claims.*, pets.name AS pet_name FROM vet_claims "
                "LEFT JOIN pets ON pets.id = vet_claims.pet_id"
            ).fetchall()
        }

    events_by_claim: dict[int, list] = {}
    review_queue = []
    for event in events:
        if event["claim_id"] is None or event["event_type"] == "unclassified":
            review_queue.append(event)
        if event["claim_id"] is not None:
            events_by_claim.setdefault(event["claim_id"], []).append(event)

    needs_action = []
    settled_reconciliation = []
    for claim_id, claim_events in events_by_claim.items():
        claim = claims_by_id.get(claim_id)
        if claim is None:
            continue
        last_flag_idx = max(
            (i for i, e in enumerate(claim_events) if e["event_type"] in ("info_requested", "suspended")),
            default=None,
        )
        if last_flag_idx is not None and not any(
            e["event_type"] == "confirmed_resolved" for e in claim_events[last_flag_idx + 1 :]
        ):
            needs_action.append({"claim": claim, "events": claim_events})
        for event in claim_events:
            if event["event_type"] == "settled":
                detail = json.loads(event["detail"] or "{}")
                invoice = json.loads(claim["invoice_data"] or "{}")
                # our own record of what was claimed, not Petcover's figure
                claimed = invoice.get("claimable_amount") or invoice.get("amount") or detail.get("claimed_amount")
                settled_reconciliation.append(
                    {"claim": claim, "claimed_amount": claimed, "paid_amount": detail.get("paid_amount")}
                )

    return {
        "needs_action": needs_action,
        "settled_reconciliation": settled_reconciliation,
        "unclassified": review_queue,
    }
