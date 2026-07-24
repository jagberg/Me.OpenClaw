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


def _policy_year(txn_date: str) -> str:
    """Policy-year key for excess/cap grouping. Calendar year of the charge —
    an approximation: Petcover's real policy-year start isn't recorded, so a
    claim near a year boundary could be grouped a year off. Refine to the true
    renewal date if/when it's stored on the pet."""
    # ponytail: calendar year, switch to renewal-anchored year if excess disputes arise
    return (txn_date or "")[:4]


def _apply_excess_and_cap(rows: list, excess, cap) -> None:
    """Fills each row's `expected` in place. Excess is drained greedily across
    a (pet, condition, year) group in charge-date order — earliest charges
    absorb it first — then the running per-year total is bounded by the cap.
    All figures are estimates (est.), never booked reimbursements: they don't
    net off what Petcover has already paid this year. Missing excess/cap →
    unavailable, never guessed."""
    if excess is None or cap is None:
        for r in rows:
            r["expected"] = {"available": False, "value": None, "note": "no policy excess/cap on file"}
        return

    # A claim with no invoice matched yet has no claimable subtotal — nothing to
    # estimate against. Flag unavailable and keep it out of the group math.
    priced = []
    for r in rows:
        if r["claimable"] is None:
            r["expected"] = {"available": False, "value": None, "note": "no invoice yet"}
        else:
            priced.append(r)

    by_condition: dict[tuple, list] = {}
    for r in priced:
        key = (r["condition_text"] or "", _policy_year(r["txn_date"]))
        by_condition.setdefault(key, []).append(r)

    year_totals: dict[str, float] = {}
    for (condition, year), group in by_condition.items():
        remaining_excess = excess
        group_claimable = sum((g["claimable"] or 0) for g in group)
        for r in sorted(group, key=lambda g: g["txn_date"] or ""):
            claimable = r["claimable"] or 0
            absorbed = min(remaining_excess, claimable)
            remaining_excess -= absorbed
            after_excess = claimable - absorbed
            # bound the running per-year total by the annual cap
            used = year_totals.get(year, 0.0)
            allowed = max(0.0, cap - used)
            value = round(min(after_excess, allowed), 2)
            year_totals[year] = used + value
            if group_claimable < excess:
                note = f"{condition or 'condition'} YTD ${group_claimable:.2f} < ${excess:.0f} excess"
            else:
                note = f"est. after ${excess:.0f} excess"
            r["expected"] = {"available": True, "value": value, "note": note, "estimate": True}


def visit_ledger() -> list:
    """Transaction-anchored rollup for the unified dashboard: one entry per vet
    bank charge (the ADR-0007 ceiling), with the claim(s) derived from it
    nested beneath. A charge with no claim yet (no invoice) is a first-class
    entry with an empty claim list. Replaces the old parallel per-status lists.

    Each entry: {txn, claims: [...], claim_count}. Each claim carries its
    claimable subtotal, expected reimbursement (excess/cap-aware estimate, or
    unavailable), status, reference, and last status event."""
    with db.get_connection() as conn:
        txns = conn.execute(
            "SELECT * FROM bank_transactions WHERE vet_flag = 1 ORDER BY date DESC, id DESC"
        ).fetchall()
        claim_rows = conn.execute(
            "SELECT vet_claims.*, pets.name AS pet_name, pets.annual_excess, pets.annual_cap, "
            "bank_transactions.date AS txn_date, bank_transactions.amount AS txn_amount, "
            "bank_transactions.merchant AS txn_merchant "
            "FROM vet_claims JOIN bank_transactions ON bank_transactions.id = vet_claims.transaction_id "
            "LEFT JOIN pets ON pets.id = vet_claims.pet_id"
        ).fetchall()
        events = conn.execute(
            "SELECT * FROM claim_status_events WHERE claim_id IS NOT NULL ORDER BY created_at"
        ).fetchall()

    last_event: dict[int, dict] = {}
    settled_paid: dict[int, float] = {}
    for e in events:
        if e["event_type"] == "unclassified":
            continue
        last_event[e["claim_id"]] = {"type": e["event_type"], "at": e["created_at"]}
        if e["event_type"] == "settled":
            paid = json.loads(e["detail"] or "{}").get("paid_amount")
            if paid is not None:
                settled_paid[e["claim_id"]] = paid

    claims_by_txn: dict[int, list] = {}
    for c in claim_rows:
        invoice = json.loads(c["invoice_data"] or "{}")
        claimable = invoice.get("claimable_amount")
        if claimable is None:
            claimable = invoice.get("amount")
        claims_by_txn.setdefault(c["transaction_id"], []).append(
            {
                "id": c["id"],
                "pet_name": c["pet_name"],
                "pet_id": c["pet_id"],
                "condition_text": c["condition_text"],
                "status": c["status"],
                "reference": c["petcover_reference"],
                "draft_id": c["draft_id"],
                "flag": c["flag"],
                "claimable": claimable,
                "txn_date": c["txn_date"],
                "annual_excess": c["annual_excess"],
                "annual_cap": c["annual_cap"],
                "last_event": last_event.get(c["id"]),
                "settled_paid": settled_paid.get(c["id"]),
            }
        )

    # Expected reimbursement, grouped per pet so each pet's excess/cap applies
    # to its own claims only.
    by_pet: dict[int, list] = {}
    for claims in claims_by_txn.values():
        for cl in claims:
            by_pet.setdefault(cl["pet_id"], []).append(cl)
    for pet_claims in by_pet.values():
        first = pet_claims[0]
        _apply_excess_and_cap(pet_claims, first["annual_excess"], first["annual_cap"])
    # Settled claims override the estimate with what Petcover actually paid.
    for pet_claims in by_pet.values():
        for cl in pet_claims:
            if cl["settled_paid"] is not None:
                cl["expected"] = {"available": True, "value": cl["settled_paid"], "note": "actual", "estimate": False}

    ledger = []
    for txn in txns:
        claims = claims_by_txn.get(txn["id"], [])
        ledger.append({"txn": txn, "claims": claims, "claim_count": len(claims)})
    return ledger


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
