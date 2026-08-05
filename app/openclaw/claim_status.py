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

# --- The state machine -------------------------------------------------------
# Twelve states and, until now, nothing written down about which may move to
# which: `status` was a mutable column written by seven statements across three
# modules, six of which appended no event. Two live incidents are what these
# three tables exist to make impossible — 2026-07-27, when re-reading already
# ingested mail moved four claims backwards, and 2026-07-28, when repairing six
# mistyped events meant inferring one claim's prior state from an absence ("it
# holds no acknowledged event, so it was sent"). ADR-0008 established that every
# state change is an event; this establishes which of them are legal.

# Event type -> the state it moves the claim to. Data, not an `if`: `matched` and
# `unmatched` name states that differ from their own event type, and everything
# `process_reply` classifies happens to name its own.
STATE_EVENTS = {
    "matched": "matched",
    "unmatched": "pending_match",
    "drafted": "drafted",
    "sent": "sent",
    "absorbed": "absorbed",
    "acknowledged": "acknowledged",
    "info_requested": "info_requested",
    "suspended": "suspended",
    "approved": "approved",
    "settled": "settled",
    "declined": "declined",
    "below_excess": "below_excess",
}

# Recorded, never move state. `unclassified` is the one that used to be a special
# case inside process_reply's UPDATE — it is a review-queue entry, and writing it
# to status would regress an acknowledged claim. Making that a property of the
# event type rather than one writer's `if` is this whole change in miniature.
STATELESS_EVENTS = frozenset(
    {
        "unclassified",
        "confirmed_resolved",
        "mismatch_dismissed",
        "reference_detached",
    }
)

# The third category, and it exists because `design.md` contradicts itself: Decision
# 1 lists `state_backfilled` as stateless, Decision 8 has it carry the claim's
# current status. Stateless wins in the fold, so the backfill would have changed
# nothing — all nineteen claims would still project `pending_match`, and Phase 2
# giving the projection authority would then have reset every one of them. That is
# the 2026-07-27 regression again, nineteen claims instead of four.
#
# So it is state-bearing, but its target is per-claim (in `detail["status"]`) rather
# than fixed like every `STATE_EVENTS` entry, and it is a SEED rather than a
# transition: it asserts a state the log has no path to, which is the whole reason
# it is being written. It is therefore exempt from `TRANSITIONS` by construction —
# the one exemption in the machine, and the only place a state is asserted rather
# than transitioned to.
BACKFILL_EVENT = "state_backfilled"

# Every legal move. Derived from what the seven existing writers actually do, so
# its first version describes current behaviour rather than an aspiration — which
# is why two entries look odd: `matched`->`matched` and `drafted`->`matched` both
# exist because `split_between_pets` resets a draft, and `below_excess` is
# non-terminal by decision (the invoice is retained, so Petcover can still
# acknowledge, approve or settle it). `None` is a brand-new claim's from-state.
# Any pair absent here is refused and flagged, never applied silently.
TRANSITIONS: dict[str | None, frozenset] = {
    None: frozenset({"pending_match"}),
    "pending_match": frozenset({"matched", "absorbed"}),
    "matched": frozenset({"drafted", "pending_match", "matched", "absorbed"}),
    "drafted": frozenset({"sent", "matched", "pending_match"}),
    "sent": frozenset(
        {
            "acknowledged",
            "info_requested",
            "suspended",
            "approved",
            "settled",
            "declined",
            "below_excess",
        }
    ),
    "acknowledged": frozenset(
        {"info_requested", "suspended", "approved", "settled", "declined", "below_excess"}
    ),
    "info_requested": frozenset(
        {"suspended", "acknowledged", "approved", "settled", "declined", "below_excess"}
    ),
    "suspended": frozenset({"info_requested", "approved", "settled", "declined", "below_excess"}),
    "approved": frozenset({"settled"}),
    "below_excess": frozenset({"sent", "acknowledged", "approved", "settled", "declined"}),
    # Terminal (ADR-0011): a later letter reusing the thread's reference must
    # never reopen them. Leaving one is reverting the event that closed it.
    "settled": frozenset(),
    "declined": frozenset(),
    "absorbed": frozenset({"pending_match"}),
}

# Policy math (ADR-0011). Per-condition-thread excess and per-pet annual cap,
# both reset on the pet's policy anniversary. $2 tolerance absorbs rounding.
POLICY_EXCESS = 150.00
ANNUAL_CAP = 10000.00
SETTLEMENT_TOLERANCE = 2.00


def claimable_subtotal(invoice_data) -> tuple[float | None, bool]:
    """A claim's claimable subtotal, and whether it is recorded at all.

    The single reader of `invoice_data.claimable_amount` — the line-item
    subtotal minus NON_CLAIMABLE_KEYWORDS, and this claim's share of a per-pet
    split (ADR-0007, ADR-0019). Deliberately NO fallback to `invoice_data.amount`:
    the invoice total is a different quantity, and substituting it is what
    produced claim #2's "we expected $430.74" out of $580.74 − $150.00, a number
    never submitted to Petcover as claimable.

    Returns (value, recorded) rather than a bare None because a stored 0.0 is a
    real answer (live: claim #20, invoice total $152.50, claimable $0.00) and a
    None conflates it with the five claims that have no key at all.

    Accepts the raw `invoice_data` column (JSON string or None) or an
    already-parsed dict, so every caller can use it without re-parsing.
    """
    if invoice_data is None:
        invoice = {}
    elif isinstance(invoice_data, (str, bytes)):
        invoice = json.loads(invoice_data) if invoice_data else {}
    else:
        invoice = invoice_data
    value = invoice.get("claimable_amount")
    if value is None:
        return None, False
    return float(value), True


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
    # "Total amount claimed:" on an approval; the under-excess refusal drops the
    # "Total" and was extracting nothing at all, which is why the letter that
    # states $55.74 had no figure to route by and fell through to the ordering
    # heuristic instead.
    "claimed_amount": r"(?:Total a|A)mount claimed:?\s*\$?([\d,]+\.\d{2})",
    "paid_amount": r"Paid by us:?\s*\$?([\d,]+\.\d{2})",
    "fixed_excess_stated": r"(?:Less\s+)?Fixed excess:?\s*\$?(-?[\d,]+\.\d{2})",
    "age_contribution_stated": r"Age Contribution:?\s*\$?([\d,]+\.\d{2})",
    # The letter's own deduction lines, both missing until 2026-08-04 while the
    # settlement check computed an expectation that used neither. Non-claimable
    # is written with U+2010 (`Non‑claimable amount $0.00`) — `_normalize` maps
    # it, which is the whole reason that normalization exists.
    "non_claimable_stated": r"Non-?claimable amount:?\s*\$?(-?[\d,]+\.\d{2})",
    # Fifth deduction, $0.00 [0%] on all nine live letters. Captured because an
    # uncaptured term is one that breaks the arithmetic silently the first time
    # Petcover uses it — the exact failure Age Contribution just caused.
    "percentage_excess_stated": r"Percentage Excess:?\s*\$?(-?[\d,]+\.\d{2})",
}

# The rate, not the dollars: `Age Contribution: $12.25 [35%]`. Absent bracket
# means absent key — never a default, because defaulting 0.35 onto a letter that
# states no percentage is exactly the modelling of Petcover's policy that
# design.md's Decision 2 rejects.
_AGE_CONTRIBUTION_PERCENT = re.compile(
    r"Age Contribution:?\s*\$?[\d,]+\.\d{2}\s*\[\s*(\d+(?:\.\d+)?)\s*%", re.IGNORECASE
)


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
    percent = _AGE_CONTRIBUTION_PERCENT.search(text)
    if percent:
        result["age_contribution_percent"] = float(percent.group(1)) / 100
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
        contacts = {
            r["email"].lower(): r["merchant"]
            for r in conn.execute("SELECT merchant, email FROM vet_contacts")
        }
    for address in external:
        if address in contacts:
            return {"owed_by": "vet", "clinic": contacts[address], "clinic_email": address}
    return {"owed_by": "vet", "clinic": None, "clinic_email": external[0]}


# What Petcover asked for, and the treatment date it names. Both come out of the
# letter's own template phrasing, so this is a regex, not an LLM call.
#
# The capture has to stop somewhere: the requested item sits on its own line(s)
# between the ask and the standard boilerplate, so the boilerplate openers are
# the terminator. Real letter (2026-07-27):
#
#   ... To assess your claim, we need a copy of
#   Consultation notes dated 18/05/2026
#   Please note we cannot process the claim without the information requested.
_BOILERPLATE = r"please note|you can reach us|in line with|kind regards|thank you"
# The filler between the ask and the item has to be consumed, or it becomes the
# "document". Real live phrasings, both of them vet cover notes:
#   "please provide the following information in order for us to review the"  (ends there — detail is in an attachment)
#   "please provide the following for us to review the claim Consult notes dated ..."
# Dropping this consumption (a 2026-07-28 refactor did) yields
# 'information in order for us to review the' as the document Justin chases.
_ASK_FILLER = (
    r"(?:\s*information)?(?:\s*in order)?(?:\s*for us)?(?:\s*to review(?:\s*the(?:\s*claim)?)?)?"
)
_DOCUMENT_ASK = re.compile(
    rf"(?:we (?:need|require) a copy of|please provide the following){_ASK_FILLER}\s*[:\-]?\s*(.+?)"
    rf"(?=\s*(?:{_BOILERPLATE})|\Z)",
    re.IGNORECASE | re.DOTALL,
)
# The lookahead cannot fire when the ask's own trailing `\s*` has already eaten
# the blank line the boilerplate sits behind: an ask with nothing after it then
# captured "Please note we cannot process…" and would have shown that to Justin
# as the requested document. Checked per line as well, which is where it is
# unambiguous.
_BOILERPLATE_LINE = re.compile(rf"^(?:{_BOILERPLATE})", re.IGNORECASE)
_DOCUMENT_DATE = re.compile(r"\bdated\s+(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})\b", re.IGNORECASE)
_DOCUMENT_DATE_WORDS = re.compile(
    r"\bdated\s+(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})\b", re.IGNORECASE
)
_MONTHS = {
    m: i
    for i, name in enumerate(
        [
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ],
        start=1,
    )
    for m in (name, name[:3])
}


def extract_requested_document(text: str) -> str | None:
    """The document Petcover asked for, verbatim from its own letter.

    "More vet info required" cannot be acted on; "consult notes dated 18/05/2026"
    can — a clinic asked for a named document on a named date answers in one
    look. Returns None when no recognized phrase is present: a null costs nothing
    (the label falls back to who-owes-it wording), while a wrong document sends
    Justin chasing paperwork nobody asked for.

    An earlier cut took `splitlines()[0]`, which is right for the one live letter
    (the item sits on the line after the ask) and drops the second item when a
    letter asks for two. This keeps every line up to the boilerplate, but stops
    at the first blank line and caps the length — an unbounded capture on a letter
    whose boilerplate we don't recognize would otherwise swallow the footer."""
    match = _DOCUMENT_ASK.search(_normalize(text or ""))
    if not match:
        return None
    block = []
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line:
            if block:
                break  # blank line after the item(s) — the request is over
            continue
        if _BOILERPLATE_LINE.match(line):
            break
        block.append(line)
    document = "; ".join(block)[:200]
    # A leftover scrap of the ask's own sentence is not a document. Live proof:
    # one vet cover note's body ends at "...for us to review the", and a stray
    # "the" reaching the label would send Justin chasing a word.
    return document if len(document) >= 4 else None


def requested_document_date(document: str | None) -> str | None:
    """The treatment date named inside a requested document, as ISO.

    The date is the useful half: it identifies the visit, which is what makes the
    request resolvable to an invoice we already hold. Day-first — these are
    Australian letters (`18/05/2026` is 18 May)."""
    if not document:
        return None
    match = _DOCUMENT_DATE.search(document)
    if match:
        day, month, year = (int(g) for g in match.groups())
    else:
        match = _DOCUMENT_DATE_WORDS.search(document)
        if not match:
            return None
        day, month_name, year = match.group(1), match.group(2).lower(), match.group(3)
        month = _MONTHS.get(month_name)
        if month is None:
            return None
        day, year = int(day), int(year)
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None  # 31/02 and friends — a malformed date is not a date


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


def find_claims_by_reference(reference: str) -> list:
    """Claims sharing a Petcover reference are one Condition Thread (the ref is
    reused for the life of the condition). A reference-only event touches the
    thread's non-terminal claims only — settled/declined claims are finished and
    a later reference-reuse letter must never reopen them."""
    with db.get_connection() as conn:
        rows = conn.execute(
            f"{_CLAIM_SELECT} WHERE vc.petcover_reference = ?", (reference,)
        ).fetchall()
    return [r for r in rows if r["status"] not in TERMINAL_STATUSES]


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


def stated_claim_amount(text: str) -> float | None:
    """The amount a Petcover letter says it assessed, whichever template it uses.

    Approval letters print `Total amount claimed:`, the older settled style
    `Amount Claimed`, and the under-excess refusal just `Amount claimed:`. All
    three identify the claim far better than any ordering heuristic can.
    """
    figures = extract_approval_amounts(text)
    if figures.get("claimed_amount") is not None:
        return figures["claimed_amount"]
    return extract_settlement_amounts(text).get("claimed_amount")


def _claim_for_sr(submission_claims: list, stated_amount: float | None = None) -> object | None:
    """Which claim a per-Sr letter belongs to, or None when we cannot tell.

    **The amount the letter states decides it.** Where exactly one claim in the
    pool is worth what Petcover says they assessed, that is the claim — matched
    to the cent against its recorded claimable subtotal, falling back to the
    invoice total only for a claim that never had a subtotal recorded.

    Returning None is the important half. This used to attach the letter to "the
    oldest-transaction claim not yet serialized", on the reasoning that Petcover's
    serials run oldest-first. Measured against Petcover's own status table on
    2026-08-04 — the one that states a treatment date per serial — that heuristic
    was wrong on **every serial we hold**, and on 2026-08-05 it took an
    under-excess letter for a $55.74 arthritis claim and attached it to a
    $2,521.46 ALT workup, moving a settled claim to `below_excess`. A guess that
    is written as a fact and never checked is worse than no answer: an unlinked
    event is visible on the dashboard and one click from correct.

    So the ordering heuristic survives only where the letter states no amount at
    all (acknowledgements), and even then it records `sr_assigned_by` so the log
    can tell a guess from a citation.
    """
    unserialized = [c for c in submission_claims if c["petcover_sr"] is None]
    pool = unserialized or submission_claims

    if stated_amount is not None:
        matches = []
        for claim in pool:
            value, recorded = claimable_subtotal(claim["invoice_data"])
            if not recorded:
                value = (json.loads(claim["invoice_data"] or "{}") or {}).get("amount")
            if value is not None and abs(float(value) - stated_amount) <= 0.005:
                matches.append(claim)
        if len(matches) == 1:
            return matches[0]
        # Stated an amount, and nothing here is worth it — or two things are.
        # Either way this letter is not ours to place.
        return None

    return min(pool, key=lambda c: (c["_txn_date"] or "", c["id"]))


def _already_recorded(claim_id: int | None, event_type: str, email_id: str | None) -> bool:
    """Has this exact (email, claim, event) already been logged?

    Stops a re-read appending duplicate events and re-running the status/flag
    write, which would resurrect a settlement mismatch Justin had dismissed. That
    is what lets a classifier fix be applied to mail already in `processed_emails`.

    It does NOT make re-reading safe in general, and an earlier version of this
    docstring said it did. ADR-0020 records that event-level idempotency was tried
    against the real DB for the 2026-07-27 incident and did not help, because the
    problem was never duplicate events — it was a correctly-deduplicated event
    reaching the wrong claim. Two of the four claims that moved that day moved
    through transitions that are legal and that this guard does not see. Nothing
    demonstrably prevents that case today; see `openspec/BACKLOG.md`."""
    if email_id is None:
        return False  # manual events (confirm_resolved, dismiss) have no email
    with db.get_connection() as conn:
        return (
            conn.execute(
                "SELECT 1 FROM claim_status_events WHERE raw_email_id IS ? AND claim_id IS ? AND event_type = ?",
                (email_id, claim_id, event_type),
            ).fetchone()
            is not None
        )


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
            (
                claim_id,
                event_type,
                email_id,
                json.dumps(detail),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return cur.lastrowid


def transition_allowed(current: str | None, event_type: str) -> bool:
    """Would `apply_event` move a claim at `current` on this event type?

    For callers that must destroy something *before* the state write and would
    otherwise leave a claim half-changed — `invoice_matching.unmatch` wipes the
    invoice, and a refusal after that wipe strands a submitted claim with no
    invoice and its old status. Asking first keeps the table the only authority;
    duplicating the lookup at the call site is how the convention rotted before.

    A stateless or backfill event answers False: neither is a transition."""
    target = STATE_EVENTS.get(event_type)
    return target is not None and target in TRANSITIONS.get(current, frozenset())


def apply_event(
    claim_id: int,
    event_type: str,
    detail: dict | None = None,
    email_id: str | None = None,
    replaying: bool = False,
) -> dict:
    """The only writer of `vet_claims.status`.

    Appends the event unconditionally — the event happened, and hiding it is how
    the 2026-07-28 audit became necessary — then consults `TRANSITIONS`:

    - legal      -> writes the new state
    - illegal    -> writes nothing, flags the claim naming both states and the
                    event id, and leaves the event in place as evidence
    - stateless  -> records only; no state write and no refusal

    Returns `{"applied", "state", "refused"}`, where `state` is the claim's state
    afterwards either way, so a caller can report what actually happened rather
    than what it asked for.

    `replaying=True` means the caller is deliberately re-applying mail the log has
    already seen. A refused *transition* is then the expected outcome for every
    claim whose state has moved on since, so it is recorded and returned but NOT
    written to the claim's flag — on 2026-08-05 a recovery replay left
    `refused settled -> acknowledged` on six claims and, on one of them, displaced
    the finding the replay existed to produce. It suppresses nothing else: an
    unknown event type or an unknown backfill status is a defect whether or not
    anyone is replaying, and stays flagged."""
    with db.get_connection() as conn:
        row = conn.execute("SELECT status FROM vet_claims WHERE id = ?", (claim_id,)).fetchone()
    if row is None:
        return {"applied": False, "state": None, "refused": f"no claim #{claim_id}"}
    current = row["status"]
    event_id = _record_event(claim_id, event_type, email_id, detail or {})

    if event_type == BACKFILL_EVENT:
        # Seeds the state it names, exempt from TRANSITIONS by construction. The
        # column is normally already at this value — the event exists so the fold
        # can reach it. A status nobody declared is refused, not seeded.
        seeded = (detail or {}).get("status")
        if seeded not in TRANSITIONS or seeded is None:
            refusal = f"backfill event #{event_id} names unknown status {seeded!r}"
            _flag_claim(claim_id, refusal)
            return {"applied": False, "state": current, "refused": refusal}
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE vet_claims SET status = ?, updated_at = ? WHERE id = ?",
                (seeded, datetime.now(timezone.utc).isoformat(), claim_id),
            )
        return {"applied": True, "state": seeded, "refused": None}

    target = STATE_EVENTS.get(event_type)
    if target is None:
        # Stateless, or an event type nobody declared. The second is a defect, and
        # a silent no-op is exactly how it would stay one, so it is flagged.
        if event_type in STATELESS_EVENTS:
            return {"applied": False, "state": current, "refused": None}
        refusal = f"unknown event type '{event_type}' (event #{event_id}) — declared in neither STATE_EVENTS nor STATELESS_EVENTS"
        _flag_claim(claim_id, refusal)
        return {"applied": False, "state": current, "refused": refusal}

    if target not in TRANSITIONS.get(current, frozenset()):
        refusal = (
            f"refused {current} -> {target} from event #{event_id} ('{event_type}') "
            "— not a declared transition; state left alone"
        )
        # Recorded either way — the event above is the audit trail. The flag is
        # the surface Justin reads, and during a replay this refusal is expected
        # rather than news.
        if not replaying:
            _flag_claim(claim_id, refusal)
        return {"applied": False, "state": current, "refused": refusal}

    with db.get_connection() as conn:
        conn.execute(
            "UPDATE vet_claims SET status = ?, updated_at = ? WHERE id = ?",
            (target, datetime.now(timezone.utc).isoformat(), claim_id),
        )
    return {"applied": True, "state": target, "refused": None}


def _flag_claim(claim_id: int, reason: str) -> None:
    """Appends to the claim's flag rather than replacing it.

    One flag column carries everything Justin must act on, and `pipeline` builds
    the inline keyboard and picks the review PDF by matching *substrings* of it —
    so overwriting "possible additional invoice" silently removes his wrong-invoice
    button. A refusal is exactly the case where another flag is likely to be
    present already (a later letter reusing a settled thread's reference). Same
    concatenating shape `claim_forms` uses for the split shortfall."""
    with db.get_connection() as conn:
        existing = conn.execute("SELECT flag FROM vet_claims WHERE id = ?", (claim_id,)).fetchone()
        current = existing["flag"] if existing else None
        if current and reason in current:
            return  # already said; re-stating it on every tick is noise
        conn.execute(
            "UPDATE vet_claims SET flag = ?, updated_at = ? WHERE id = ?",
            (
                f"{current}; {reason}" if current else reason,
                datetime.now(timezone.utc).isoformat(),
                claim_id,
            ),
        )


# Every claim is born here. `TRANSITIONS[None]` says a new claim may only move to
# `pending_match`, and all three INSERT sites honour that — vet_detection writes
# it literally, and the two apportionment inserts (invoice_matching, claim_forms)
# write 'matched' but append a `matched` event, so the fold reaches the same place
# from this seed. Seeding at None instead would refuse every claim's first event,
# because no creation event exists to consume the None->pending_match step.
CREATED_STATE = "pending_match"


# Reversion is Phase 2 (task 7.1). The fold used to skip events named by a
# `state_reverted` event, but nothing ever wrote one — the live log holds zero,
# and `apply_event` could not produce one because the type was stateless. Removed
# 2026-07-31 with the type itself, so an attempt to revert now hits the unknown-
# event-type refusal and flags the claim instead of being silently recorded and
# ignored. Rebuild both together when `revert_state` lands, and note the fold has
# to handle reverting a reversion at that point, which this never did.
def _fold(rows) -> str:
    state = CREATED_STATE
    for row in rows:
        if row["event_type"] == BACKFILL_EVENT:
            # A seed, not a transition — see BACKFILL_EVENT. Ignored if it names a
            # status nobody declared, rather than seeding the fold with nonsense.
            seeded = json.loads(row["detail"] or "{}").get("status")
            if seeded in TRANSITIONS and seeded is not None:
                state = seeded
            continue
        target = STATE_EVENTS.get(row["event_type"])
        if target is None:
            continue  # stateless, or a type nobody declared — apply_event flagged it
        if target in TRANSITIONS.get(state, frozenset()):
            state = target
        # Illegal from the replayed state: skip this event and keep folding. One
        # bad event must not cost us the rest of the claim's history.
    return state


def project_state(claim_id: int) -> str | None:
    """The claim's state as its own event log says it is, ignoring the column.

    This is the fact `status` caches. During Phase 1 the two are compared and any
    disagreement is a detectable defect; in Phase 2 this becomes authoritative.
    Returns None only when the claim does not exist."""
    with db.get_connection() as conn:
        if conn.execute("SELECT 1 FROM vet_claims WHERE id = ?", (claim_id,)).fetchone() is None:
            return None
        rows = conn.execute(
            "SELECT id, event_type, detail FROM claim_status_events WHERE claim_id = ? "
            "ORDER BY created_at, id",
            (claim_id,),
        ).fetchall()
    return _fold(rows)


def project_all() -> dict[int, str]:
    """Bulk form for the tick — two queries, not one per claim."""
    with db.get_connection() as conn:
        claim_ids = [r["id"] for r in conn.execute("SELECT id FROM vet_claims")]
        events = conn.execute(
            "SELECT claim_id, id, event_type, detail FROM claim_status_events "
            "WHERE claim_id IS NOT NULL ORDER BY created_at, id"
        ).fetchall()
    by_claim: dict[int, list] = {}
    for row in events:
        by_claim.setdefault(row["claim_id"], []).append(row)
    return {claim_id: _fold(by_claim.get(claim_id, [])) for claim_id in claim_ids}


def state_projection_disagreements() -> list[dict]:
    """Claims whose stored status differs from what their events project.

    Reads only. Deliberately writes nothing — not the column, not a flag: during
    Phase 1 the comparison is evidence about the projection, and a comparison
    that repairs what it measures can't be trusted to have measured anything.

    A non-zero count before the backfill is expected and named in the change's
    tasks.md: every transition we performed by hand predates the event log."""
    with db.get_connection() as conn:
        stored = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM vet_claims")}
    projected = project_all()
    return [
        {"claim_id": claim_id, "stored": status, "projected": projected.get(claim_id)}
        for claim_id, status in sorted(stored.items())
        if projected.get(claim_id) != status
    ]


def process_reply(
    email_id: str,
    subject: str,
    body: str,
    sender: str | None = None,
    recipients: str | None = None,
    replaying: bool = False,
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
    # What Petcover says this letter is worth. The strongest routing evidence we
    # get, and free — it is already in the text being classified.
    stated_amount = stated_claim_amount(text)
    unmatched_reason = "needs manual link — no claim matched"
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
                chosen = _claim_for_sr(pool, stated_amount)
                if chosen is not None:
                    claims = [chosen]
                    learn_sr = True
                else:
                    unmatched_reason = (
                        f"needs manual link — {reference} Sr {sr} states ${stated_amount:,.2f} "
                        f"and no claim awaiting a serial is worth that "
                        f"(candidates: {', '.join('#' + str(c['id']) for c in pool)})"
                    )
    elif reference:
        # Reference only: the thread's non-terminal claims, or — if none yet
        # hold the reference — the submission the ack is teaching it to.
        claims = find_claims_by_reference(reference) or correlate_ack(text)
    else:
        # No reference at all — pure ack/early-reply correlation.
        claims = correlate_ack(text)

    detail = {"subject": subject}
    if learn_sr:
        # Say that the serial was GUESSED. `_claim_for_sr` picks the
        # oldest-transaction un-serialized claim, which is a heuristic over
        # Petcover's ordering and nothing they ever confirmed — and against their
        # 2026-07-29 status table it was wrong on every serial we hold. Recording
        # how the link was made is what lets a wrong one be found later; the log
        # currently cannot distinguish a guess from a citation.
        detail["sr_assigned_by"] = "heuristic: oldest un-serialized claim"
    if event_type == "info_requested":
        # Which party owes the document decides Justin's next move: chase the vet,
        # or supply it himself. Nothing recorded this before — process_reply never
        # saw a header.
        detail.update(resolve_owed_by(recipients))
        # WHAT was asked for, and the treatment date it names. Left absent rather
        # than guessed when the phrase isn't there (the vet cover note's detail
        # lives in an attachment we get no text for).
        document = extract_requested_document(body)
        if document:
            detail["requested_document"] = document
            requested_date = requested_document_date(document)
            if requested_date:
                detail["requested_document_date"] = requested_date
    if event_type == "settled":
        detail.update(extract_settlement_amounts(body))
    elif event_type == "approved":
        # The approval email carries the only dollar breakdown in the whole
        # lifecycle — validate here, not at the later dollar-less 'settled' event.
        detail.update(extract_approval_amounts(body))

    if not claims:
        if not _already_recorded(None, event_type, email_id):
            _record_event(
                None,
                event_type,
                email_id,
                {**detail, "flag": unmatched_reason},
            )
        return

    now = datetime.now(timezone.utc).isoformat()
    for claim in claims:
        if _already_recorded(claim["id"], event_type, email_id):
            continue  # re-read of mail already applied — nothing new to record or write
        # Learn the reference and Sr BEFORE validating, not after. The check's
        # thread-sibling query reads `claim["petcover_reference"]`, so on the
        # letter that teaches it the query used to run with NULL and find no
        # siblings — live on 2026-07-30, claim #2 was told "fresh $150 excess
        # this policy year" one second after claim #8's letter in the same
        # thread and policy year had stated a $150.00 excess. Nothing else in
        # this loop changes order.
        learned = []
        learned_params = []
        if reference and not claim["petcover_reference"]:
            learned.append("petcover_reference = ?")
            learned_params.append(reference)
        if learn_sr and sr is not None and claim["petcover_sr"] is None:
            learned.append("petcover_sr = ?")
            learned_params.append(sr)
        if learned:
            with db.get_connection() as conn:
                conn.execute(
                    f"UPDATE vet_claims SET {', '.join(learned)}, updated_at = ? WHERE id = ?",
                    (*learned_params, now, claim["id"]),
                )
                claim = conn.execute(
                    "SELECT vet_claims.*, ? AS _txn_date FROM vet_claims WHERE id = ?",
                    (claim["_txn_date"], claim["id"]),
                ).fetchone()
        # Validate whenever a paid amount is actually stated — the newer
        # 'approved' email carries it; the older settled-with-PDF style
        # carries it directly in the settled email itself. Either way, the
        # dollar-less 'payment processed' settled email that FOLLOWS an
        # approval has nothing to validate and correctly no-ops here.
        settlement_flag = _validate_settlement(claim, detail, claim["_txn_date"])
        # The event and the state change are `apply_event`'s to make; what stays
        # here is the learning this reply enabled (reference, Sr) and the
        # settlement check. "unclassified" moving no state used to be an `if` in
        # this UPDATE and is now a property of the event type.
        outcome = apply_event(claim["id"], event_type, detail, email_id, replaying=replaying)
        with db.get_connection() as conn:
            updates = ["updated_at = ?"]
            params = [now]
            # A refusal already flagged this claim, and it is the more serious of
            # the two: the state did not move at all. Don't overwrite it with a
            # settlement note — that number is still in the event's detail.
            # A refusal outranks a settlement note — the state did not move at
            # all. But during a replay the refusal was never written, so nothing
            # is being overwritten and the finding is the only news there is:
            # claim #2's "claimable subtotal not recorded" lost to an unwritten
            # refusal on 2026-08-05 and reached no surface.
            refusal_holds_the_flag = bool(outcome["refused"]) and not replaying
            if settlement_flag and not refusal_holds_the_flag:
                updates.append("flag = ?")
                params.append(settlement_flag)
            elif (
                event_type == "acknowledged"
                and not reference
                and not claim["petcover_reference"]
                and not outcome["refused"]
            ):
                # spec: never guess or discard — flag visibly instead
                updates.append("flag = ?")
                params.append("unclassified — reference format not recognized")
            conn.execute(
                f"UPDATE vet_claims SET {', '.join(updates)} WHERE id = ?", (*params, claim["id"])
            )


def _policy_year_start(anniversary_mmdd: str, on: date) -> date:
    """Start of the policy year (anniversary→anniversary) containing date `on`."""
    mm, dd = (int(x) for x in anniversary_mmdd.split("-"))
    this_year = date(on.year, mm, dd)
    return this_year if on >= this_year else date(on.year - 1, mm, dd)


# The three settlement findings, by flag prefix. One column holds one flag, so
# the prefix is what tells the card, the dismissal and the waiting party which
# finding they are looking at.
SETTLEMENT_FLAG_PREFIXES = (
    "settlement mismatch",             # Check A — Petcover's own arithmetic
    "assessment difference",           # Check B — they assessed something else
    "claimable subtotal not recorded",  # we cannot check B at all
)


def _settlement_check_kind(flag: str | None) -> str | None:
    """Which check raised a flag. `assessment` is a question outstanding with
    Petcover; everything else is Justin's own review."""
    if not flag:
        return None
    if flag.startswith("assessment difference"):
        return "assessment"
    if flag.startswith(SETTLEMENT_FLAG_PREFIXES):
        return "arithmetic"
    return None


def _check_petcovers_arithmetic(detail: dict, paid_amount: float) -> str | None:
    """Check A — re-add the letter's own stated line items and see whether they
    reach the amount it says was paid.

    Arithmetic on given data, not a model of Petcover's policy (design.md
    Decision 2): every input is a labelled field on the letter being validated,
    including the percentage. Skipped entirely when the letter states no
    percentage — the five `approved` events written before the age-contribution
    pattern shipped take that path rather than being checked against a term
    they never captured.

    Verified against all nine live approval letters (2026-08-04), exact to the
    cent, including `DC1-27-5628` Tr 8 — the only one with a non-zero
    non-claimable amount ($580.74 − $135.00) × 0.65 = $289.73, and so the only
    one that distinguishes this formula from the post-mortem's
    `(claimed − excess) × 0.65`, which is wrong there by $87.75.
    """
    claimed = detail.get("claimed_amount")
    age_percent = detail.get("age_contribution_percent")
    if claimed is None or age_percent is None:
        return None
    excess = detail.get("fixed_excess_stated") or 0.0
    non_claimable = detail.get("non_claimable_stated") or 0.0
    # ponytail: percentage excess has been $0.00 [0%] on every letter seen, so
    # where it sits in the order of operations is unverified. Deducted with the
    # others; if one ever arrives non-zero and Check A fires on an otherwise
    # sound letter, that placement is the first thing to re-read.
    percentage_excess = detail.get("percentage_excess_stated") or 0.0
    expected = (claimed - excess - non_claimable - percentage_excess) * (1 - age_percent)
    if abs(paid_amount - expected) <= SETTLEMENT_TOLERANCE:
        return None
    extra = f", percentage excess ${percentage_excess:.2f}" if percentage_excess else ""
    return (
        "settlement mismatch — Petcover's own figures don't add up: they state claimed "
        f"${claimed:.2f}, fixed excess ${excess:.2f}, non-claimable ${non_claimable:.2f}"
        f"{extra} and age contribution {age_percent:.0%}, which comes to ${expected:.2f}, "
        f"but they paid ${paid_amount:.2f} — review"
    )


def _claims_whose_invoice_matches(amount: float, exclude_claim_id: int) -> list[int]:
    """Which other claims of ours are worth exactly this much.

    Petcover's stated figure is never a mystery number — on 2026-08-04, all
    seven letters carrying one matched some real claim of ours to the cent. What
    was wrong was which claim we had put under that serial, so the useful thing
    to tell Justin is whose invoice the figure actually is.
    """
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT id, invoice_data FROM vet_claims WHERE id != ? AND invoice_data IS NOT NULL",
            (exclude_claim_id,),
        ).fetchall()
    matches = []
    for row in rows:
        value, recorded = claimable_subtotal(row["invoice_data"])
        if not recorded:
            value = (json.loads(row["invoice_data"] or "{}") or {}).get("amount")
        if value is not None and abs(float(value) - amount) <= 0.005:
            matches.append(row["id"])
    return matches


def _check_what_petcover_assessed(claim, detail: dict, claimable: float) -> str | None:
    """Check B — does the amount Petcover assessed match what we submitted under
    this serial.

    A separate finding from Check A with a different audience: their arithmetic
    can be perfect on an amount that is not this claim's. Claim #8 is the live
    case — $(351.50 − 150.00) × 0.65 = $130.97 to the cent, against a claim we
    submitted at $446.50.

    **What this most likely means is that OUR serial map is wrong, not their
    assessment.** Petcover's status table of 2026-07-29 states a treatment date
    per serial, and against it every one of our serial assignments was on the
    wrong claim while every stated amount matched its true claim's invoice
    exactly (7/7). `_claim_for_sr` assigns a serial to "the oldest-transaction
    claim not yet serialized", a heuristic over Petcover's ordering that nothing
    ever confirmed. So this flag names the claim whose invoice the figure really
    is, and still refuses to re-route: that is a money-affecting write and
    Justin's call.
    """
    claimed = detail.get("claimed_amount")
    if claimed is None or abs(claimed - claimable) <= SETTLEMENT_TOLERANCE:
        return None
    where = claim["petcover_reference"] or "no reference"
    sr = claim["petcover_sr"]
    elsewhere = _claims_whose_invoice_matches(claimed, claim["id"])
    if elsewhere:
        whose = ", ".join(f"#{i}" for i in elsewhere)
        cause = (
            f"${claimed:.2f} is claim {whose}'s invoice exactly, so the serial is most likely "
            "on the wrong claim here — check the treatment date before re-routing anything"
        )
    else:
        cause = "no invoice of ours matches that figure — ask Petcover which invoice this assessed"
    return (
        f"assessment difference — claim #{claim['id']} ({where} Sr {sr if sr is not None else '?'}): "
        f"we submitted ${claimable:.2f}, Petcover states they assessed ${claimed:.2f}. "
        f"Their arithmetic on their own figure is correct — {cause}"
    )


def _validate_settlement(claim, detail: dict, txn_date_iso: str) -> str | None:
    """Two independent checks over one settlement, never fused into one flag.

    Fusing them is what made the real finding invisible: claim #8's live flag
    read *expected $296.50, Petcover paid $130.97 (fresh $150 excess this policy
    year)* — our number not theirs, an excess the letter had actually stated, and
    no mention of the one thing that genuinely did not reconcile.

    Check A asks whether Petcover's own figures add up; Check B asks whether they
    assessed what we submitted. Both can fire; the column holds one flag, so A
    wins — a supplier's arithmetic being wrong is the more serious of the two and
    the figures for both are on the event either way.

    When the letter states no line items (the older PDF style, `Amount Claimed` /
    `Total Payable` only) the transaction-date-bucketed excess/cap fallback below
    still runs — ADR-0011, ADR-0013.

    Closed-year default: our claim history for any policy year that has already
    ended is presumed incomplete (some vet spend never hits the tracked card,
    and bank-CSV coverage doesn't reach arbitrarily far back) — so excess/cap
    math only runs for the CURRENT, still-open policy year. A claim whose own
    transaction falls in an already-closed year is assumed to have already
    passed the threshold: expected = full claimable, no excess deducted.

    Every flag is a warning to review in EITHER direction, never an assertion
    that Petcover is wrong, and nothing here auto-disputes or sends mail."""
    paid_amount = detail.get("paid_amount")
    if paid_amount is None:
        return None
    claimable, subtotal_recorded = claimable_subtotal(claim["invoice_data"])

    if detail.get("claimed_amount") is not None and detail.get("age_contribution_percent") is not None:
        # The letter states its own breakdown, so it — not our excess model —
        # decides the expectation. The excess/cap path below never runs here.
        arithmetic = _check_petcovers_arithmetic(detail, paid_amount)
        if arithmetic:
            return arithmetic
        if not subtotal_recorded:
            # No expectation is computed. Naming the letter's figures is what
            # keeps the letter from being lost behind a silent None.
            return (
                f"claimable subtotal not recorded — claim #{claim['id']}: Petcover states they "
                f"assessed ${detail['claimed_amount']:.2f} and paid ${paid_amount:.2f}, and their "
                "figures add up, but nothing was recorded as this claim's claimable subtotal so "
                "there is nothing to check it against — review"
            )
        return _check_what_petcover_assessed(claim, detail, claimable)

    if not subtotal_recorded:
        return None  # nothing to compare against — don't fabricate an expectation

    with db.get_connection() as conn:
        pet = conn.execute(
            "SELECT policy_anniversary FROM pets WHERE id = ?", (claim["pet_id"],)
        ).fetchone()
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
                thread_prior = (
                    conn.execute(
                        "SELECT bt.date AS txn_date FROM claim_status_events e "
                        "JOIN vet_claims v ON v.id = e.claim_id "
                        "JOIN bank_transactions bt ON bt.id = v.transaction_id "
                        "WHERE v.petcover_reference IS ? AND e.event_type IN ('approved', 'settled') "
                        "AND e.claim_id != ?",
                        (reference, claim["id"]),
                    ).fetchall()
                    if reference
                    else []
                )
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
            # A stated excess is a fact from the letter; the inferred one is our
            # model of their policy. Where they state it, we neither infer nor
            # narrate an inference — "fresh $150 excess this policy year" on a
            # letter that printed $150.00 is a claim about our own reasoning
            # dressed up as a finding about theirs.
            stated_excess = detail.get("fixed_excess_stated")
            if stated_excess is not None:
                expected = claimable - stated_excess
                reason = ""
            else:
                expected = claimable - (0.0 if excess_consumed else POLICY_EXCESS)
                reason = (
                    " (excess already used this policy year)"
                    if excess_consumed
                    else " (fresh $150 excess this policy year)"
                )
            expected = max(0.0, min(expected, remaining_cap))
            note = ""

    if abs(paid_amount - expected) > SETTLEMENT_TOLERANCE:
        return f"settlement mismatch — we expected ${expected:.2f}, Petcover paid ${paid_amount:.2f}{reason}{note} — review"
    return None


def link_event(event_id: int, claim_id: int) -> bool:
    """Manually attaches an unlinked event to a claim (the dashboard's answer
    to 'needs manual link'). Link only — deliberately does NOT rewrite the
    claim's status: a late-linked old email must not regress a settled claim.
    Returns False when the event or claim doesn't exist or is already linked."""
    with db.get_connection() as conn:
        event = conn.execute(
            "SELECT * FROM claim_status_events WHERE id = ?", (event_id,)
        ).fetchone()
        claim = conn.execute("SELECT 1 FROM vet_claims WHERE id = ?", (claim_id,)).fetchone()
        if event is None or event["claim_id"] is not None or claim is None:
            return False
        conn.execute(
            "UPDATE claim_status_events SET claim_id = ? WHERE id = ?", (claim_id, event_id)
        )
    return True


def mark_sent(claim_id: int) -> dict:
    """Advances drafted->sent, which is what starts Petcover reply polling for
    the claim. A batch submission is several claims sharing one draft — sending
    that one email sends them all, so one action advances the whole group.
    Shared by the dashboard route and the Telegram /sent command."""
    # No timestamp is computed here on purpose: this function stopped writing
    # vet_claims itself when apply_event became the only status writer, and
    # apply_event stamps its own event row. A leftover `now` sat unused here
    # until ruff flagged it.
    with db.get_connection() as conn:
        claim = conn.execute(
            "SELECT status, draft_id FROM vet_claims WHERE id = ?", (claim_id,)
        ).fetchone()
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
            return {
                "ok": False,
                "message": f"Claim #{claim_id} isn't drafted (status: {claim['status']}).",
            }
        # Same `AND status = 'drafted'` selection the single group UPDATE used to
        # do — one tap still advances the whole submission — but resolved to ids
        # first, so each member's send is its own event.
        if claim["draft_id"]:
            members = [
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM vet_claims WHERE draft_id = ? AND status = 'drafted' ORDER BY id",
                    (claim["draft_id"],),
                )
            ]
        else:
            members = [claim_id]
    count = sum(
        1
        for member in members
        if apply_event(member, "sent", {"draft_id": claim["draft_id"]})["applied"]
    )
    with db.get_connection() as conn:
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
    last_flag = max(
        (i for i, t in enumerate(types) if t in ("info_requested", "suspended")), default=None
    )
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
            r["expected"] = {
                "available": False,
                "value": None,
                "note": "no policy excess/cap on file",
            }
        return

    # A claim with no invoice matched yet has no claimable subtotal — nothing to
    # estimate against. Flag unavailable and keep it out of the group math.
    priced = []
    for r in rows:
        if r["claimable"] is None:
            # Two different absences, and "no invoice yet" is only true for one
            # of them: five live claims (#2, #16, #18, #19, #21) DO have an
            # invoice and simply never had a claimable subtotal recorded on it.
            # Saying "no invoice yet" there sends Justin looking for a document
            # he already has.
            note = (
                "invoice on file, but no claimable subtotal recorded"
                if r.get("invoice_present")
                else "no invoice yet"
            )
            r["expected"] = {"available": False, "value": None, "note": note}
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
            parts.append(
                {"row": r, "condition": r["condition_text"] or "", "amount": r["claimable"] or 0}
            )

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
            # Petcover pays 65% of what's left (Justin, 2026-08-04: "Petcover
            # only paying 65% of a claim"), which is the same figure their
            # letters print as a 35% Age Contribution — a stated policy term, not
            # a rate inferred from the letters. Applied after the excess and
            # before the cap, the order the letters themselves use: on Tr 8,
            # $(580.74 − 135.00) × 0.65 = $289.73 exactly.
            payable = after_excess * config.PETCOVER_BENEFIT_RATE
            # bound the running per-year total by the annual cap
            used = year_totals.get(year, 0.0)
            allowed = max(0.0, cap - used)
            value = round(min(payable, allowed), 2)
            year_totals[year] = used + value
            benefit = f", {config.PETCOVER_BENEFIT_RATE:.0%} benefit"
            if group_claimable < excess:
                note = (
                    f"{condition or 'condition'} YTD ${group_claimable:.2f} < ${excess:.0f} excess"
                )
            else:
                note = f"est. after ${excess:.0f} excess{benefit}"
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
    owed_by: dict[int, str] = {}
    requested_document: dict[int, str] = {}
    for e in events:
        if e["event_type"] == "unclassified":
            continue
        last_event[e["claim_id"]] = {"type": e["event_type"], "at": e["created_at"]}
        if e["event_type"] == "settled":
            paid = json.loads(e["detail"] or "{}").get("paid_amount")
            if paid is not None:
                settled_paid[e["claim_id"]] = paid
        # Who owes the requested document — the label says so, and the events
        # are already in hand, so no second query for it.
        if e["event_type"] == "info_requested":
            info = json.loads(e["detail"] or "{}")
            if info.get("owed_by"):
                owed_by[e["claim_id"]] = info["owed_by"]
            # WHAT was asked for, so the chip can name it rather than saying
            # "more vet info" at a clinic that needs a document name to look up.
            if info.get("requested_document"):
                requested_document[e["claim_id"]] = info["requested_document"]

    claims_by_txn: dict[int, list] = {}
    for c in claim_rows:
        claimable, claimable_recorded = claimable_subtotal(c["invoice_data"])
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
                # A recorded $0.00 and an absent key are different answers and
                # both arrive here as a falsy `claimable`; only this flag tells
                # a renderer which one it is holding.
                "claimable_recorded": claimable_recorded,
                # Which of the two absences this is — see _apply_excess_and_cap.
                "invoice_present": bool(c["invoice_data"]),
                "txn_date": c["txn_date"],
                "annual_excess": c["annual_excess"],
                "annual_cap": c["annual_cap"],
                "policy_anniversary": c["policy_anniversary"],
                "last_event": last_event.get(c["id"]),
                "settled_paid": settled_paid.get(c["id"]),
                "owed_by": owed_by.get(c["id"]),
                "requested_document": requested_document.get(c["id"]),
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
        _apply_excess_and_cap(
            pet_claims, first["annual_excess"], first["annual_cap"], first["policy_anniversary"]
        )
    # Settled claims override the estimate with what Petcover actually paid.
    for pet_claims in by_pet.values():
        for cl in pet_claims:
            if cl["settled_paid"] is not None:
                cl["expected"] = {
                    "available": True,
                    "value": cl["settled_paid"],
                    "note": "actual",
                    "estimate": False,
                }

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
    # Local import: status_labels reads this module's action determination, so
    # importing it at the top would close a cycle.
    from . import status_labels

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
                    # Worded here, where the flag/pet/condition/owed_by the
                    # label derives from are all in hand — the renderer only has
                    # this row.
                    "label": status_labels.label(claim),
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

# The only four things a card may say about who is blocked. Naming the wrong
# party is how a chase never happens — the same reason `info_requested` carries
# `owed_by` and never defaults it.
PETCOVER_WAITING_ON_YOU = "Petcover is waiting on you"
YOU_WAITING_ON_PETCOVER = "you're waiting on Petcover"
YOU_WAITING_ON_THE_VET = "you're waiting on the vet"
NOBODY_WAITING = "nobody is waiting — this is for your review"
WAITING_PARTIES = (
    PETCOVER_WAITING_ON_YOU,
    YOU_WAITING_ON_PETCOVER,
    YOU_WAITING_ON_THE_VET,
    NOBODY_WAITING,
)

# What each kind means to Justin, what stalls until he acts, and who is actually
# waiting. Three elements, all data on the kind — a phrase assembled at the card
# site is how the Telegram card, the dashboard and the chat agent come to
# disagree (ADR-0021, status_labels.py). Where one kind covers two situations the
# third element is a dict and `waiting_party` resolves it; there is no default.
_ACTION_META = {
    "split_proposal": ("Confirm invoice split", "one invoice paid over several charges", NOBODY_WAITING),
    "unmatch": ("Check invoice match", "a possible wrong/extra invoice is attached", NOBODY_WAITING),
    # Petcover asked for something. Who holds it decides who is blocked — the
    # answer is already on the claim as `owed_by`, so it is read, not assumed.
    "confirm_resolved": (
        "Confirm resolved",
        "a Petcover request is still open",
        {"justin": PETCOVER_WAITING_ON_YOU, "vet": YOU_WAITING_ON_THE_VET},
    ),
    "mark_sent": ("Send Gmail draft", "Petcover reply tracking hasn't started", NOBODY_WAITING),
    "invoice_request_sent": ("Invoice request sent?", "no invoice means no claim", YOU_WAITING_ON_THE_VET),
    "set_condition": ("Set condition", "the claim can't be drafted", NOBODY_WAITING),
    "assign_pet": ("Assign pet", "the claim can't be filled", NOBODY_WAITING),
    # Justin's own words on the old card: "It also said Petcover is waiting on me
    # but that doesn't seem to be the case if I have to just check the payment
    # discrepancy". Arithmetic he can check himself; an assessment difference is
    # a question already put to Petcover.
    "dismiss_mismatch": (
        "Review settlement",
        "a paid-vs-expected difference is unreviewed",
        {"arithmetic": NOBODY_WAITING, "assessment": YOU_WAITING_ON_PETCOVER},
    ),
    "blocked_insurer": ("Define claim process", "every claim for this pet is stuck", NOBODY_WAITING),
}


def waiting_party(kind: str, key: str | None = None) -> str:
    """Who is blocked on this action. Raises rather than defaulting: a kind
    added without a waiting party, or with a situation this map doesn't cover,
    is a bug that must fail loudly instead of naming the wrong party."""
    declared = _ACTION_META[kind][2]
    if isinstance(declared, str):
        return declared
    if key not in declared:
        raise KeyError(f"{kind} declares no waiting party for {key!r}")
    return declared[key]


def _waiting_key(kind: str, claim: dict) -> str | None:
    """The discriminator for the kinds whose waiting party depends on the claim."""
    if kind == "dismiss_mismatch":
        return _settlement_check_kind(claim.get("flag")) or "arithmetic"
    if kind == "confirm_resolved":
        return "vet" if claim.get("owed_by") == "vet" else "justin"
    return None

_INSURER_UNDEFINED = "claim process not yet defined"


def _action_kind(
    claim: dict, open_split_claim_ids: set, unresolved_event_claim_ids: set
) -> str | None:
    """The single action a claim needs, or None when it's waiting on someone
    else (sent/acknowledged/approved = Petcover's turn) or finished."""
    if claim["id"] in open_split_claim_ids:
        return "split_proposal"
    # `unmatch` outranks `confirm_resolved` and lives in the row-only part below,
    # so it is checked here too rather than silently losing to the set check.
    if claim["id"] in unresolved_event_claim_ids and not (claim["flag"] or "").startswith(
        "possible additional invoice"
    ):
        return "confirm_resolved"
    return _action_kind_from_row(claim)


def _action_kind_from_row(claim: dict) -> str | None:
    """The part of the determination that reads only the claim's own row.

    Split out so `status_labels` can ask "what does this claim need" from a
    rendering path without the two DB queries `_action_kind`'s set arguments
    require. One function, so a label can never disagree with an action.
    """
    flag = claim["flag"] or ""
    if flag.startswith("possible additional invoice"):
        return "unmatch"
    if claim["status"] == "drafted":
        return "mark_sent"
    # flag, NOT invoice_request_sent_at + draft_id: draft_id is overloaded
    # (claim drafts AND invoice-request drafts), so that pair matches almost
    # every claim and is useless as a predicate.
    if flag == "invoice_request_drafted":
        return "invoice_request_sent"
    if flag.endswith(_INSURER_UNDEFINED):
        return "blocked_insurer"
    if flag.startswith(SETTLEMENT_FLAG_PREFIXES):
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
        open_splits = conn.execute(
            "SELECT claim_ids FROM split_proposals WHERE status = 'open'"
        ).fetchall()
    open_split_claim_ids = {
        cid for row in open_splits for cid in json.loads(row["claim_ids"] or "[]")
    }
    unresolved_event_claim_ids = {
        entry["claim"]["id"] for entry in dashboard_lists()["needs_action"]
    }

    today = datetime.now(timezone.utc).date()
    actions = []
    for entry in visit_ledger():
        txn = entry["txn"]
        for claim in entry["claims"]:
            kind = _action_kind(claim, open_split_claim_ids, unresolved_event_claim_ids)
            if kind is None:
                continue
            title, blocks, _ = _ACTION_META[kind]
            actions.append(
                {
                    "kind": kind,
                    "title": title,
                    "blocks": blocks,
                    "waiting": waiting_party(kind, _waiting_key(kind, claim)),
                    # Straight off the ledger row this loop is already walking —
                    # a second calculation is a second answer that eventually
                    # disagrees with the dashboard's.
                    "claimable": claim["claimable"],
                    "claimable_recorded": claim["claimable_recorded"],
                    "expected": claim["expected"],
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
                    # The card has room for the full phrase the chip has to
                    # shorten — "Consultation notes dated 18/05/2026" is what a
                    # clinic can actually look up.
                    "owed_by": claim["owed_by"],
                    "requested_document": claim["requested_document"],
                    # Flag first — it is a failure, and failures stay visible.
                    "detail": claim["flag"] or claim["requested_document"] or "",
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
        newest = max(
            (latest_event[c["id"]] for c in claims if c["id"] in latest_event),
            key=lambda e: e["created_at"],
            default=None,
        )
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
    claimable, claimable_recorded = claimable_subtotal(invoice)
    # Only the figures — the raw detail also holds subjects and bodies, which
    # would blow the chat turn's token budget for no answering power.
    # Plus who owes a requested document and what it is: the chip only has room
    # for "consult notes", and the date in the full phrase is what identifies the
    # visit a clinic has to look up.
    figure_keys = (
        "claimed_amount",
        "paid_amount",
        "fixed_excess_stated",
        "age_contribution_stated",
        "age_contribution_percent",
        "non_claimable_stated",
        "percentage_excess_stated",
        "subject",
        "owed_by",
        "clinic",
        "clinic_email",
        "requested_document",
        "requested_document_date",
    )
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
        "claimable_amount": claimable,
        # Explicit, so the agent answers "not recorded" instead of reading a
        # null as zero or reaching for invoice_amount.
        "claimable_amount_recorded": claimable_recorded,
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


def treatment_date(invoice_data: str | None, txn_date: str) -> tuple[str, bool]:
    """When the pet was actually treated, and whether we know it or assumed it.

    Petcover's deadline is anchored on treatment — *"your claim must be submitted
    within one year of your pet receiving treatment"* — and the bank charge is a
    different date. Confirmed live: The Shire Vet treated Aari on **19 Jun 2026**
    and Echo on **30 Jun**, and both were paid on **06/07/2026** (receipts
    SHV49c1622284e5 / SHVd5b232905fdb, forwarded 27 Jul). Anchoring on the charge
    silently grants up to weeks of slack the policy does not give.

    The EARLIEST date on the invoice wins, because an invoice billing several
    visits expires on its oldest one. Falls back to the transaction date when no
    invoice is attached, and says so, rather than pretending to know.
    """
    invoice = json.loads(invoice_data or "{}") if invoice_data else {}
    candidates = {invoice.get("date")} | {item.get("date") for item in (invoice.get("items") or [])}
    known = sorted(d for d in candidates if d)
    return (known[0], True) if known else (txn_date[:10], False)


def unanswered_vet_requests() -> list[dict]:
    """Information requests the vet owes and nobody has answered.

    "Unanswered" cannot mean "no reply seen": the clinic replies to Petcover, not
    to us, and all ten historical vet-addressed threads hold exactly one message
    (ADR-0020). The only available signal is that our claim still sits on an
    unresolved request — the same determination the dashboard's needs-action list
    makes, so the two can never disagree.

    Ordered by days remaining, not by request age: the deadline is anchored on the
    TREATMENT date ("your claim must be submitted within one year of your pet
    receiving treatment"), so a request made late in that year has less slack than
    its age suggests. Past-deadline requests are excluded — they are history for
    the register, not an action anyone can still take."""
    unresolved = {entry["claim"]["id"] for entry in dashboard_lists()["needs_action"]}
    if not unresolved:
        return []
    today = datetime.now(timezone.utc).date()
    out = []
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT vc.id, vc.status, vc.invoice_data, bt.date AS txn_date, bt.merchant, p.name AS pet_name, "
            "(SELECT detail FROM claim_status_events e WHERE e.claim_id = vc.id "
            " AND e.event_type = 'info_requested' ORDER BY e.created_at DESC, e.id DESC LIMIT 1) AS info, "
            "(SELECT created_at FROM claim_status_events e WHERE e.claim_id = vc.id "
            " AND e.event_type = 'info_requested' ORDER BY e.created_at DESC, e.id DESC LIMIT 1) AS asked_at "
            "FROM vet_claims vc JOIN bank_transactions bt ON bt.id = vc.transaction_id "
            "LEFT JOIN pets p ON p.id = vc.pet_id "
            f"WHERE vc.id IN ({','.join('?' * len(unresolved))})",
            tuple(unresolved),
        ).fetchall()
    for row in rows:
        info = json.loads(row["info"] or "{}")
        if info.get("owed_by") != "vet":
            continue  # asked of Justin, or unrecorded — not a vet chase
        treated_on, from_invoice = treatment_date(row["invoice_data"], row["txn_date"])
        days_left = (
            config.INFO_REQUEST_DEADLINE_DAYS - (today - date.fromisoformat(treated_on)).days
        )
        if days_left < 0:
            continue  # past the submission deadline — the register's problem now
        out.append(
            {
                "claim_id": row["id"],
                "pet_name": row["pet_name"],
                "merchant": row["merchant"],
                "clinic": info.get("clinic") or row["merchant"],
                "clinic_email": info.get("clinic_email"),
                "requested_document": info.get("requested_document"),
                # Derived when the event predates the date parsing, so an older
                # request still resolves to its visit without a backfill.
                "requested_document_date": (
                    info.get("requested_document_date")
                    or requested_document_date(info.get("requested_document"))
                ),
                "treated_on": treated_on,
                "treatment_date_known": from_invoice,
                "asked_at": row["asked_at"],
                "days_outstanding": (today - date.fromisoformat(row["asked_at"][:10])).days
                if row["asked_at"]
                else None,
                "days_left": days_left,
            }
        )
    out.sort(key=lambda r: r["days_left"])
    return out


def dismiss_mismatch(claim_id: int) -> dict:
    """Clears a settlement-mismatch flag once Justin has looked at it. Records a
    `mismatch_dismissed` event rather than just wiping the flag — the append-only
    log (ADR-0008) is the audit trail, and a silently-erased discrepancy is
    exactly the invisible failure the hard rules forbid."""
    with db.get_connection() as conn:
        claim = conn.execute(
            "SELECT flag, invoice_data FROM vet_claims WHERE id = ?", (claim_id,)
        ).fetchone()
        if claim is None:
            return {"ok": False, "message": f"No claim #{claim_id} found."}
        if not (claim["flag"] or "").startswith(SETTLEMENT_FLAG_PREFIXES):
            return {
                "ok": False,
                "message": f"Claim #{claim_id} has no settlement difference to review.",
            }
        dismissed = claim["flag"]
        # The letter's figures, from the event that carried them. Prose in
        # `dismissed_flag` is not a record: event 58 holds claim #2's entire
        # finding as a sentence, and nothing can query a sentence.
        stated = {}
        for row in conn.execute(
            "SELECT detail FROM claim_status_events WHERE claim_id = ? "
            "AND event_type IN ('approved', 'settled') ORDER BY id",
            (claim_id,),
        ):
            figures = json.loads(row["detail"] or "{}")
            if figures.get("paid_amount") is not None:
                stated = figures
        conn.execute(
            "UPDATE vet_claims SET flag = NULL, updated_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), claim_id),
        )
    claimable, recorded = claimable_subtotal(claim["invoice_data"])
    check = _settlement_check_kind(dismissed)
    _record_event(
        claim_id,
        "mismatch_dismissed",
        None,
        {
            "dismissed_flag": dismissed,
            "check": check,
            "claimed_amount": stated.get("claimed_amount"),
            "paid_amount": stated.get("paid_amount"),
            "fixed_excess_stated": stated.get("fixed_excess_stated"),
            "non_claimable_stated": stated.get("non_claimable_stated"),
            "claimable_subtotal": claimable,
            "claimable_subtotal_recorded": recorded,
        },
    )
    if check == "assessment":
        # Justin has read it; Petcover still hasn't answered. Dismissal clears
        # the card, not the question — the claim stays in the review queue.
        return {
            "ok": True,
            "message": f"Claim #{claim_id}: noted. It stays in the review queue until Petcover answers.",
        }
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
    return {
        "ok": True,
        "message": f"Claim #{claim_id}: invoice request marked sent — watching for the reply.",
    }


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
        # A dismissed ASSESSMENT difference is a question outstanding with
        # Petcover, not a thing Justin has resolved — so dismissal clears the
        # card and this keeps the question on a surface. An arithmetic dismissal
        # is Justin saying "I checked this", and correctly disappears.
        elif event["event_type"] == "mismatch_dismissed" and (
            json.loads(event["detail"] or "{}").get("check") == "assessment"
        ):
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
            (
                i
                for i, e in enumerate(claim_events)
                if e["event_type"] in ("info_requested", "suspended")
            ),
            default=None,
        )
        if last_flag_idx is not None and not any(
            e["event_type"] == "confirmed_resolved" for e in claim_events[last_flag_idx + 1 :]
        ):
            needs_action.append({"claim": claim, "events": claim_events})
        for event in claim_events:
            if event["event_type"] == "settled":
                detail = json.loads(event["detail"] or "{}")
                # Our own record of what was claimed, and NOTHING in its place:
                # the invoice total is a different quantity and Petcover's own
                # figure is the thing this row exists to compare against, so
                # falling back to either turns a difference into an agreement.
                claimed, _ = claimable_subtotal(claim["invoice_data"])
                settled_reconciliation.append(
                    {
                        "claim": claim,
                        "claimed_amount": claimed,
                        "paid_amount": detail.get("paid_amount"),
                    }
                )

    return {
        "needs_action": needs_action,
        "settled_reconciliation": settled_reconciliation,
        "unclassified": review_queue,
    }
