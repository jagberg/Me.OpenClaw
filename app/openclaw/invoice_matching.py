import base64
import json
import re
from datetime import date, datetime, timedelta, timezone

from . import config, db, gmail_client, llm

# The bank charge is the CEILING on what can be claimed — it can exceed the
# invoice total via card surcharge (confirmed live: real $580.74 invoice
# charged as $585.39, 0.8%) or cover several invoices at once (confirmed live:
# one $177.50 charge = a $35 + a $142.50 invoice, different pets). So a
# candidate invoice matches when its total is AT MOST the charge (+1c float
# rounding); an invoice larger than the charge can't be the right one.
AMOUNT_TOLERANCE_FLAT = 0.01
# Gap beyond a plausible surcharge — flags "another invoice may exist".
SURCHARGE_MARGIN_PCT = 0.02

# Invoice line items that are routine/preventive care, not illness or injury —
# excluded from the claimable amount (most pet policies exclude them; Justin
# maintains this list).
NON_CLAIMABLE_KEYWORDS = [
    "vaccination",
    "vaccine",
    "desexing",
    "worming",
    "deworm",
    "heartworm",
    "flea",
    "tick prevention",
    "milbemax",
]


def _within_ceiling(invoice_amount: float, txn_amount: float) -> bool:
    return invoice_amount <= abs(txn_amount) + AMOUNT_TOLERANCE_FLAT


def _invoice_date_plausible(invoice: dict, txn_date: date) -> bool:
    """Guards against a real invoice for a DIFFERENT visit slipping through
    under the ceiling. The Gmail search window is intentionally wide once
    invoice_request_sent_at is reconciled (a reply can arrive months late),
    but the invoice's OWN date should still sit close to the actual
    transaction regardless of when the reply arrived — confirmed live: an
    open-ended window let one Shire Vet claim grab another Shire Vet claim's
    real invoice (correct vet, wrong visit) purely because it fit under the
    ceiling. A missing/unparseable invoice date can't be checked — allow
    through unchanged rather than reject on absence of evidence."""
    raw_date = invoice.get("date")
    if not raw_date:
        return True
    try:
        invoice_date = date.fromisoformat(raw_date[:10])
    except ValueError:
        return True
    return abs((invoice_date - txn_date).days) <= config.INVOICE_MATCH_WINDOW_DAYS


def _unexplained_remainder(invoice_amount: float, txn_amount: float) -> float | None:
    """Bank charge minus invoice total, when it exceeds a plausible card
    surcharge — a sign the charge covered another invoice too."""
    remainder = abs(txn_amount) - invoice_amount
    if remainder > abs(txn_amount) * SURCHARGE_MARGIN_PCT:
        return round(remainder, 2)
    return None


def claimable_amount(invoice: dict) -> float | None:
    """Sum of line items that aren't routine/preventive care. Falls back to
    the invoice total when no itemization is available (extraction gave no
    items); None only when neither exists."""
    items = invoice.get("items") or []
    if not items:
        return invoice.get("amount")
    claimable = 0.0
    for item in items:
        description = (item.get("description") or "").lower()
        if any(kw in description for kw in NON_CLAIMABLE_KEYWORDS):
            continue
        claimable += float(item.get("amount") or 0)
    return round(claimable, 2)


# One email can carry SEVERAL invoices (confirmed live: a vet's reply to a
# yearly bulk request listed three visits' invoices plus their grand total —
# single-invoice extraction returned the total and the ceiling rejected it for
# every claim). Extract every invoice; the matcher tests each one.
EXTRACTION_PROMPT = """Extract ALL invoices from this email as strict JSON:
{{"invoices": [{{"date": "<the visit/service date this invoice bills for — NOT the email, statement, \
issue or print date — ISO 8601, or null>", "amount": <this single invoice's total as number, or null>, \
"services": "<comma-separated itemized services, or null>", \
"items": [{{"description": "<line item>", "amount": <number>}}, ...]}}, ...]}}

One email may contain several invoices (e.g. a reply covering many visits) — return one entry per \
invoice with its own date and total. Never combine invoices: no grand totals, and two invoices on \
the SAME date (e.g. two pets seen the same day) stay two separate entries. "items" lists each \
charged line item with its own amount; use [] if the itemization is unreadable. Use \
{{"invoices": []}} if no invoice is present.

Email:
{email_text}
"""

INVOICE_REQUEST_SUBJECT = "Invoice request for recent visit"
INVOICE_REQUEST_BODY = (
    "Hi,\n\n"
    "Please could you send through the invoice for visit on {visit_date} for our dog "
    "{pet} {surname}. The amount was for {amount}.\n\n"
    "Many thanks in advance,\n\n"
    "{owner}"
)


def _salvage_truncated(raw: str, start: int):
    """A long bulk email can push the reply past the model's output budget,
    cutting the JSON mid-array (observed live on a 12k-char invoice PDF).
    Walk back to the last complete object and close the invoices array —
    only complete invoice objects survive, partial ones are dropped."""
    pos = len(raw)
    while True:
        pos = raw.rfind("}", start, pos)
        if pos <= start:
            return None
        try:
            return json.loads(raw[start : pos + 1] + "]}")
        except json.JSONDecodeError:
            continue


def _parse_invoices(raw: str) -> list | None:
    """Parses the extraction reply into a list of invoice dicts. Accepts the
    legacy single-invoice object too (cached rows / model regressions)."""
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        data = _salvage_truncated(raw, start)
        if data is None:
            return None
    if isinstance(data, dict) and isinstance(data.get("invoices"), list):
        return [inv for inv in data["invoices"] if isinstance(inv, dict)]
    if isinstance(data, dict) and "amount" in data:
        return [data]
    return None


def _extract_invoices(email_text: str) -> list | None:
    raw = llm.extract(EXTRACTION_PROMPT.format(email_text=email_text), purpose="invoice_extraction")
    return _parse_invoices(raw)


def _cached_extraction(message_id: str) -> list | None:
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT extracted_json FROM email_extractions WHERE message_id = ?", (message_id,)
        ).fetchone()
    return json.loads(row["extracted_json"]) if row else None


def _store_extraction(message_id: str, invoices: list) -> None:
    with db.get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO email_extractions (message_id, extracted_json, extracted_at) VALUES (?, ?, ?)",
            (message_id, json.dumps(invoices), datetime.now(timezone.utc).isoformat()),
        )


def _invoices_for_email(message_id: str, text: str) -> list | None:
    """One LLM extraction per email, ever — candidates get re-tested against
    claims every tick and across claims, so the parsed result is cached.
    A failed/unparseable extraction is NOT cached (retried next tick)."""
    cached = _cached_extraction(message_id)
    if cached is not None:
        return cached
    invoices = _extract_invoices(text)
    if invoices is not None:
        _store_extraction(message_id, invoices)
    return invoices


# --- Vision-OCR fallback for scanned (image-only) invoice PDFs -----------------
# Kings Vet emails photo scans: no text layer, so the text pipeline can't read
# them. Verified live: Gemini flash reads the scans accurately (invoice number,
# visit date, patient, itemised amounts). Hard attempt cap so a scan it can't
# parse doesn't burn tokens every tick forever.

VISION_MAX_ATTEMPTS = 3
VISION_PAGE_PROMPT = """This is one scanned page of a vet invoice PDF. Extract exactly this JSON:
{"invoice_number": "...", "date": "YYYY-MM-DD", "patient": "...", "amount": <invoice total as number>, \
"items": [{"description": "...", "amount": <number>}, ...]}
The date must be the visit/service date this invoice bills for — NOT the email, statement, issue or print date.
If the page is not an invoice (cover letter, statement, blank), return {"not_invoice": true}.
JSON only, no prose."""


def _vision_attempts_left(message_id: str) -> bool:
    """True while this email still has vision budget; consumes one attempt."""
    now = datetime.now(timezone.utc).isoformat()
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT attempts FROM vision_ocr_attempts WHERE message_id = ?", (message_id,)
        ).fetchone()
        if row and row["attempts"] >= VISION_MAX_ATTEMPTS:
            return False
        conn.execute(
            "INSERT INTO vision_ocr_attempts (message_id, attempts, last_attempt_at) VALUES (?, 1, ?) "
            "ON CONFLICT(message_id) DO UPDATE SET attempts = attempts + 1, last_attempt_at = excluded.last_attempt_at",
            (message_id, now),
        )
    return True


def _page_jpeg(image_bytes: bytes) -> bytes:
    """Scan pages embed one large photo each — downscale for the vision model."""
    import io

    from PIL import Image

    im = Image.open(io.BytesIO(image_bytes))
    if max(im.size) > 1600:
        im.thumbnail((1600, 1600))
    buf = io.BytesIO()
    im.convert("RGB").save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _vision_invoices(message_id: str) -> list | None:
    """Reads a scanned invoice bundle page-by-page with the vision model.
    Returns invoice dicts carrying source_pdf/page (so the claim's invoice
    pages can be sliced without a text layer), or None. Consumes one of the
    email's VISION_MAX_ATTEMPTS regardless of outcome; a success is cached in
    email_extractions so vision never re-runs for that email."""
    if not _vision_attempts_left(message_id):
        return None

    from io import BytesIO

    from pypdf import PdfReader

    from . import claim_forms

    invoices = []
    try:
        attachments = claim_forms.email_pdf_attachments(message_id)
        for filename, data in attachments:
            try:
                reader = PdfReader(BytesIO(data))
            except Exception:
                continue
            for page_no, page in enumerate(reader.pages):
                images = page.images
                if not images:
                    continue
                raw = llm.extract_vision(VISION_PAGE_PROMPT, _page_jpeg(images[0].data))
                parsed = _parse_invoices(raw)
                if not parsed:
                    continue  # not_invoice pages / unparseable replies carry no data
                for inv in parsed:
                    if inv.get("amount") is None:
                        continue
                    inv["source_pdf"] = filename
                    inv["page"] = page_no
                    invoices.append(inv)
    except llm.LLMUnavailableError:
        # provider outage, not an unreadable scan — refund the attempt so
        # 503 spikes can't exhaust the email's vision budget
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE vision_ocr_attempts SET attempts = attempts - 1 WHERE message_id = ? AND attempts > 0",
                (message_id,),
            )
        raise
    if invoices:
        _store_extraction(message_id, invoices)
        return invoices
    return None


# NetBank descriptors append a trailing city/state (e.g. "...CARINGBAH NSW"),
# which never appears verbatim in a real invoice email — quoting the full
# descriptor as an exact phrase was confirmed live to suppress real matches.
AU_STATES = {"ACT", "NSW", "NT", "QLD", "SA", "TAS", "VIC", "WA"}


def _search_terms(merchant: str) -> str:
    words = merchant.split()
    if words and words[-1].upper() in AU_STATES:
        words = words[:-1]
    return " ".join(words)


def _build_queries(merchant: str, txn_date: date) -> list[tuple[str, bool]]:
    """Each query pairs with whether a candidate it returns still needs
    content-level vet confirmation (see _forward_confirms_vet) before being
    trusted — the spouse fallback has no merchant term in the query itself,
    so it needs that extra gate; the merchant query already searched on the
    vet's name.

    Two arrival windows per source: the narrow txn±window one (also covers an
    invoice emailed a couple of days BEFORE the charge settled), and an
    unconditional open-ended one — invoices and forwards arrive months after
    the visit (confirmed live: February invoices forwarded in July). Arrival
    date is only a search hint; eligibility is _invoice_date_plausible."""
    after = txn_date - timedelta(days=config.INVOICE_MATCH_WINDOW_DAYS)
    before = txn_date + timedelta(days=config.INVOICE_MATCH_WINDOW_DAYS)
    narrow = f"after:{after.isoformat()} before:{before.isoformat()}"
    wide = f"after:{txn_date.isoformat()}"
    terms = _search_terms(merchant)
    # -from:me — Justin's OWN outgoing invoice-request emails list visit dates
    # and charge amounts, which extraction reads as invoices with exact
    # amount+date fits (confirmed live: 12 claims matched his own requests the
    # moment the wide window surfaced them). Own mail is never an invoice.
    queries = [(f"{terms} {narrow} -from:me", False), (f"{terms} {wide} -from:me", False)]
    if config.SPOUSE_EMAIL:
        # Invoices sometimes get forwarded from a spouse's address instead of
        # arriving from the vet directly — no merchant terms in the QUERY
        # itself since a forward's subject/body rarely repeats it verbatim as
        # an exact phrase (Gmail query-side phrase matching is brittle).
        # Confirmed instead against the fetched message body below.
        queries.append((f"from:{config.SPOUSE_EMAIL} {narrow}", True))
        queries.append((f"from:{config.SPOUSE_EMAIL} {wide}", True))
    return queries


# Merchant words too generic to identify a vet in forwarded content — matching
# on these let a human-hospital forward pass as a vet invoice (confirmed live).
GENERIC_MERCHANT_WORDS = {"veterinary", "animal", "hospital", "clinic", "sydney"}


def _forward_confirms_vet(text: str, merchant: str, known_vet_email: str | None) -> bool:
    """A forwarded invoice's quoted content usually still names the vet or
    shows their address in the quoted 'From:' line — require one of those to
    actually appear before trusting a spouse-forward match. Without this, the
    open-ended arrival window can match ANY forwarded invoice from the spouse,
    wrong vet included — confirmed live: two claims for two different vets
    both matched the same unrelated forward purely because it was under the
    ceiling. Distinctive words only (≥5 chars, non-generic) and whole-word
    matches only — substring matching let "Kings" (of "Kings Vet") fire inside
    an unrelated human-medical forward (confirmed live)."""
    lowered = text.lower()
    if known_vet_email and known_vet_email.lower() in lowered:
        return True
    words = [
        w.lower()
        for w in _search_terms(merchant).split()
        if len(w) >= 5 and w.lower() not in GENERIC_MERCHANT_WORDS
    ]
    return any(re.search(rf"\b{re.escape(w)}\b", lowered) for w in words)


def _single_pet_in_text(text: str) -> int | None:
    """The pet id when the email names exactly ONE known pet (word-boundary,
    case-insensitive) — e.g. a receipt itemising 'Echo 17 Jun 2026
    Consultation'. Two names (a bulk reply covering both dogs) = no signal.
    Reading a printed fact off the vet's own document, not guessing."""
    with db.get_connection() as conn:
        pets = conn.execute("SELECT id, name FROM pets").fetchall()
    named = [p for p in pets if re.search(rf"\b{re.escape(p['name'])}\b", text, re.IGNORECASE)]
    return named[0]["id"] if len(named) == 1 else None


def _pet_id_by_name(patient: str | None) -> int | None:
    """Pet id when the extracted patient field IS a known pet's name."""
    if not patient:
        return None
    with db.get_connection() as conn:
        row = conn.execute("SELECT id FROM pets WHERE name = ? COLLATE NOCASE", (patient.strip(),)).fetchone()
    return row["id"] if row else None


def _mark_matched(claim_id: int, email_id: str, invoice: dict, flag: str | None = None,
                  pet_id: int | None = None) -> None:
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE vet_claims SET status = 'matched', matched_email_id = ?, invoice_data = ?, "
            "flag = ?, pet_id = COALESCE(?, pet_id), updated_at = ? WHERE id = ?",
            (email_id, json.dumps(invoice), flag, pet_id, datetime.now(timezone.utc).isoformat(), claim_id),
        )


def _pick_invoice(invoices: list, txn_amount: float, txn_date: date) -> dict | None:
    """First invoice in the email that fits under the charge ceiling AND whose
    own date sits near the transaction — a bulk reply's other invoices belong
    to other claims."""
    for invoice in invoices:
        if invoice.get("amount") is None:
            continue
        total = float(invoice["amount"])
        if not _within_ceiling(total, txn_amount):
            continue
        if not _invoice_date_plausible(invoice, txn_date):
            continue
        return invoice
    return None


def _oversized_candidate(invoices: list, txn_amount: float, txn_date: date) -> dict | None:
    """An invoice whose date matches the visit but whose total EXCEEDS this
    charge — the vet billed one invoice paid via several card charges
    (confirmed live: one $2,521.46 invoice = a $551.06 + a $1,970.40 charge on
    the same day). Can't be matched without guessing how to split it; surfaced
    as a flag for Justin instead."""
    for invoice in invoices:
        if invoice.get("amount") is None:
            continue
        total = float(invoice["amount"])
        if _invoice_date_plausible(invoice, txn_date) and invoice.get("date") and not _within_ceiling(total, txn_amount):
            return invoice
    return None


_AMOUNT_RE = re.compile(r"\$\s?\d[\d,]*\.\d{2}")


def _has_pdf_attachment(message: dict) -> bool:
    return any(
        part.get("mimeType") == "application/pdf"
        for part in gmail_client._iter_attachment_parts(message.get("payload", {}))
    )


def _flag_claim(claim_id: int, flag: str | None) -> None:
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE vet_claims SET flag = ?, updated_at = ? WHERE id = ?",
            (flag, datetime.now(timezone.utc).isoformat(), claim_id),
        )


def match_claim(claim) -> bool:
    """Searches Gmail for an invoice matching claim's transaction (merchant name,
    then spouse's address if configured, as a fallback). The bank charge is the
    ceiling: an invoice matches when its total is at most the charged amount.
    An email may hold several invoices; each is tested individually. Returns
    True and advances the claim to 'matched' on success. Raises
    llm.LLMUnavailableError when extraction is down (caller isolates it)."""
    txn_date = date.fromisoformat(claim["txn_date"])
    queries = _build_queries(claim["txn_merchant"], txn_date)

    rejected = set(json.loads(claim["rejected_email_ids"]) if claim["rejected_email_ids"] else [])
    known_vet_email = None
    vet_email_looked_up = False
    seen: set[str] = set()
    unreadable_subject = None
    oversized = None

    service = gmail_client.build_service()
    for query, needs_vet_confirmation in queries:
        response = service.users().messages().list(userId="me", q=query, maxResults=5).execute()
        for item in response.get("messages", []):
            if item["id"] in seen:
                continue  # narrow and wide windows overlap
            seen.add(item["id"])
            if item["id"] in rejected:
                continue  # Justin unmatched this invoice — don't re-grab it
            message = service.users().messages().get(userId="me", id=item["id"], format="full").execute()
            if "SENT" in message.get("labelIds", []):
                continue  # own outgoing mail is never an invoice (second layer past -from:me)
            text = gmail_client.full_message_text(service, message)

            if needs_vet_confirmation:
                if not vet_email_looked_up:
                    known_vet_email = _lookup_vet_email(claim["txn_merchant"])
                    vet_email_looked_up = True
                if not _forward_confirms_vet(text, claim["txn_merchant"], known_vet_email):
                    continue

            invoices = _invoices_for_email(item["id"], text)
            if not invoices:
                # A vet-addressed candidate whose PDF gave us nothing readable
                # (scanned/image PDF, no text layer) — try the vision-OCR
                # fallback; if the model can't read it either (attempt-capped),
                # surface it so Justin asks the vet for a text copy.
                if (
                    not needs_vet_confirmation
                    and _has_pdf_attachment(message)
                    and not _AMOUNT_RE.search(text)
                ):
                    invoices = _vision_invoices(item["id"])
                    if not invoices:
                        headers = {h["name"]: h["value"] for h in message.get("payload", {}).get("headers", [])}
                        unreadable_subject = headers.get("Subject", "(no subject)")
            if not invoices:
                continue

            invoice = _pick_invoice(invoices, claim["txn_amount"], txn_date)
            if invoice is None:
                if oversized is None:
                    candidate = _oversized_candidate(invoices, claim["txn_amount"], txn_date)
                    if candidate:
                        # keep the amounts the email/PDF text mentions — the
                        # invoice's own payment lines listing both bank charges
                        # is the strongest merge evidence (see _propose_split)
                        oversized = {**candidate, "_email_id": item["id"], "_text_amounts": _text_amounts(text)}
                continue
            total = float(invoice["amount"])
            invoice["claimable_amount"] = claimable_amount(invoice)
            remainder = _unexplained_remainder(total, claim["txn_amount"])
            flag = f"possible additional invoice — unexplained ${remainder:.2f}" if remainder else None
            pet_id = None
            if claim["pet_id"] is None:
                # scanned invoices have no email text — the vision extraction's
                # patient field is the printed fact instead
                pet_id = _pet_id_by_name(invoice.get("patient")) or _single_pet_in_text(text)
            _mark_matched(claim["id"], item["id"], invoice, flag, pet_id=pet_id)
            return True

    if unreadable_subject:
        flag = f"invoice attachment unreadable — {unreadable_subject}"
    elif oversized:
        flag = _propose_split(claim, oversized) or (
            f"invoice dated {oversized['date']} totals ${float(oversized['amount']):.2f} — exceeds this "
            f"charge; likely one invoice paid over several charges, split/confirm manually"
        )
    else:
        return False
    if claim["flag"] != flag:
        _flag_claim(claim["id"], flag)
    return False


_TEXT_AMOUNT_RE = re.compile(r"-?\$?\s?\d[\d,]*\.\d{2}\b")


def _text_amounts(text: str) -> list[float]:
    """Every money-looking number in the email/PDF text, as positive floats —
    an invoice's payment section lists each card payment (e.g. ': -1970.40')."""
    return [abs(float(m.replace("$", "").replace(",", "").strip())) for m in _TEXT_AMOUNT_RE.findall(text)]


def _propose_split(claim, oversized: dict) -> str | None:
    """One invoice paid over several charges (confirmed live: MediPaws invoice
    #411193, $2,521.46 for Aari, its own payment section listing the two card
    payments -1970.40 and -551.06). If this claim plus ONE other pending claim
    at the same vet sum to the oversized invoice's total (ceiling tolerance),
    record a merge proposal. WHICH claim carries the invoice doesn't matter —
    Petcover sees the invoice, never the bank charges — so nothing asks Justin
    to pick: the larger charge carries it, and Telegram asks only to CONFIRM
    the merge (with a reject escape hatch). Returns the flag text, or None
    when no sibling explains the total."""
    total = float(oversized["amount"])
    email_id = oversized.get("_email_id", "")
    with db.get_connection() as conn:
        siblings = conn.execute(
            "SELECT vet_claims.id, bank_transactions.amount FROM vet_claims "
            "JOIN bank_transactions ON bank_transactions.id = vet_claims.transaction_id "
            "WHERE vet_claims.status = 'pending_match' AND vet_claims.id != ? "
            "AND bank_transactions.merchant = ?",
            (claim["id"], claim["txn_merchant"]),
        ).fetchall()
    match = next(
        (s for s in siblings if _within_ceiling(total, -(abs(claim["txn_amount"]) + abs(s["amount"])))),
        None,
    )
    if match is None:
        return None
    claim_ids = sorted([claim["id"], match["id"]])
    # The strongest evidence: the invoice's own payment records list BOTH bank
    # charge amounts. Recorded on the proposal so the Telegram message can say so.
    text_amounts = oversized.get("_text_amounts") or []
    payments_confirmed = all(
        any(abs(abs(amt) - candidate) < 0.005 for candidate in text_amounts)
        for amt in (claim["txn_amount"], match["amount"])
    )
    now = datetime.now(timezone.utc).isoformat()
    with db.get_connection() as conn:
        # any prior proposal for this pair blocks a new one — and a REJECTED
        # merge must not come back every tick, nor re-claim the flag text
        existing = conn.execute(
            "SELECT status FROM split_proposals WHERE claim_ids = ? ORDER BY id DESC LIMIT 1",
            (json.dumps(claim_ids),),
        ).fetchone()
        if existing and existing["status"] != "open":
            return None  # Justin already decided (rejected) or it's history — keep the manual flag
        if not existing:
            invoice = {k: v for k, v in oversized.items() if not k.startswith("_")}
            invoice["payments_confirmed"] = payments_confirmed
            conn.execute(
                "INSERT INTO split_proposals (email_id, invoice_json, claim_ids, created_at) VALUES (?, ?, ?, ?)",
                (email_id, json.dumps(invoice), json.dumps(claim_ids), now),
            )
    other = match["id"]
    return (
        f"invoice dated {oversized['date']} totals ${total:.2f} — one invoice paid by this charge and "
        f"claim #{other}'s together; confirm the merge on Telegram"
    )


def merge_split_proposal(proposal_id: int) -> dict:
    """Justin confirmed the merge: the larger charge's claim carries the
    invoice (deterministic — the choice is bookkeeping, not Petcover-facing).
    Shared by the Telegram ✅ Merge button."""
    with db.get_connection() as conn:
        proposal = conn.execute(
            "SELECT * FROM split_proposals WHERE id = ? AND status = 'open'", (proposal_id,)
        ).fetchone()
        if proposal is None:
            return {"ok": False, "message": "That merge proposal is gone or already resolved."}
        claim_ids = json.loads(proposal["claim_ids"])
        rows = conn.execute(
            f"SELECT vet_claims.id, bank_transactions.amount FROM vet_claims "
            f"JOIN bank_transactions ON bank_transactions.id = vet_claims.transaction_id "
            f"WHERE vet_claims.id IN ({','.join('?' * len(claim_ids))})",
            claim_ids,
        ).fetchall()
    primary = max(rows, key=lambda r: (abs(r["amount"]), -r["id"]))["id"]
    return resolve_split_proposal(proposal_id, primary)


def reject_split_proposal(proposal_id: int) -> dict:
    """Justin said the charges are NOT payments of this one invoice: close the
    proposal (never re-proposed — see the any-status dedupe in _propose_split)
    and flag both claims for manual matching."""
    now = datetime.now(timezone.utc).isoformat()
    with db.get_connection() as conn:
        proposal = conn.execute(
            "SELECT * FROM split_proposals WHERE id = ? AND status = 'open'", (proposal_id,)
        ).fetchone()
        if proposal is None:
            return {"ok": False, "message": "That merge proposal is gone or already resolved."}
        invoice = json.loads(proposal["invoice_json"])
        conn.execute("UPDATE split_proposals SET status = 'rejected' WHERE id = ?", (proposal_id,))
        for claim_id in json.loads(proposal["claim_ids"]):
            conn.execute(
                "UPDATE vet_claims SET flag = ?, updated_at = ? WHERE id = ? AND status = 'pending_match'",
                (
                    f"merge of ${float(invoice['amount']):.2f} invoice rejected — match this charge manually",
                    now,
                    claim_id,
                ),
            )
    return {"ok": True, "message": "Merge rejected — both claims flagged for manual matching."}


def resolve_split_proposal(proposal_id: int, chosen_claim_id: int) -> dict:
    """Attaches a multi-charge invoice to one claim: that claim is matched
    (ceiling = the charges together, validated); the other claim is closed as
    'absorbed' — same money, one claim. Called by merge_split_proposal (auto
    primary) and the legacy per-claim pick buttons."""
    now = datetime.now(timezone.utc).isoformat()
    with db.get_connection() as conn:
        proposal = conn.execute(
            "SELECT * FROM split_proposals WHERE id = ? AND status = 'open'", (proposal_id,)
        ).fetchone()
        if proposal is None:
            return {"ok": False, "message": "That split proposal is gone or already resolved."}
        claim_ids = json.loads(proposal["claim_ids"])
        if chosen_claim_id not in claim_ids:
            return {"ok": False, "message": f"Claim #{chosen_claim_id} isn't part of this proposal."}
        rows = conn.execute(
            f"SELECT vet_claims.id, vet_claims.status, bank_transactions.amount FROM vet_claims "
            f"JOIN bank_transactions ON bank_transactions.id = vet_claims.transaction_id "
            f"WHERE vet_claims.id IN ({','.join('?' * len(claim_ids))})",
            claim_ids,
        ).fetchall()
    if any(r["status"] != "pending_match" for r in rows):
        return {"ok": False, "message": "A claim in this proposal already moved on — nothing changed."}
    invoice = json.loads(proposal["invoice_json"])
    total = float(invoice["amount"])
    combined = sum(abs(r["amount"]) for r in rows)
    if not _within_ceiling(total, -combined):
        return {"ok": False, "message": f"Invoice ${total:.2f} exceeds the charges combined (${combined:.2f}) — refusing."}

    invoice.pop("payments_confirmed", None)
    invoice["claimable_amount"] = claimable_amount(invoice)
    others = [c for c in claim_ids if c != chosen_claim_id]
    _mark_matched(
        chosen_claim_id,
        proposal["email_id"],
        invoice,
        flag=f"one ${total:.2f} invoice paid via charges of claims #{chosen_claim_id} + #{', #'.join(map(str, others))} — merged here",
    )
    with db.get_connection() as conn:
        for other in others:
            conn.execute(
                "UPDATE vet_claims SET status = 'absorbed', "
                "flag = ?, updated_at = ? WHERE id = ?",
                (f"second payment of claim #{chosen_claim_id}'s ${total:.2f} invoice", now, other),
            )
        conn.execute("UPDATE split_proposals SET status = 'resolved' WHERE id = ?", (proposal_id,))
    return {
        "ok": True,
        "message": f"Merged: claim #{chosen_claim_id} carries the ${total:.2f} invoice; "
        f"claim{'s' if len(others) > 1 else ''} #{', #'.join(map(str, others))} closed as its other payment.",
    }


def unmatch(claim_id: int) -> dict:
    """Rejects a wrong invoice match: remembers the rejected email so the
    matcher won't re-grab it, then resets the claim to 'pending_match' so the
    next pipeline run searches Gmail again. Shared by the Telegram button."""
    now = datetime.now(timezone.utc).isoformat()
    with db.get_connection() as conn:
        claim = conn.execute("SELECT * FROM vet_claims WHERE id = ?", (claim_id,)).fetchone()
        if claim is None:
            return {"ok": False, "message": f"No claim #{claim_id} found."}
        if not claim["matched_email_id"]:
            return {"ok": False, "message": f"Claim #{claim_id} has no matched invoice to reject."}
        rejected = json.loads(claim["rejected_email_ids"]) if claim["rejected_email_ids"] else []
        if claim["matched_email_id"] not in rejected:
            rejected.append(claim["matched_email_id"])
        conn.execute(
            "UPDATE vet_claims SET status = 'pending_match', matched_email_id = NULL, invoice_data = NULL, "
            "invoice_file_path = NULL, flag = NULL, rejected_email_ids = ?, "
            "telegram_notified_status = NULL, telegram_notified_flag = NULL, updated_at = ? WHERE id = ?",
            (json.dumps(rejected), now, claim_id),
        )
    return {"ok": True, "message": f"Claim #{claim_id}: wrong invoice rejected — re-searching Gmail for the right one."}


def _lookup_vet_email(merchant: str) -> str | None:
    """Looks up the vet's contact address: a manually-supplied override first
    (vet_contacts — bank CSVs carry no contact info, and a matched invoice's
    From header is often a forwarder, not the vet, see matched-email fallback
    below), else the From header of a previously matched invoice email."""
    with db.get_connection() as conn:
        override = conn.execute(
            "SELECT email FROM vet_contacts WHERE merchant = ?", (merchant,)
        ).fetchone()
    if override:
        return override["email"]

    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT vet_claims.matched_email_id FROM vet_claims "
            "JOIN bank_transactions ON bank_transactions.id = vet_claims.transaction_id "
            "WHERE bank_transactions.merchant = ? AND vet_claims.matched_email_id IS NOT NULL "
            "ORDER BY vet_claims.updated_at DESC LIMIT 1",
            (merchant,),
        ).fetchone()
    if not row:
        return None

    service = gmail_client.build_service()
    message = service.users().messages().get(
        userId="me", id=row["matched_email_id"], format="metadata", metadataHeaders=["From"]
    ).execute()
    headers = {h["name"]: h["value"] for h in message.get("payload", {}).get("headers", [])}
    return headers.get("From")


def draft_invoice_request(claim) -> str | None:
    """Drafts (never sends) an email asking the vet for the invoice. Returns the
    draft's message id, or None if no vet email is on file yet."""
    to = _lookup_vet_email(claim["txn_merchant"])
    if not to:
        return None

    owner = config.OWNER_NAME or "Justin Goldberg"
    surname = owner.split()[-1]
    if claim["pet_id"]:
        with db.get_connection() as conn:
            pet = conn.execute("SELECT name FROM pets WHERE id = ?", (claim["pet_id"],)).fetchone()
        pet_name = pet["name"] if pet else "Aari or Echo"
    else:
        pet_name = "Aari or Echo"
    visit_date = date.fromisoformat(claim["txn_date"]).strftime("%d-%b-%Y")
    body = INVOICE_REQUEST_BODY.format(
        visit_date=visit_date,
        pet=pet_name,
        surname=surname,
        amount=f"${abs(claim['txn_amount']):.2f}",
        owner=owner,
    )
    raw = base64.urlsafe_b64encode(
        f"To: {to}\r\nSubject: {INVOICE_REQUEST_SUBJECT}\r\n\r\n{body}".encode()
    ).decode()

    service = gmail_client.build_service()
    draft = service.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
    return draft["message"]["id"]
