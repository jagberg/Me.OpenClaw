import base64
import json
from datetime import date, datetime, timedelta, timezone
from io import BytesIO

from pypdf import PdfReader

from . import config, db, gemini, gmail_client

# Flat cent tolerance covers float rounding only. Card-surcharge fees mean the
# bank-charged amount often legitimately differs from the vet's invoice total
# by ~0.5-1.5% (confirmed live: a real $580.74 invoice showed as $585.39 on
# the statement, a 0.8% surcharge) — so tolerance scales with the amount too.
AMOUNT_TOLERANCE_FLAT = 0.01
AMOUNT_TOLERANCE_PCT = 0.03


def _within_tolerance(invoice_amount: float, txn_amount: float) -> bool:
    tolerance = max(AMOUNT_TOLERANCE_FLAT, abs(txn_amount) * AMOUNT_TOLERANCE_PCT)
    return abs(invoice_amount - abs(txn_amount)) <= tolerance

EXTRACTION_PROMPT = """Extract invoice details from this email as strict JSON:
{{"date": "<ISO 8601 date, or null>", "amount": <number, or null>, "services": "<comma-separated \
itemized services, or null>"}}

Email:
{email_text}
"""

INVOICE_REQUEST_SUBJECT = "Invoice request for recent visit"
INVOICE_REQUEST_BODY = (
    "Hi,\n\nCould you please send through the invoice for our recent visit "
    "(transaction on {txn_date} for {amount})?\n\nThanks."
)


def _decode_part(data: str) -> str:
    return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")


def _message_text(message: dict) -> str:
    """Best-effort plain-text body extraction; falls back to the snippet if no
    text/plain part is found (sufficient for Gemini extraction either way)."""
    payload = message.get("payload", {})
    parts = payload.get("parts") or [payload]
    for part in parts:
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return _decode_part(part["body"]["data"])
    return message.get("snippet", "")


def _iter_attachment_parts(payload: dict):
    for part in payload.get("parts") or []:
        if part.get("filename") and part.get("body", {}).get("attachmentId"):
            yield part
        if part.get("parts"):
            yield from _iter_attachment_parts(part)


def _pdf_attachment_text(service, message_id: str, attachment_id: str) -> str:
    attachment = service.users().messages().attachments().get(
        userId="me", messageId=message_id, id=attachment_id
    ).execute()
    data = base64.urlsafe_b64decode(attachment["data"] + "==")
    reader = PdfReader(BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _full_message_text(service, message: dict) -> str:
    """Body text plus any PDF attachment text — receipts are frequently
    forwarded with the actual amount only in an attached PDF, none in the
    body. Image attachments (PNG/JPG) are skipped: no OCR support."""
    text = _message_text(message)
    for part in _iter_attachment_parts(message.get("payload", {})):
        if part.get("mimeType") != "application/pdf":
            continue
        try:
            text += "\n" + _pdf_attachment_text(service, message["id"], part["body"]["attachmentId"])
        except Exception:
            continue  # unreadable attachment — fall back to whatever text we already have
    return text


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


def _mark_matched(claim_id: int, email_id: str, invoice: dict) -> None:
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE vet_claims SET status = 'matched', matched_email_id = ?, invoice_data = ?, "
            "flag = NULL, updated_at = ? WHERE id = ?",
            (email_id, json.dumps(invoice), datetime.now(timezone.utc).isoformat(), claim_id),
        )


def match_claim(claim) -> bool:
    """Searches Gmail for an invoice matching claim's transaction (merchant name,
    then spouse's address if configured, as a fallback). Returns True and advances
    the claim to 'matched' if found within amount tolerance."""
    txn_date = date.fromisoformat(claim["txn_date"])
    queries = _build_queries(claim["txn_merchant"], txn_date, claim["invoice_request_sent_at"])

    service = gmail_client.build_service()
    for query in queries:
        response = service.users().messages().list(userId="me", q=query, maxResults=5).execute()
        for item in response.get("messages", []):
            message = service.users().messages().get(userId="me", id=item["id"], format="full").execute()
            invoice = _extract_invoice(_full_message_text(service, message))
            if not invoice or invoice.get("amount") is None:
                continue
            if _within_tolerance(float(invoice["amount"]), claim["txn_amount"]):
                _mark_matched(claim["id"], item["id"], invoice)
                return True
    return False


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
