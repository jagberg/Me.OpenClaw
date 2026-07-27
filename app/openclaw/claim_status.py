import json
import re
from datetime import date, datetime, timedelta, timezone

from . import claim_forms, config, db

# "Automatic reply: ..." fires instantly on submission, before the real
# Acknowledgement Letter (1-2 business days later per its own boilerplate) —
# noise, not a status event. Distinct from "unclassified" (a real reply we
# couldn't classify) so it never shows up needing manual review.
IGNORE_KEYWORDS = ["automatic reply"]

# requiredinfo.au@ is a dedicated single-purpose channel — every email from it is
# an information request, so the sender classifies it without any phrase match.
# That is what rescues the vet-addressed cover note: one sentence of body, the
# reference only in the subject, and the detail in an attachment we get no text
# for. It matched nothing and was recorded `unclassified` — the one classification
# that produces no action (confirmed live 2026-07-19 and 2026-07-27).
INFO_REQUEST_SENDER = "requiredinfo.au@petcovergroup.com"

# Ordered: first match wins. Checked against subject first, then body as a
# fallback for subjects that don't carry a clean keyword (confirmed real
# patterns from the 201-email survey + one full dry-run lifecycle).
SUBJECT_KEYWORDS = [
    ("acknowledged", ["acknowledgement letter"]),
    # "Claim Approval" precedes "payment processed"/settled and is the ONLY
    # place the dollar breakdown appears (confirmed live, Jul 2026) — the
    # later settled email carries no figures at all.
    ("approved", ["your claim has been approved", "claim has been approved"]),
    # Real subject is the generic "Petcover Insurance Claim for Ari" — this
    # phrase is body-only, confirmed live ("Claim assessment outcome: Under
    # excess ... Less Fixed excess: $105.00").
    ("below_excess", ["claim assessment outcome: under excess", "under your fixed excess"]),
    # info_requested MUST stay ahead of suspended. The request letter contains the
    # sentence "Your claim will be suspended until we have the required
    # information" — a statement about its own future, not a suspension. With
    # suspended first, every request was filed as a suspension: the live DB held
    # zero info_requested events and two suspended ones, both of which were
    # requests (confirmed 2026-07-27). A genuine "Claim suspended" letter exists
    # separately (29 Jan 2026) and carries no request wording, so the two stay
    # distinguishable — the distinction is "a document is missing" vs "we have
    # stopped assessing".
    (
        "info_requested",
        [
            "request for information",
            "request for invoice",
            "request for consult note",
            "request for completed claim form",
            "request for itemized invoice",
            "request for cf",
            "further information required",
            "information required",
            "please provide the following",
        ],
    ),
    ("suspended", ["suspended"]),
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

# The policy number (GABR-0306-DC1-00000001R) is the only thing shaped enough to
# be mistaken for a reference, and it is why bare patterns were rejected here
# originally. Deleting it before the shape fallback runs is cheaper and more
# honest than trying to write a regex that steps around it.
_POLICY_NUMBER = re.compile(r"\b[A-Za-z]{2,4}-\d{4}-[A-Za-z]{2,4}\d?-\d{6,}[A-Za-z]?\b")

# Shape fallback for letters that carry the reference with no context phrase at
# all — the vet cover note has it only in a free-form subject ("Petcover claim
# for Ari DC1-27-5628 Sr.8"), where the phrases above match nothing and are
# additionally case-sensitive. Petcover has used at least five subject shapes in
# two years; the reference's own shape has been stable across all of them.
_REFERENCE_SHAPE = re.compile(r"\b(?:[A-Za-z]{2,4}\d?-\d{2}-\d{4}|GABR-\d{4})\b", re.IGNORECASE)

# Petcover's letters — and the PDF text behind them — render the reference with
# U+2010 non-breaking hyphens: "DC1‐26‐5992". Every pattern here uses ASCII "-",
# so a raw letter yielded the reference "DC1", missed its exact (reference, Sr)
# lookup, and correlated by recency onto the wrong claim (confirmed live
# 2026-07-27: the DC1-26-5992 Sr 1 letter attached to claim #2 instead of #8).
# Normalizing once at this seam keeps stored references canonical ASCII, so they
# can still match one stored earlier; widening each character class instead would
# let a non-ASCII hyphen into petcover_reference, where nothing would match it.
_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")


def _normalize(text: str) -> str:
    return (text or "").translate(_DASHES)

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
    lowered = _normalize(text).lower()
    if any(kw in lowered for kw in IGNORE_KEYWORDS):
        return "ignore"
    for event_type, keywords in SUBJECT_KEYWORDS:
        if any(kw in lowered for kw in keywords):
            return event_type
    return None


def classify(subject: str, body: str, sender: str | None = None) -> str:
    # Ignore-check first so an "Automatic reply:" from the required-info channel
    # is still noise rather than a fabricated request.
    if any(kw in _normalize(subject).lower() for kw in IGNORE_KEYWORDS):
        return "ignore"
    if sender and INFO_REQUEST_SENDER in sender.lower():
        return "info_requested"
    return _match_keywords(subject) or _match_keywords(body) or "unclassified"


def extract_reference(text: str) -> str | None:
    """Shape first, context phrase second.

    The phrase looked like the higher-confidence signal, but it captures whatever
    token follows it, and Petcover's subjects put junk there: "Petcover claim
    for--Aari--DC1-27-5628 Serial Number: 2" yields `for--Aari--DC1-27-5628`. In a
    live trial that string was written to a claim as its reference. A shape match
    is unambiguous when it hits, so it goes first; the phrase remains the fallback
    for a future format the shape doesn't know, where a loose capture beats none."""
    text = _normalize(text)
    match = _REFERENCE_SHAPE.search(_POLICY_NUMBER.sub(" ", text))
    if match:
        return match.group(0)
    for pattern in REFERENCE_CONTEXT_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            candidate = match.group(1).rstrip(".,")
            # Still has to look like an identifier — the phrase can sit next to a
            # bare word ("Petcover claim for Ari …" captures "for").
            if "-" in candidate and any(c.isdigit() for c in candidate):
                return candidate
    return None


def extract_sr(text: str, reference: str | None) -> int | None:
    """Petcover's per-document serial within a Condition Thread, in either of
    two confirmed-live formats: "DC1-27-5628 SR1"/"Sr 3" sitting right after
    the reference (a bare 'Sr N' elsewhere carries no thread meaning and must
    not misfire), or the newer "Treatment number: N" field — its own labeled
    line, not adjacent to the reference, but unambiguous on its own (real:
    the "Claim Approval" letter has no "Sr" text at all, only this field —
    missing it silently broadened routing to the whole thread instead of one
    claim, confirmed live)."""
    if not reference:
        return None
    text = _normalize(text)
    # Separator is [\s.:]* not \s*: "DC1-27-5628 Sr.8" and "sr.1" are both live
    # (2026-07-27) and a whitespace-only separator silently missed them.
    match = re.search(re.escape(reference) + r"\s*SR[\s.:]*0*(\d+)", text, re.IGNORECASE)
    if match:
        return int(match.group(1))
    # "Serial Number: 2" is a third labeled form beside "Treatment number: N",
    # live 2026-07-19 on a letter whose reference is subject-only.
    match = re.search(r"(?:Treatment number|Serial Number):?\s*0*(\d+)", text, re.IGNORECASE)
    return int(match.group(1)) if match else None


def extract_settlement_amounts(text: str) -> dict:
    """Older PDF-attachment settlement style ($ breakdown in the PDF, not the
    email body — confirmed via dry-run). Newer 'Claim Approval' emails use a
    different template entirely; see extract_approval_amounts."""
    result = {}
    text = _normalize(text)
    claimed = re.search(r"Amount Claimed\s*\$?([\d,]+\.\d{2})", text)
    payable = re.search(r"Total Payable\s*:?\s*\$?([\d,]+\.\d{2})", text)
    if claimed:
        result["claimed_amount"] = float(claimed.group(1).replace(",", ""))
    if payable:
        result["paid_amount"] = float(payable.group(1).replace(",", ""))
    return result


# 'Claim Approval' email fields (confirmed live, Jul 2026): "Total amount
# claimed: $35.00", "Paid by us: $22.75", "Fixed excess $0.00" / "Less Fixed
# excess: $105.00", "Age Contribution: $12.25 [35%]". This is the ONLY email
# in the lifecycle that states these numbers — captured for display/
# comparison against our own expectation, never folded into it: Petcover's
# own math (Age Contribution etc.) is theirs to get right, not ours to model.
_APPROVAL_PATTERNS = {
    "claimed_amount": r"Total amount claimed:?\s*\$?([\d,]+\.\d{2})",
    "paid_amount": r"Paid by us:?\s*\$?([\d,]+\.\d{2})",
    "fixed_excess_stated": r"(?:Less\s+)?Fixed excess:?\s*\$?(-?[\d,]+\.\d{2})",
    "age_contribution_stated": r"Age Contribution:?\s*\$?([\d,]+\.\d{2})",
}


def extract_approval_amounts(text: str) -> dict:
    result = {}
    # Not broken today (these patterns match $ amounts, not references) — but the
    # text comes from the same PDFs, and one letter using an en dash in "Less
    # Fixed excess" would fail silently. Normalizing removes the class, not the case.
    text = _normalize(text)
    for key, pattern in _APPROVAL_PATTERNS.items():
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            result[key] = float(match.group(1).replace(",", ""))
    return result


_EMAIL_IN_HEADER = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def resolve_owed_by(recipients: str | None) -> dict:
    """Who owes the requested information, read from the letter's To:/Cc:.

    The sender cannot answer this — claims.au@ sends both kinds (real:
    "GABR-0305-Request for consult note" went to a vet, "GABR-0306 First Request
    for CF" went to Justin). The To: header is the only discriminator.

    Never defaults to Justin: silently reassigning a vet's obligation to him is
    the failure that loses the claim, so an unrecognized address is reported as a
    vet with its raw address rather than absorbed.
    """
    addresses = [a.lower() for a in _EMAIL_IN_HEADER.findall(recipients or "")]
    ours = {e.lower() for e in (config.OWNER_EMAIL, config.SPOUSE_EMAIL) if e}
    external = [a for a in addresses if a not in ours]
    if not external:
        return {"owed_by": "justin", "clinic": None, "clinic_email": None}
    with db.get_connection() as conn:
        contacts = {r["email"].lower(): r["merchant"] for r in conn.execute("SELECT merchant, email FROM vet_contacts")}
    for address in external:
        if address in contacts:
            return {"owed_by": "vet", "clinic": contacts[address], "clinic_email": address}
    return {"owed_by": "vet", "clinic": None, "clinic_email": external[0]}


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


def submission_group_id(claim_ids) -> str:
    """A submission's short sayable id: 'S' + its claim ids ascending — 'S6+7'.

    Derived, never stored. A stored sequence would buy a fixed-width token at the
    cost of manual live DDL (root CLAUDE.md), and this one carries the ids Justin
    already acts on (/mark 6 …) instead of adding a second vocabulary beside them.
    Sorted, so read order can't change the token.

    ponytail: derived is only stable because nothing re-splits a drafted batch —
    the only reset (invoice_matching.unmatch) clears the batch entirely. If a redo
    path ever re-groups drafted claims this has to become a real column.
    """
    return "S" + "+".join(str(i) for i in sorted(claim_ids))


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


def _already_recorded(claim_id: int | None, event_type: str, email_id: str | None) -> bool:
    """Has this exact (email, claim, event) already been logged?

    Makes re-reading Petcover mail safe, which is what lets a classifier fix be
    applied to mail already in `processed_emails` — the reason the four live
    misclassifications found 2026-07-27 would otherwise stay wrong forever.
    Without this, a re-read appends duplicate events AND re-runs the status/flag
    write, which would resurrect a settlement mismatch Justin had dismissed."""
    if email_id is None:
        return False  # manual events (confirm_resolved, dismiss) have no email
    with db.get_connection() as conn:
        return conn.execute(
            "SELECT 1 FROM claim_status_events WHERE raw_email_id IS ? AND claim_id IS ? AND event_type = ?",
            (email_id, claim_id, event_type),
        ).fetchone() is not None


def detach_reference(claim_id: int) -> dict:
    """Un-learns a wrongly-learned Petcover reference, returning the claim to the
    correlation pool so a re-read can route it correctly.

    Reference learning had no undo, and a mis-learned reference is self-sealing:
    the claim stops being an un-referenced candidate, so correlation can never
    reconsider it. Live proof — claim #2 learned `DC1` from a letter whose
    non-breaking hyphens truncated the reference, and while that value sat there
    the letter naming its real thread would have routed to a sibling claim.

    Logged, not silently wiped: the append-only log is the audit trail."""
    with db.get_connection() as conn:
        claim = conn.execute(
            "SELECT petcover_reference, petcover_sr FROM vet_claims WHERE id = ?", (claim_id,)
        ).fetchone()
        if claim is None:
            return {"ok": False, "message": f"No claim #{claim_id} found."}
        if claim["petcover_reference"] is None and claim["petcover_sr"] is None:
            return {"ok": False, "message": f"Claim #{claim_id} holds no reference to detach."}
        detached = {"reference": claim["petcover_reference"], "sr": claim["petcover_sr"]}
        conn.execute(
            "UPDATE vet_claims SET petcover_reference = NULL, petcover_sr = NULL, updated_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), claim_id),
        )
    _record_event(claim_id, "reference_detached", None, detached)
    return {
        "ok": True,
        "message": f"Claim #{claim_id}: detached reference {detached['reference']} "
        f"(Sr {detached['sr']}) — it can be re-learned from Petcover's letters.",
    }


def _record_event(claim_id: int | None, event_type: str, email_id: str | None, detail: dict) -> int:
    with db.get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO claim_status_events (claim_id, event_type, raw_email_id, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (claim_id, event_type, email_id, json.dumps(detail), datetime.now(timezone.utc).isoformat()),
        )
        return cur.lastrowid


def process_reply(
    email_id: str, subject: str, body: str, sender: str | None = None, recipients: str | None = None
) -> None:
    """Classifies one Petcover reply and routes it to the claim(s) it concerns.
    Routing precedence: (reference, Sr) → the one cited claim; reference-only →
    the thread's non-terminal claims; no stored reference → ack correlation by
    pet + condition + recency. Never guesses across Condition Threads, and never
    reopens a settled/declined claim."""
    event_type = classify(subject, body, sender)
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
    if event_type == "info_requested":
        # Which party owes the document decides Justin's next move: chase the vet,
        # or supply it himself. Nothing recorded this before — process_reply never
        # saw a header.
        detail.update(resolve_owed_by(recipients))
        requested = re.search(
            r"(?:we need a copy of|please provide the following(?: information)?(?: in order)?(?: for us)?"
            r"(?: to review the claim)?)\s*[:\-]?\s*(.+)",
            _normalize(body),
            re.IGNORECASE,
        )
        if requested:
            # First line only — the letter continues into boilerplate. Left absent
            # rather than guessed when the phrase isn't there (the vet cover note's
            # detail lives in an attachment we get no text for).
            detail["requested_document"] = requested.group(1).strip().splitlines()[0][:200]
    if event_type == "settled":
        detail.update(extract_settlement_amounts(body))
    elif event_type == "approved":
        # The approval email carries the only dollar breakdown in the whole
        # lifecycle — validate here, not at the later dollar-less 'settled' event.
        detail.update(extract_approval_amounts(body))

    if not claims:
        if not _already_recorded(None, event_type, email_id):
            _record_event(None, event_type, email_id, {**detail, "flag": "needs manual link — no claim matched"})
        return

    now = datetime.now(timezone.utc).isoformat()
    for claim in claims:
        if _already_recorded(claim["id"], event_type, email_id):
            continue  # re-read of mail already applied — nothing new to record or write
        # Validate whenever a paid amount is actually stated — the newer
        # 'approved' email carries it; the older settled-with-PDF style
        # carries it directly in the settled email itself. Either way, the
        # dollar-less 'payment processed' settled email that FOLLOWS an
        # approval has nothing to validate and correctly no-ops here.
        settlement_flag = _validate_settlement(claim, detail.get("paid_amount"), claim["_txn_date"])
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


def _policy_year_start(anniversary_mmdd: str, on: date) -> date:
    """Start of the policy year (anniversary→anniversary) containing date `on`."""
    mm, dd = (int(x) for x in anniversary_mmdd.split("-"))
    this_year = date(on.year, mm, dd)
    return this_year if on >= this_year else date(on.year - 1, mm, dd)


def _validate_settlement(claim, paid_amount: float | None, txn_date_iso: str) -> str | None:
    """Deterministic settlement check (ADR-0011, simplified per Justin): expected
    = claimable − $150 excess, once per condition thread per policy year — the
    policy year a CLAIM belongs to is judged by its own transaction date, never
    by when the reply happened to arrive (two claims processed the same week
    can be a year apart by transaction date, straddling the anniversary).

    Closed-year default: our claim history for any policy year that has already
    ended is presumed incomplete (some vet spend never hits the tracked card,
    and bank-CSV coverage doesn't reach arbitrarily far back) — so excess/cap
    math only runs for the CURRENT, still-open policy year. A claim whose own
    transaction falls in an already-closed year is assumed to have already
    passed the threshold: expected = full claimable, no excess deducted.

    The flag is deliberately a plain mismatch check in EITHER direction — we
    don't try to replicate Petcover's own internal math (e.g. their Age
    Contribution co-pay), we just compare our simple expectation to what they
    actually reported and surface any difference as a warning for Justin."""
    if paid_amount is None:
        return None
    invoice = json.loads(claim["invoice_data"]) if claim["invoice_data"] else {}
    claimable = invoice.get("claimable_amount")
    if claimable is None:
        claimable = invoice.get("amount")
    if claimable is None:
        return None  # nothing to compare against — don't fabricate an expectation
    claimable = float(claimable)

    with db.get_connection() as conn:
        pet = conn.execute("SELECT policy_anniversary FROM pets WHERE id = ?", (claim["pet_id"],)).fetchone()
    anniversary = pet["policy_anniversary"] if pet else None
    txn_date = date.fromisoformat(txn_date_iso[:10])

    reason = ""
    if not anniversary:
        # No anniversary on record: can't tell current vs closed year at all —
        # don't guess a boundary, just expect full claimable.
        expected = claimable
        note = "; policy anniversary unknown, expected full claimable"
    else:
        claim_year_start = _policy_year_start(anniversary, txn_date)
        current_year_start = _policy_year_start(anniversary, datetime.now(timezone.utc).date())
        if claim_year_start != current_year_start:
            expected = claimable
            note = ""
        else:
            year_end = claim_year_start.replace(year=claim_year_start.year + 1)
            reference = claim["petcover_reference"]
            with db.get_connection() as conn:
                thread_prior = conn.execute(
                    "SELECT bt.date AS txn_date FROM claim_status_events e "
                    "JOIN vet_claims v ON v.id = e.claim_id "
                    "JOIN bank_transactions bt ON bt.id = v.transaction_id "
                    "WHERE v.petcover_reference IS ? AND e.event_type IN ('approved', 'settled') "
                    "AND e.claim_id != ?",
                    (reference, claim["id"]),
                ).fetchall() if reference else []
                pet_paid = conn.execute(
                    "SELECT bt.date AS txn_date, e.detail FROM claim_status_events e "
                    "JOIN vet_claims v ON v.id = e.claim_id "
                    "JOIN bank_transactions bt ON bt.id = v.transaction_id "
                    "WHERE v.pet_id IS ? AND e.event_type = 'approved' AND e.claim_id != ?",
                    (claim["pet_id"], claim["id"]),
                ).fetchall()

            def _in_year(iso: str) -> bool:
                return claim_year_start <= date.fromisoformat(iso) < year_end

            excess_consumed = any(_in_year(r["txn_date"]) for r in thread_prior)
            paid_this_year = sum(
                (json.loads(r["detail"] or "{}").get("paid_amount") or 0.0)
                for r in pet_paid
                if _in_year(r["txn_date"])
            )
            remaining_cap = max(0.0, ANNUAL_CAP - paid_this_year)
            expected = claimable - (0.0 if excess_consumed else POLICY_EXCESS)
            expected = max(0.0, min(expected, remaining_cap))
            note = ""
            reason = " (excess already used this policy year)" if excess_consumed else " (fresh $150 excess this policy year)"

    if abs(paid_amount - expected) > SETTLEMENT_TOLERANCE:
        return f"settlement mismatch — we expected ${expected:.2f}, Petcover paid ${paid_amount:.2f}{reason}{note} — review"
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
            # The first tap on a batch advances every member, so any second tap —
            # a sibling card, an older push, the dashboard's per-claim button —
            # necessarily lands here. "isn't drafted (status: sent)" read as a
            # failure when the send had in fact succeeded. Only 'sent' gets this
            # wording: a claim at acknowledged/settled moved on through Petcover's
            # replies and must report where it actually is.
            if claim["status"] == "sent":
                group = _sent_group_ids(conn, claim_id, claim["draft_id"])
                return {
                    "ok": False,
                    "message": f"Submission {submission_group_id(group)} ({_id_list(group)})"
                    " was already marked sent — nothing to do.",
                }
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
        group = _sent_group_ids(conn, claim_id, claim["draft_id"])
    # Group id supplements the claim ids, never replaces them — Justin's commands
    # take ids and a regression test enforces their presence in every message.
    suffix = f", {count} claims in this submission" if count > 1 else ""
    return {
        "ok": True,
        "message": f"Submission {submission_group_id(group)} ({_id_list(group)}{suffix})"
        " marked sent — Petcover replies now tracked.",
    }


def _id_list(claim_ids) -> str:
    return ", ".join(f"#{i}" for i in sorted(claim_ids))


def _sent_group_ids(conn, claim_id: int, draft_id: str | None) -> list[int]:
    """Member ids of the submission this claim's send covers. Restricted to 'sent'
    so a member Petcover has already answered isn't relabelled as just-sent."""
    if not draft_id:
        return [claim_id]
    rows = conn.execute(
        "SELECT id FROM vet_claims WHERE draft_id = ? AND status = 'sent'", (draft_id,)
    ).fetchall()
    return [r["id"] for r in rows] or [claim_id]


def confirm_resolved(claim_id: int) -> dict:
    """Clears a needs-action flag (info_requested/suspended) by Justin's explicit
    confirmation — ADR-0008.

    Idempotent on purpose: an update can be replayed after a crash, and two
    confirmations of the same request would otherwise write two audit events for
    one decision. Nothing outstanding means nothing to confirm."""
    with db.get_connection() as conn:
        events = conn.execute(
            "SELECT event_type FROM claim_status_events WHERE claim_id = ? ORDER BY id",
            (claim_id,),
        ).fetchall()
    types = [e["event_type"] for e in events]
    last_flag = max((i for i, t in enumerate(types) if t in ("info_requested", "suspended")), default=None)
    if last_flag is None or "confirmed_resolved" in types[last_flag + 1 :]:
        return {"ok": False, "message": f"Claim #{claim_id} has nothing outstanding to confirm."}
    _record_event(claim_id, "confirmed_resolved", None, {})
    return {"ok": True, "message": f"Claim #{claim_id} confirmed resolved."}


def _policy_year_key(txn_date: str, anniversary: str | None) -> str:
    """Policy-year key for excess/cap grouping. Anniversary-anchored (ADR-0011)
    when the pet's renewal date is on file; calendar year otherwise — an
    approximation that can group a claim a year off near the real boundary."""
    if not txn_date:
        return ""
    if anniversary:
        return _policy_year_start(anniversary, date.fromisoformat(txn_date[:10])).isoformat()
    return txn_date[:4]


def _split_conditions(row) -> dict[str, float] | None:
    """Recovers a split claim's real per-condition subtotals from its stored
    item_conditions JSON (same grouping claim_forms._group_by_condition did
    when the claim was split into e.g. "Arthritis; Dermatitis"). None when
    there's nothing to split (no item_conditions, or it doesn't parse) — the
    row's single joined condition_text bucket applies unchanged, same as
    today, rather than guessing a breakdown."""
    raw = row.get("item_conditions")
    if not raw:
        return None
    try:
        groups = claim_forms._group_by_condition(json.loads(raw))
    except (ValueError, TypeError, AttributeError):
        return None
    return groups or None


def _apply_excess_and_cap(rows: list, excess, cap, anniversary: str | None = None) -> None:
    """Fills each row's `expected` in place. Excess is drained greedily across
    a (condition, year) group in charge-date order — earliest charges absorb
    it first — then the running per-year total is bounded by the cap. A claim
    spanning >1 condition (item_conditions on file) is split back into its
    real per-condition subtotals for this grouping — Petcover's $150 excess
    applies per condition, so a joined "Arthritis; Dermatitis" claim must
    drain two buckets, not share one; the claim still ends up with a single
    `expected.value` (the sum of its condition-portions). All figures are
    estimates (est.), never booked reimbursements: they don't net off what
    Petcover has already paid this year. Missing excess/cap → unavailable,
    never guessed."""
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

    # Each row contributes one "part" per condition it actually covers — one
    # part for the common single-condition case, one per condition for a
    # split claim (a single row can land in >1 bucket below).
    parts = []
    for r in priced:
        split = _split_conditions(r)
        if split:
            for condition, amount in split.items():
                parts.append({"row": r, "condition": condition, "amount": amount})
        else:
            parts.append({"row": r, "condition": r["condition_text"] or "", "amount": r["claimable"] or 0})

    by_condition: dict[tuple, list] = {}
    for p in parts:
        key = (p["condition"], _policy_year_key(p["row"]["txn_date"], anniversary))
        by_condition.setdefault(key, []).append(p)

    year_totals: dict[str, float] = {}
    row_values: dict[int, float] = {}
    row_notes: dict[int, list] = {}
    for (condition, year), group in by_condition.items():
        remaining_excess = excess
        group_claimable = sum(g["amount"] for g in group)
        for p in sorted(group, key=lambda g: g["row"]["txn_date"] or ""):
            amount = p["amount"]
            absorbed = min(remaining_excess, amount)
            remaining_excess -= absorbed
            after_excess = amount - absorbed
            # bound the running per-year total by the annual cap
            used = year_totals.get(year, 0.0)
            allowed = max(0.0, cap - used)
            value = round(min(after_excess, allowed), 2)
            year_totals[year] = used + value
            if group_claimable < excess:
                note = f"{condition or 'condition'} YTD ${group_claimable:.2f} < ${excess:.0f} excess"
            else:
                note = f"est. after ${excess:.0f} excess"
            row_id = id(p["row"])
            row_values[row_id] = row_values.get(row_id, 0.0) + value
            row_notes.setdefault(row_id, []).append(note)

    for r in priced:
        row_id = id(r)
        r["expected"] = {
            "available": True,
            "value": round(row_values.get(row_id, 0.0), 2),
            "note": "; ".join(row_notes.get(row_id, [])),
            "estimate": True,
        }


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
            "pets.policy_anniversary, "
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
                "item_conditions": c["item_conditions"],
                "status": c["status"],
                "reference": c["petcover_reference"],
                "draft_id": c["draft_id"],
                "flag": c["flag"],
                "claimable": claimable,
                "txn_date": c["txn_date"],
                "annual_excess": c["annual_excess"],
                "annual_cap": c["annual_cap"],
                "policy_anniversary": c["policy_anniversary"],
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
        _apply_excess_and_cap(pet_claims, first["annual_excess"], first["annual_cap"], first["policy_anniversary"])
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


def history_rows(days: int = 365) -> list[dict]:
    """Flat, one-row-per-claim view of visit_ledger() for Telegram's /history.
    A split charge's claims already appear as separate entries in the nested
    ledger — this just flattens them and windows by transaction date (a charge
    with no claim yet contributes no rows, same as it contributes nothing to
    render in the web ledger).

    OLDEST first, deliberately inverting visit_ledger's newest-first order: a
    visit stops being claimable once it's a year old, so the rows nearest the
    `days` cutoff are the ones about to expire — they belong at the top of
    page 1, not buried on the last page."""
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
    rows = []
    for entry in reversed(visit_ledger()):
        txn = entry["txn"]
        if txn["date"] < cutoff:
            continue
        for claim in entry["claims"]:
            rows.append(
                {
                    "date": txn["date"],
                    "merchant": txn["merchant"],
                    "amount": txn["amount"],
                    "status": claim["status"],
                    "pet_name": claim["pet_name"],
                    "condition_text": claim["condition_text"],
                    # paid = what Petcover actually paid (settled only, a hard
                    # fact); expected = our excess/cap estimate for the rest.
                    "paid": claim["settled_paid"],
                    "expected": claim["expected"],
                }
            )
    return rows


# Action kinds in resolution priority, most-blocking first. A claim yields ONE
# action (its first match here) — several predicates overlap in real data, e.g.
# an awaiting-invoice claim usually has no pet yet either, and two cards for one
# claim would just be noise.
ACTION_PRIORITY = (
    "split_proposal",
    "unmatch",
    "confirm_resolved",
    "mark_sent",
    "invoice_request_sent",
    # pet before condition, matching the order process_claim actually blocks in
    # (claim_forms flags "pet not identified" before it looks at the condition)
    "assign_pet",
    "set_condition",
    "dismiss_mismatch",
    "blocked_insurer",
)

# Kinds where the action belongs to the whole submission, not one claim: sending
# one Gmail draft sends every claim in it, so N cards for one email is N-1 taps
# too many — and taps 2..N land on an already-sent claim, which read as failures.
#
# Stated explicitly rather than inferred from "has a draft_id" so the reasoning
# stays reviewable: every other kind fires at or before `matched`, where no draft
# and so no batch exists (pipeline.py:236 says the same), except confirm_resolved
# and dismiss_mismatch, which hang off per-claim status events — collapsing those
# would hide one claim's settlement mismatch behind its batch.
SUBMISSION_LEVEL_ACTIONS = ("mark_sent",)

# Nothing left for Justin to do. settled/declined end a Condition Thread;
# below_excess (nothing payable) and absorbed (merged into a sibling claim) are
# equally finished but don't, so they aren't in TERMINAL_STATUSES.
CLOSED_STATUSES = TERMINAL_STATUSES + ("below_excess", "absorbed")

# What each kind means to Justin, and what stalls until he acts.
_ACTION_META = {
    "split_proposal": ("Confirm invoice split", "one invoice paid over several charges"),
    "unmatch": ("Check invoice match", "a possible wrong/extra invoice is attached"),
    "confirm_resolved": ("Confirm resolved", "Petcover is waiting on you"),
    "mark_sent": ("Send Gmail draft", "Petcover reply tracking hasn't started"),
    "invoice_request_sent": ("Invoice request sent?", "no invoice means no claim"),
    "set_condition": ("Set condition", "the claim can't be drafted"),
    "assign_pet": ("Assign pet", "the claim can't be filled"),
    "dismiss_mismatch": ("Review settlement", "a paid-vs-expected difference is unreviewed"),
    "blocked_insurer": ("Define claim process", "every claim for this pet is stuck"),
}

_INSURER_UNDEFINED = "claim process not yet defined"


def _action_kind(claim: dict, open_split_claim_ids: set, unresolved_event_claim_ids: set) -> str | None:
    """The single action a claim needs, or None when it's waiting on someone
    else (sent/acknowledged/approved = Petcover's turn) or finished."""
    flag = claim["flag"] or ""
    if claim["id"] in open_split_claim_ids:
        return "split_proposal"
    if flag.startswith("possible additional invoice"):
        return "unmatch"
    if claim["id"] in unresolved_event_claim_ids:
        return "confirm_resolved"
    if claim["status"] == "drafted":
        return "mark_sent"
    # flag, NOT invoice_request_sent_at + draft_id: draft_id is overloaded
    # (claim drafts AND invoice-request drafts), so that pair matches almost
    # every claim and is useless as a predicate.
    if flag == "invoice_request_drafted":
        return "invoice_request_sent"
    if flag.endswith(_INSURER_UNDEFINED):
        return "blocked_insurer"
    if flag.startswith("settlement mismatch"):
        return "dismiss_mismatch"
    if claim["status"] in CLOSED_STATUSES:
        return None  # finished — an absorbed/below-excess claim needs nothing
    if claim["pet_id"] is None:
        return "assign_pet"
    if claim["status"] == "matched" and not claim["condition_text"]:
        return "set_condition"
    return None


def pending_actions() -> list[dict]:
    """Everything waiting on Justin, one entry per claim, oldest charge first.

    Oldest-first for the same reason /history is: a visit stops being claimable
    once it's a year old, so the ones nearest expiry are the urgent ones.

    Nothing else in the codebase answers this. dashboard_lists covers only the
    event-driven slice (one of nine kinds here), and pipeline.notify_claim_states
    is a change-feed — it dedupes on (status, flag) and so goes silent on a
    claim that stays outstanding, which is how two drafted claims sat unsent for
    three days without a single reminder."""
    with db.get_connection() as conn:
        open_splits = conn.execute("SELECT claim_ids FROM split_proposals WHERE status = 'open'").fetchall()
    open_split_claim_ids = {cid for row in open_splits for cid in json.loads(row["claim_ids"] or "[]")}
    unresolved_event_claim_ids = {entry["claim"]["id"] for entry in dashboard_lists()["needs_action"]}

    today = datetime.now(timezone.utc).date()
    actions = []
    for entry in visit_ledger():
        txn = entry["txn"]
        for claim in entry["claims"]:
            kind = _action_kind(claim, open_split_claim_ids, unresolved_event_claim_ids)
            if kind is None:
                continue
            title, blocks = _ACTION_META[kind]
            actions.append(
                {
                    "kind": kind,
                    "title": title,
                    "blocks": blocks,
                    "claim_id": claim["id"],
                    "claim_ids": [claim["id"]],
                    "group_id": submission_group_id([claim["id"]]),
                    "draft_id": claim["draft_id"],
                    "pet_name": claim["pet_name"],
                    "pet_id": claim["pet_id"],
                    "merchant": txn["merchant"],
                    "amount": txn["amount"],
                    "date": txn["date"],
                    "status": claim["status"],
                    "condition_text": claim["condition_text"],
                    "flag": claim["flag"],
                    "detail": claim["flag"] or "",
                    "age_days": (today - date.fromisoformat(txn["date"][:10])).days,
                    # blocked_insurer needs a decision from Justin, not a tap —
                    # there is no UI that can clear it.
                    "actionable": kind != "blocked_insurer",
                }
            )
    actions = _collapse_submissions(actions)
    actions.sort(key=lambda a: (a["date"], ACTION_PRIORITY.index(a["kind"])))
    return actions


def _collapse_submissions(actions: list[dict]) -> list[dict]:
    """Fold SUBMISSION_LEVEL_ACTIONS down to one entry per submission.

    The collapsed entry keeps a single `claim_id` (the lowest member's, the same
    convention as agent._single_target) so every existing consumer keeps working
    untouched: the callback token is `sent:{claim_id}` and mark_sent(any member)
    already advances the whole group via `WHERE draft_id = ?`."""
    out, groups = [], {}
    for action in actions:
        if action["kind"] in SUBMISSION_LEVEL_ACTIONS and action["draft_id"]:
            groups.setdefault(action["draft_id"], []).append(action)
        else:
            out.append(action)
    for members in groups.values():
        members.sort(key=lambda a: a["claim_id"])
        if len(members) == 1:
            out.append(members[0])
            continue
        oldest = min(members, key=lambda a: a["date"])
        out.append(
            {
                **members[0],
                "claim_ids": [a["claim_id"] for a in members],
                "group_id": submission_group_id(a["claim_id"] for a in members),
                # One email covering several charges: the total is what Justin is
                # confirming. Urgency comes from the OLDEST member — a visit stops
                # being claimable at a year, so the batch expires with its eldest.
                "amount": sum(a["amount"] for a in members),
                "date": oldest["date"],
                "age_days": oldest["age_days"],
                # Members differ in date, amount and condition (live: #6 Raised ALT
                # $351.50, #7 Arthritis $132.50), so one summary line would hide
                # what's in the email.
                "members": [
                    {k: a[k] for k in ("claim_id", "merchant", "amount", "date", "condition_text")}
                    for a in members
                ],
            }
        )
    return out


# Sent to Petcover (or ready to be) and no final answer yet. Broader than
# AWAITING_REPLY_STATUSES, which is the ack-correlation pool: this one also
# carries 'drafted' (Justin hasn't pressed send) and 'approved' (their figures
# arrived, the payment confirmation hasn't).
_OPEN_SUBMISSION_STATUSES = ("drafted",) + AWAITING_REPLY_STATUSES + ("approved",)


def submissions_awaiting_reply() -> list[dict]:
    """What has gone to Petcover and whether an answer came back — one entry per
    Submission (claims sharing a draft_id move together), newest activity last.

    Nothing else answers this. reconcile_sent_invoice_requests covers only
    invoice-request drafts to vets; dashboard_lists covers only the event slice.

    Caveat worth knowing: there is no sent-at column, so "waiting" is measured
    from vet_claims.updated_at — which mark_sent stamps and nothing else touches
    while a submission sits unanswered. Once a reply lands, the last event is
    reported instead, so the imprecision never applies to a case that matters."""
    today = datetime.now(timezone.utc).date()
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT vc.id, vc.status, vc.draft_id, vc.petcover_reference, vc.updated_at, "
            "       p.name AS pet_name, bt.merchant, bt.amount AS txn_amount, bt.date AS txn_date "
            "FROM vet_claims vc "
            "LEFT JOIN pets p ON p.id = vc.pet_id "
            "LEFT JOIN bank_transactions bt ON bt.id = vc.transaction_id "
            f"WHERE vc.status IN ({','.join('?' * len(_OPEN_SUBMISSION_STATUSES))}) "
            "ORDER BY vc.id",
            _OPEN_SUBMISSION_STATUSES,
        ).fetchall()
        events = conn.execute(
            "SELECT claim_id, event_type, created_at FROM claim_status_events "
            "WHERE claim_id IS NOT NULL ORDER BY id"
        ).fetchall()

    latest_event = {e["claim_id"]: e for e in events}  # ordered by id, so last wins

    groups: dict[str, list] = {}
    for row in rows:
        # A claim with no draft_id is its own submission — grouping them all
        # under one None key would merge unrelated claims into one entry.
        groups.setdefault(row["draft_id"] or f"claim-{row['id']}", []).append(row)

    out = []
    for key, claims in groups.items():
        newest = max((latest_event[c["id"]] for c in claims if c["id"] in latest_event),
                     key=lambda e: e["created_at"], default=None)
        activity = newest["created_at"] if newest else max(c["updated_at"] for c in claims)
        out.append(
            {
                "claim_ids": [c["id"] for c in claims],
                "draft_id": key if not key.startswith("claim-") else None,
                "status": claims[0]["status"],
                "pet_name": claims[0]["pet_name"],
                "reference": claims[0]["petcover_reference"],
                "merchants": sorted({c["merchant"] for c in claims if c["merchant"]}),
                "total_amount": sum(abs(c["txn_amount"] or 0) for c in claims),
                "last_event": newest["event_type"] if newest else None,
                "last_activity": activity,
                "days_waiting": (today - date.fromisoformat(activity[:10])).days,
            }
        )
    out.sort(key=lambda s: s["last_activity"])
    return out


def claim_detail(claim_id: int) -> dict | None:
    """Everything about one claim, for answering "why is #21 like this?" — the
    transaction, invoice line items, claimable subtotal, current flag, and every
    status event WITH the dollar figures recorded on it.

    claim_history gives event types and subjects only and is keyed by
    pet/reference, so it cannot answer a question about a specific claim id."""
    with db.get_connection() as conn:
        claim = conn.execute(
            "SELECT vc.*, p.name AS pet_name, p.policy_anniversary, "
            "       bt.date AS txn_date, bt.amount AS txn_amount, bt.merchant "
            "FROM vet_claims vc "
            "LEFT JOIN pets p ON p.id = vc.pet_id "
            "LEFT JOIN bank_transactions bt ON bt.id = vc.transaction_id "
            "WHERE vc.id = ?",
            (claim_id,),
        ).fetchone()
        if claim is None:
            return None
        events = conn.execute(
            "SELECT event_type, created_at, detail FROM claim_status_events "
            "WHERE claim_id = ? ORDER BY id",
            (claim_id,),
        ).fetchall()

    invoice = json.loads(claim["invoice_data"]) if claim["invoice_data"] else {}
    # Only the figures — the raw detail also holds subjects and bodies, which
    # would blow the chat turn's token budget for no answering power.
    figure_keys = ("claimed_amount", "paid_amount", "fixed_excess_stated",
                   "age_contribution_stated", "subject")
    return {
        "claim_id": claim["id"],
        "status": claim["status"],
        "flag": claim["flag"],
        "pet_name": claim["pet_name"],
        "condition_text": claim["condition_text"],
        "reference": claim["petcover_reference"],
        "petcover_sr": claim["petcover_sr"],
        "txn_date": claim["txn_date"],
        "txn_amount": claim["txn_amount"],
        "merchant": claim["merchant"],
        "invoice_number": invoice.get("invoice_number"),
        "invoice_amount": invoice.get("amount"),
        "claimable_amount": invoice.get("claimable_amount", invoice.get("amount")),
        "items": invoice.get("items") or [],
        "events": [
            {
                "event_type": e["event_type"],
                "at": e["created_at"],
                **{k: v for k, v in (json.loads(e["detail"] or "{}")).items() if k in figure_keys},
            }
            for e in events
        ],
    }


def dismiss_mismatch(claim_id: int) -> dict:
    """Clears a settlement-mismatch flag once Justin has looked at it. Records a
    `mismatch_dismissed` event rather than just wiping the flag — the append-only
    log (ADR-0008) is the audit trail, and a silently-erased discrepancy is
    exactly the invisible failure the hard rules forbid."""
    with db.get_connection() as conn:
        claim = conn.execute("SELECT flag FROM vet_claims WHERE id = ?", (claim_id,)).fetchone()
        if claim is None:
            return {"ok": False, "message": f"No claim #{claim_id} found."}
        if not (claim["flag"] or "").startswith("settlement mismatch"):
            return {"ok": False, "message": f"Claim #{claim_id} has no settlement mismatch to review."}
        dismissed = claim["flag"]
        conn.execute(
            "UPDATE vet_claims SET flag = NULL, updated_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), claim_id),
        )
    _record_event(claim_id, "mismatch_dismissed", None, {"dismissed_flag": dismissed})
    return {"ok": True, "message": f"Claim #{claim_id}: settlement difference marked reviewed."}


def mark_invoice_request_sent(claim_id: int) -> dict:
    """Justin sent the invoice-request draft himself (the app never sends —
    hard rule), which opens the reply search window. Shared by the dashboard
    route and the Telegram button so both can't drift."""
    now = datetime.now(timezone.utc).isoformat()
    with db.get_connection() as conn:
        claim = conn.execute("SELECT 1 FROM vet_claims WHERE id = ?", (claim_id,)).fetchone()
        if claim is None:
            return {"ok": False, "message": f"No claim #{claim_id} found."}
        conn.execute(
            "UPDATE vet_claims SET invoice_request_sent_at = ?, flag = NULL, updated_at = ? WHERE id = ?",
            (now, now, claim_id),
        )
    return {"ok": True, "message": f"Claim #{claim_id}: invoice request marked sent — watching for the reply."}


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
