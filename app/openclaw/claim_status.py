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

# A Condition Thread's claim is done at these statuses: a later letter reusing
# the thread's reference (Petcover reuses it for years) must NEVER reopen them.
# Shared with pipeline notify so "terminal" means one thing everywhere.
TERMINAL_STATUSES = ("settled", "declined")

# Statuses meaning "submitted to Petcover, still awaiting the first correlating
# reply" — the pool ack-correlation draws from. Deliberately NOT date-windowed:
# a claim's transaction can be a year older than its submission (real: Aug 2025
# invoices submitted Jul 2026), so txn-date proximity would reject real matches.
AWAITING_REPLY_STATUSES = ("sent", "acknowledged", "info_requested", "suspended")

# Policy math (ADR-0011). Per-condition-thread excess and per-pet annual cap,
# both reset on the pet's policy anniversary. $2 tolerance absorbs rounding.
POLICY_EXCESS = 150.00
ANNUAL_CAP = 10000.00
SETTLEMENT_TOLERANCE = 2.00


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


def extract_sr(text: str, reference: str | None) -> int | None:
    """Petcover's per-document serial ("DC1-27-5628 SR1", "... Sr 3"). Read only
    where it sits right after the reference — a bare 'Sr N' elsewhere in a
    letter carries no thread meaning and must not misfire."""
    if not reference:
        return None
    match = re.search(re.escape(reference) + r"\s*SR\s*0*(\d+)", text, re.IGNORECASE)
    return int(match.group(1)) if match else None


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


# Every correlation query carries the claim's transaction date as _txn_date so
# per-Sr assignment (oldest-txn-first) works on any returned row uniformly.
_CLAIM_SELECT = (
    "SELECT vc.*, bt.date AS _txn_date FROM vet_claims vc "
    "JOIN bank_transactions bt ON bt.id = vc.transaction_id"
)


def find_claim_by_reference_and_sr(reference: str, sr: int) -> list:
    """The single claim a (reference, Sr) letter cites — Petcover's serial pins
    one document within a Condition Thread."""
    with db.get_connection() as conn:
        return conn.execute(
            f"{_CLAIM_SELECT} WHERE vc.petcover_reference = ? AND vc.petcover_sr = ?",
            (reference, sr),
        ).fetchall()


def find_claims_by_reference(reference: str, include_terminal: bool = False) -> list:
    """Claims sharing a Petcover reference are one Condition Thread (the ref is
    reused for the life of the condition). A reference-only event touches the
    thread's non-terminal claims only — settled/declined claims are finished and
    a later reference-reuse letter must never reopen them."""
    with db.get_connection() as conn:
        rows = conn.execute(f"{_CLAIM_SELECT} WHERE vc.petcover_reference = ?", (reference,)).fetchall()
    return rows if include_terminal else [r for r in rows if r["status"] not in TERMINAL_STATUSES]


def _submission_key(claim) -> str:
    return claim["draft_id"] or f"claim-{claim['id']}"


def correlate_ack(text: str) -> list:
    """Fallback correlation when no stored reference matches (an ack teaching the
    reference, or an early reply). Candidates are un-referenced, still-awaiting
    claims for the pet the letter names (nickname-tolerant), grouped into
    submissions by draft_id. Justin's rule: if the letter's text carries a
    submission's own condition text, that decides it; otherwise attribute it to
    the most-recently-sent awaiting submission (Petcover re-conditions documents,
    so their printed condition is NOT matched against — the recency rule wins and
    the claim's condition_text is left untouched). Returns one submission's claims
    (possibly several sharing a draft), or [] when no pet matches."""
    with db.get_connection() as conn:
        candidates = conn.execute(
            "SELECT vc.*, p.name AS pet_name, bt.date AS _txn_date "
            "FROM vet_claims vc JOIN pets p ON p.id = vc.pet_id "
            "JOIN bank_transactions bt ON bt.id = vc.transaction_id "
            "WHERE vc.petcover_reference IS NULL "
            f"AND vc.status IN ({','.join('?' * len(AWAITING_REPLY_STATUSES))})",
            AWAITING_REPLY_STATUSES,
        ).fetchall()
    candidates = [c for c in candidates if _mentions_pet(text, c["pet_name"])]
    if not candidates:
        return []

    submissions: dict[str, list] = {}
    for c in candidates:
        submissions.setdefault(_submission_key(c), []).append(c)

    lowered = text.lower()
    by_condition = [
        claims
        for claims in submissions.values()
        if any(c["condition_text"] and c["condition_text"].lower() in lowered for c in claims)
    ]
    if len(by_condition) == 1:
        return by_condition[0]
    # recency fallback: the submission whose most recent claim update is latest
    # (proxy for most-recently-sent). Attaching learns the reference, so the
    # submission leaves the pool — two same-day acks land on distinct submissions.
    return max(submissions.values(), key=lambda claims: max(c["updated_at"] for c in claims))


def _claim_for_sr(submission_claims: list) -> object:
    """Within a multi-claim submission, a per-Sr letter attaches to the oldest-
    transaction claim not yet serialized — Petcover's serials run oldest-first,
    and acks arrive in serial order (poll processes oldest-first)."""
    unserialized = [c for c in submission_claims if c["petcover_sr"] is None]
    pool = unserialized or submission_claims
    return min(pool, key=lambda c: (c["_txn_date"] or "", c["id"]))


def _record_event(claim_id: int | None, event_type: str, email_id: str | None, detail: dict) -> int:
    with db.get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO claim_status_events (claim_id, event_type, raw_email_id, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (claim_id, event_type, email_id, json.dumps(detail), datetime.now(timezone.utc).isoformat()),
        )
        return cur.lastrowid


def process_reply(email_id: str, subject: str, body: str) -> None:
    """Classifies one Petcover reply and routes it to the claim(s) it concerns.
    Routing precedence: (reference, Sr) → the one cited claim; reference-only →
    the thread's non-terminal claims; no stored reference → ack correlation by
    pet + condition + recency. Never guesses across Condition Threads, and never
    reopens a settled/declined claim."""
    event_type = classify(subject, body)
    if event_type == "ignore":
        return

    text = f"{subject}\n{body}"
    reference = extract_reference(subject) or extract_reference(body)
    sr = extract_sr(text, reference)

    claims: list = []
    learn_sr = False
    if reference and sr is not None:
        exact = find_claim_by_reference_and_sr(reference, sr)
        if exact:
            claims = exact  # the serial is already recorded — direct hit
        else:
            # This serial isn't recorded yet. Its claim is an un-serialized
            # sibling (still un-referenced), so find the submission by pet +
            # condition first; only fall back to the known thread if that finds
            # nothing (e.g. a serial we never captured on an already-referenced
            # claim). Assign to the oldest-transaction un-serialized claim.
            pool = correlate_ack(text) or find_claims_by_reference(reference)
            if pool:
                claims = [_claim_for_sr(pool)]
                learn_sr = True
    elif reference:
        # Reference only: the thread's non-terminal claims, or — if none yet
        # hold the reference — the submission the ack is teaching it to.
        claims = find_claims_by_reference(reference) or correlate_ack(text)
    else:
        # No reference at all — pure ack/early-reply correlation.
        claims = correlate_ack(text)

    detail = {"subject": subject}
    if event_type == "settled":
        detail.update(extract_settlement_amounts(body))

    if not claims:
        _record_event(None, event_type, email_id, {**detail, "flag": "needs manual link — no claim matched"})
        return

    now = datetime.now(timezone.utc).isoformat()
    for claim in claims:
        settlement_flag = _validate_settlement(claim, detail.get("paid_amount")) if event_type == "settled" else None
        _record_event(claim["id"], event_type, email_id, detail)
        with db.get_connection() as conn:
            # "unclassified" is a review queue entry, not a lifecycle stage —
            # writing it to status would regress e.g. an acknowledged claim.
            updates = ["updated_at = ?"] if event_type == "unclassified" else ["status = ?", "updated_at = ?"]
            params = [now] if event_type == "unclassified" else [event_type, now]
            if reference and not claim["petcover_reference"]:
                updates.append("petcover_reference = ?")
                params.append(reference)
            if learn_sr and sr is not None and claim["petcover_sr"] is None:
                updates.append("petcover_sr = ?")
                params.append(sr)
            if settlement_flag:
                updates.append("flag = ?")
                params.append(settlement_flag)
            elif event_type == "acknowledged" and not reference and not claim["petcover_reference"]:
                # spec: never guess or discard — flag visibly instead
                updates.append("flag = ?")
                params.append("unclassified — reference format not recognized")
            conn.execute(f"UPDATE vet_claims SET {', '.join(updates)} WHERE id = ?", (*params, claim["id"]))


def _policy_year_start(anniversary_mmdd: str, on: "datetime") -> "datetime":
    """Start of the policy year (anniversary→anniversary) containing `on`."""
    from datetime import date

    mm, dd = (int(x) for x in anniversary_mmdd.split("-"))
    this_year = date(on.year, mm, dd)
    on_date = on.date() if hasattr(on, "date") else on
    return this_year if on_date >= this_year else date(on.year - 1, mm, dd)


def _validate_settlement(claim, paid_amount: float | None) -> str | None:
    """Deterministic settlement check (ADR-0011): expected = claimable − excess
    (only when this thread has no earlier settled claim in the current policy
    year) bounded by the pet's remaining annual cap. Paid short of expected by
    more than the tolerance returns a human flag; else None. Never auto-disputes
    — the flag is a prompt for Justin. Degrades when the anniversary is unknown."""
    if paid_amount is None:
        return None
    invoice = json.loads(claim["invoice_data"]) if claim["invoice_data"] else {}
    claimable = invoice.get("claimable_amount")
    if claimable is None:
        claimable = invoice.get("amount")
    if claimable is None:
        return None  # nothing to compare against — don't fabricate an expectation
    claimable = float(claimable)

    now = datetime.now(timezone.utc)
    reference = claim["petcover_reference"]
    with db.get_connection() as conn:
        pet = conn.execute("SELECT policy_anniversary FROM pets WHERE id = ?", (claim["pet_id"],)).fetchone()
        thread_settlements = conn.execute(
            "SELECT e.created_at FROM claim_status_events e JOIN vet_claims v ON v.id = e.claim_id "
            "WHERE v.petcover_reference IS ? AND e.event_type = 'settled' AND e.claim_id != ?",
            (reference, claim["id"]),
        ).fetchall() if reference else []
        pet_settlements = conn.execute(
            "SELECT e.created_at, e.detail FROM claim_status_events e JOIN vet_claims v ON v.id = e.claim_id "
            "WHERE v.pet_id IS ? AND e.event_type = 'settled' AND e.claim_id != ?",
            (claim["pet_id"], claim["id"]),
        ).fetchall()

    anniversary = pet["policy_anniversary"] if pet else None
    if anniversary:
        year_start = _policy_year_start(anniversary, now)
        year_end = year_start.replace(year=year_start.year + 1)

        def _in_year(iso: str) -> bool:
            d = datetime.fromisoformat(iso).date()
            return year_start <= d < year_end

        note = ""
    else:
        # No anniversary on record: use thread-lifetime excess only, whole-history
        # cap. Any flag says the anniversary is unknown so Justin can weigh it.
        _in_year = lambda iso: True  # noqa: E731
        note = "; policy anniversary unknown, excess/cap not year-bounded"

    excess_consumed = any(_in_year(r["created_at"]) for r in thread_settlements)
    paid_this_year = sum(
        (json.loads(r["detail"] or "{}").get("paid_amount") or 0.0)
        for r in pet_settlements
        if _in_year(r["created_at"])
    )
    remaining_cap = max(0.0, ANNUAL_CAP - paid_this_year)

    expected = claimable - (0.0 if excess_consumed else POLICY_EXCESS)
    expected = max(0.0, min(expected, remaining_cap))
    if paid_amount < expected - SETTLEMENT_TOLERANCE:
        reason = "excess already deducted this policy year" if excess_consumed else "less excess"
        return f"settlement short — expected ${expected:.2f}, paid ${paid_amount:.2f} ({reason}{note})"
    return None


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
