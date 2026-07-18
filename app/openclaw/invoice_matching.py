import base64
import json
from datetime import date, datetime, timedelta, timezone

from . import config, db, gemini, gmail_client

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


EXTRACTION_PROMPT = """Extract invoice details from this email as strict JSON:
{{"date": "<ISO 8601 date, or null>", "amount": <total as number, or null>, "services": "<comma-separated \
itemized services, or null>", "items": [{{"description": "<line item>", "amount": <number>}}, ...]}}

"items" lists each charged line item with its own amount; use [] if the itemization is unreadable.

Email:
{email_text}
"""

INVOICE_REQUEST_SUBJECT = "Invoice request for recent visit"
INVOICE_REQUEST_BODY = (
    "Hi,\n\nCould you please send through the invoice for our recent visit "
    "(transaction on {txn_date} for {amount})?\n\nThanks."
)


def _extract_invoice(email_text: str) -> dict | None:
    raw = gemini.extract(EXTRACTION_PROMPT.format(email_text=email_text), purpose="invoice_extraction")
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
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


def _date_range_clause(txn_date: date, invoice_request_sent_at: str | None) -> str:
    if invoice_request_sent_at:
        # rolling recheck: search from the original transaction date through to now,
        # not a fixed window, so a late reply after the request is still picked up.
        return f"after:{txn_date.isoformat()}"
    after = txn_date - timedelta(days=config.INVOICE_MATCH_WINDOW_DAYS)
    before = txn_date + timedelta(days=config.INVOICE_MATCH_WINDOW_DAYS)
    return f"after:{after.isoformat()} before:{before.isoformat()}"


def _build_queries(merchant: str, txn_date: date, invoice_request_sent_at: str | None) -> list[str]:
    date_clause = _date_range_clause(txn_date, invoice_request_sent_at)
    queries = [f"{_search_terms(merchant)} {date_clause}"]
    if config.SPOUSE_EMAIL:
        # Invoices sometimes get forwarded from a spouse's address instead of
        # arriving from the vet directly — same date window, no merchant terms
        # required since a forward's subject/body rarely repeats it verbatim.
        queries.append(f"from:{config.SPOUSE_EMAIL} {date_clause}")
    return queries


def _mark_matched(claim_id: int, email_id: str, invoice: dict, flag: str | None = None) -> None:
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE vet_claims SET status = 'matched', matched_email_id = ?, invoice_data = ?, "
            "flag = ?, updated_at = ? WHERE id = ?",
            (email_id, json.dumps(invoice), flag, datetime.now(timezone.utc).isoformat(), claim_id),
        )


def match_claim(claim) -> bool:
    """Searches Gmail for an invoice matching claim's transaction (merchant name,
    then spouse's address if configured, as a fallback). The bank charge is the
    ceiling: an invoice matches when its total is at most the charged amount.
    Returns True and advances the claim to 'matched' on success."""
    txn_date = date.fromisoformat(claim["txn_date"])
    queries = _build_queries(claim["txn_merchant"], txn_date, claim["invoice_request_sent_at"])

    rejected = set(json.loads(claim["rejected_email_ids"]) if claim["rejected_email_ids"] else [])

    service = gmail_client.build_service()
    for query in queries:
        response = service.users().messages().list(userId="me", q=query, maxResults=5).execute()
        for item in response.get("messages", []):
            if item["id"] in rejected:
                continue  # Justin unmatched this invoice — don't re-grab it
            message = service.users().messages().get(userId="me", id=item["id"], format="full").execute()
            invoice = _extract_invoice(gmail_client.full_message_text(service, message))
            if not invoice or invoice.get("amount") is None:
                continue
            total = float(invoice["amount"])
            if not _within_ceiling(total, claim["txn_amount"]):
                continue
            invoice["claimable_amount"] = claimable_amount(invoice)
            remainder = _unexplained_remainder(total, claim["txn_amount"])
            flag = f"possible additional invoice — unexplained ${remainder:.2f}" if remainder else None
            _mark_matched(claim["id"], item["id"], invoice, flag)
            return True
    return False


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

    body = INVOICE_REQUEST_BODY.format(txn_date=claim["txn_date"], amount=abs(claim["txn_amount"]))
    raw = base64.urlsafe_b64encode(
        f"To: {to}\r\nSubject: {INVOICE_REQUEST_SUBJECT}\r\n\r\n{body}".encode()
    ).decode()

    service = gmail_client.build_service()
    draft = service.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
    return draft["message"]["id"]
