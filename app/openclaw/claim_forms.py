import base64
import json
import re
from datetime import datetime, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from . import config, db, gmail_client

# Shared by the single-claim and batch draft guards: Petcover requires the
# itemised invoice attached, so a claim without one on file isn't draftable yet.
_AWAITING_INVOICE_FLAG = "awaiting itemised invoice from vet — not drafting until it can be attached"

# Field map for Petcover's real fillable AcroForm (Petcover-AU-Claim-Vet-EN-V20211201),
# verified against the actual file: field names are generic ("Text Field 90")
# so this maps them to logical keys by their on-page position, cross-checked
# against the form's printed labels and, for the radio buttons/checkboxes
# below, each widget's on-page /Rect position relative to the question text.
# Bank details, other-insurer/continuation answers, and the declaration
# tick+date are filled with Justin's explicit values (confirmed 2026-07) —
# previously left blank as "Justin should tick this himself"; he has since
# directly supplied these and asked for them to be filled automatically.
FIELD_MAP = {
    "Text Field 90": "policy_number",
    "Text Field 92": "owner_name",
    "Text Field 91": "owner_phone",
    "Text Field 93": "owner_email",
    "Text Field 117": "owner_address",
    "Text Field 118": "owner_postcode",
    "Combo Box 5": "owner_state",
    "Text Field 94": "pet_name",
    "Text Field 95": "pet_dob",
    "Text Field 98": "condition_1",
    "Text Field 99": "treatment_date_1",
    "Text Field 100": "first_signs_date_1",
    "Text Field 101": "charge_1",
    # Rows 2-4: the form holds up to 4 invoice line items per claim document
    # (confirmed against a real past submission with 3 rows filled).
    "Text Field 102": "condition_2",
    "Text Field 103": "treatment_date_2",
    "Text Field 104": "first_signs_date_2",
    "Text Field 105": "charge_2",
    "Text Field 106": "condition_3",
    "Text Field 107": "treatment_date_3",
    "Text Field 108": "first_signs_date_3",
    "Text Field 109": "charge_3",
    "Text Field 110": "condition_4",
    "Text Field 111": "treatment_date_4",
    "Text Field 112": "first_signs_date_4",
    "Text Field 113": "charge_4",
    # Radio Button 3 sits directly under "Is this pet insured with any other
    # company?" (states left-to-right: /0=Yes, /1=No, matching "Yes No" text order).
    "Radio Button 3": "other_insurer_state",
    # Radio Button 2 sits under "Is this claim a continuation of a previous
    # claim?" (same /0=Yes, /1=No ordering).
    "Radio Button 2": "claim_continuation_state",
    "Check Box 21": "pay_bank_account",
    "Text Field 155": "bank_account_name",
    "Text Field 156": "bank_bsb",
    "Text Field 157": "bank_account_number",
    "Check Box 23": "declaration_ack",
    "Text Field 97": "declaration_date",
}

# NON_CLAIMABLE_KEYWORDS lives in invoice_matching (applied at extraction time,
# stored as invoice_data.claimable_amount) — the claim form just reads it.


class ClaimFillError(Exception):
    """Raised when an expected field name is missing from the template — refuses
    to guess which field is which rather than filling the wrong data in."""


def fill_petcover_form(data: dict, output_path: str) -> None:
    reader = PdfReader(config.PETCOVER_TEMPLATE_PATH)
    available = reader.get_fields() or {}
    missing = [name for name in FIELD_MAP if name not in available]
    if missing:
        raise ClaimFillError(
            f"Petcover template is missing expected field(s) {missing} — "
            "template layout changed, refusing to fill blind."
        )

    # Fields with no value are left untouched (template default) rather than
    # forced to "" — required for checkboxes/radios, where an empty string
    # isn't a valid on-state and would just be ignored by pypdf anyway.
    values = {name: str(data[key]) for name, key in FIELD_MAP.items() if data.get(key) is not None}
    writer = PdfWriter()
    writer.append(reader)
    for page in writer.pages:
        # auto_regenerate defaults to True, which sets /NeedAppearances on the
        # AcroForm — tells viewers (confirmed: Adobe too, not just Gmail/Chrome)
        # to ignore the template's pre-drawn appearance streams and synthesize
        # their own, which renders radio buttons blank regardless of /AS. The
        # template's real streams (confirmed: state "/0" draws a filled dot,
        # "/Off" draws just the outline) are correct — just needed left alone.
        writer.update_page_form_field_values(page, values, auto_regenerate=False)

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as f:
        writer.write(f)


def _build_mime_message(to: str, subject: str, body: str, attachment_paths: list[str]) -> dict:
    msg = MIMEMultipart()
    msg["to"] = to
    msg["subject"] = subject
    msg.attach(MIMEText(body))

    for attachment_path in attachment_paths:
        with open(attachment_path, "rb") as f:
            part = MIMEApplication(f.read(), Name=Path(attachment_path).name)
        part["Content-Disposition"] = f'attachment; filename="{Path(attachment_path).name}"'
        msg.attach(part)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return {"raw": raw}


def create_claim_draft(to: str, subject: str, body: str, attachment_paths: list[str]) -> str:
    """Creates a Gmail draft with the filled claim form + source invoice(s)
    attached — draft only, never sends. Petcover's own instructions require
    the itemised invoice(s) attached, not just the completed form. Returns the
    draft's message id (used for the dashboard link)."""
    service = gmail_client.build_service()
    message = _build_mime_message(to, subject, body, attachment_paths)
    draft = service.users().drafts().create(userId="me", body={"message": message}).execute()
    return draft["message"]["id"]


def _flag(claim_id: int, message: str) -> None:
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE vet_claims SET flag = ?, updated_at = ? WHERE id = ?",
            (message, datetime.now(timezone.utc).isoformat(), claim_id),
        )


def _shared_fields(pet, continuation: bool | None) -> dict:
    """Fields that are the same across every claim for a pet (owner/pet/bank
    details, declaration) rather than per-invoice. `continuation` is a
    per-claim judgment call (is this the same ongoing condition as a prior
    submission?) so it's passed in per call, not stored on the pet."""
    fields = {
        "policy_number": pet["policy_number"],
        "owner_name": config.OWNER_NAME,
        "owner_phone": config.OWNER_PHONE,
        "owner_email": config.OWNER_EMAIL,
        "owner_address": config.OWNER_ADDRESS,
        "owner_postcode": config.OWNER_POSTCODE,
        "owner_state": config.OWNER_STATE,
        "pet_name": pet["name"],
        "pet_dob": pet["dob"],
        "other_insurer_state": "/1" if not pet["insured_elsewhere"] else "/0",
        "pay_bank_account": "/Yes",
        "bank_account_name": config.OWNER_BANK_ACCOUNT_NAME,
        "bank_bsb": config.OWNER_BANK_BSB,
        "bank_account_number": config.OWNER_BANK_ACCOUNT_NUMBER,
        "declaration_ack": "/Yes",
        "declaration_date": datetime.now(timezone.utc).date().isoformat(),
    }
    if continuation is not None:
        fields["claim_continuation_state"] = "/0" if continuation else "/1"
    return fields


def _charge(invoice: dict, transaction) -> float:
    """What goes on the claim form: the claimable subtotal (routine-care items
    excluded), not the bank charge — the charge is only the ceiling (it can
    include card surcharge and non-claimable items)."""
    if invoice.get("claimable_amount") is not None:
        return invoice["claimable_amount"]
    if invoice.get("amount") is not None:
        return invoice["amount"]
    return abs(transaction["amount"])


def _build_form_data(pet, transaction, invoice: dict, condition_text: str, continuation: bool | None = None) -> dict:
    return {
        **_shared_fields(pet, continuation),
        "condition_1": condition_text,
        "treatment_date_1": invoice.get("date") or transaction["date"],
        "first_signs_date_1": invoice.get("date") or transaction["date"],
        "charge_1": _charge(invoice, transaction),
    }


def _group_by_condition(item_conditions: list[dict]) -> dict[str, float]:
    """Sum item amounts per assigned condition; items with no condition
    (skipped / not claimable) drop out."""
    groups: dict[str, float] = {}
    for item in item_conditions:
        cond = item.get("condition")
        if not cond:
            continue
        groups[cond] = groups.get(cond, 0.0) + float(item.get("amount") or 0)
    return groups


def _build_grouped_form_data(pet, transaction, item_conditions: list[dict], continuation: bool | None = None) -> dict:
    data = _shared_fields(pet, continuation)
    treatment_date = transaction["date"]
    for i, (condition, amount) in enumerate(_group_by_condition(item_conditions).items(), start=1):
        data[f"condition_{i}"] = condition
        data[f"treatment_date_{i}"] = treatment_date
        data[f"first_signs_date_{i}"] = treatment_date
        data[f"charge_{i}"] = round(amount, 2)
    return data


def apply_item_conditions(claim_id: int, item_conditions: list[dict]) -> dict:
    """Store per-item condition assignments (from the Telegram split flow) and
    advance the claim. Groups items by condition into one form row each."""
    groups = _group_by_condition(item_conditions)
    if not groups:
        return {"ok": False, "message": "Nothing claimable assigned."}
    if len(groups) > 4:
        return {"ok": False, "message": f"{len(groups)} conditions — the Petcover form holds 4. Combine some."}
    if sum(groups.values()) == 0:  # items had no per-item amounts — can't split the charge, don't fill $0 rows
        return {
            "ok": False,
            "message": "These invoice items have no amounts extracted, so I can't split the charge. "
            "Use a single condition instead, or re-match the invoice to re-read the line items.",
        }
    now = datetime.now(timezone.utc).isoformat()
    with db.get_connection() as conn:
        if conn.execute("SELECT 1 FROM vet_claims WHERE id = ?", (claim_id,)).fetchone() is None:
            return {"ok": False, "message": f"No claim #{claim_id} found."}
        conn.execute(
            "UPDATE vet_claims SET item_conditions = ?, condition_text = ?, updated_at = ? WHERE id = ?",
            (json.dumps(item_conditions), "; ".join(groups), now, claim_id),
        )
    process_claim(claim_id)
    return {"ok": True, "message": f"Claim #{claim_id}: {', '.join(f'{k} (${v:.2f})' for k, v in groups.items())}."}


# A page starting a proper per-visit invoice: "INVOICE #411193" (MediPaws) or
# "Tax Invoice ..." (SAH). Account statements carry amounts but no such header
# and no "Patient name:" — verified against MediPaws' real Financial Statement
# vs its Individual Invoices bundle (0 header/patient hits vs 11 each).
_INVOICE_HEADER_RE = re.compile(r"INVOICE\s*#\s*\d{4,}|Tax Invoice", re.IGNORECASE)
_PATIENT_RE = re.compile(r"Patient\s*(?:name)?\s*:\s*\n?\s*(\w+)", re.IGNORECASE)


def _amount_variants(amount: float) -> list[str]:
    plain = f"{amount:.2f}"
    grouped = f"{amount:,.2f}"
    return [plain] if plain == grouped else [plain, grouped]


def find_invoice_segment(page_texts: list[str], amount: float, pet_name: str | None) -> tuple[int, int] | None:
    """Locates THIS claim's invoice inside a multi-invoice PDF bundle: pages are
    segmented at invoice headers (each segment = one per-visit invoice, running
    to the next header), and a segment matches when it contains the claim's
    invoice total and doesn't name a different patient. Returns (start, end)
    page indexes, or None — which doubles as the adequacy validation: an
    account statement has no invoice headers, so nothing can match."""
    headers = [i for i, t in enumerate(page_texts) if _INVOICE_HEADER_RE.search(t or "")]
    if not headers:
        return None
    variants = _amount_variants(float(amount))
    for k, start in enumerate(headers):
        end = (headers[k + 1] if k + 1 < len(headers) else len(page_texts)) - 1
        text = "\n".join(page_texts[start : end + 1])
        if not any(v in text for v in variants):
            continue
        if pet_name:
            patient = _PATIENT_RE.search(text)
            if patient and patient.group(1).lower() != pet_name.lower():
                continue  # another pet's invoice happens to carry the same total
        return (start, end)
    return None


def _email_pdf_documents(email_id: str) -> list[tuple[PdfReader, list[str]]]:
    """Every PDF attachment of the email, as (reader, per-page texts). Unreadable
    PDFs (image scans) yield empty texts and simply won't segment."""
    service = gmail_client.build_service()
    message = service.users().messages().get(userId="me", id=email_id, format="full").execute()
    documents = []
    for part in gmail_client._iter_attachment_parts(message.get("payload", {})):
        if part.get("mimeType") != "application/pdf":
            continue
        attachment = service.users().messages().attachments().get(
            userId="me", messageId=email_id, id=part["body"]["attachmentId"]
        ).execute()
        data = base64.urlsafe_b64decode(attachment["data"] + "==")
        try:
            reader = PdfReader(BytesIO(data))
            documents.append((reader, [page.extract_text() or "" for page in reader.pages]))
        except Exception:
            continue  # corrupt attachment — treated like any other non-matching PDF
    return documents


def ensure_invoice_file(claim) -> None:
    """Extracts THIS claim's per-visit invoice pages from its matched email's
    PDF bundle into their own file and sets invoice_file_path — the step that
    used to be manual (nothing wrote invoice_file_path before). When no
    adequate per-visit invoice exists in any attachment (account statement,
    image-only scan), the claim is flagged so Telegram/dashboard surface it.
    `claim` needs txn_merchant joined in; never overwrites an existing path."""
    if claim["invoice_file_path"] or not claim["matched_email_id"]:
        return
    invoice = json.loads(claim["invoice_data"]) if claim["invoice_data"] else {}
    if invoice.get("amount") is None:
        return
    pet_name = None
    if claim["pet_id"]:
        with db.get_connection() as conn:
            pet = conn.execute("SELECT name FROM pets WHERE id = ?", (claim["pet_id"],)).fetchone()
        pet_name = pet["name"] if pet else None

    for reader, page_texts in _email_pdf_documents(claim["matched_email_id"]):
        segment = find_invoice_segment(page_texts, float(invoice["amount"]), pet_name)
        if segment is None:
            continue
        start, end = segment
        writer = PdfWriter()
        for i in range(start, end + 1):
            writer.add_page(reader.pages[i])
        date_part = (invoice.get("date") or "undated")[:10]
        output_path = str(Path(config.INVOICE_OUTPUT_DIR) / f"claim-{claim['id']}-{date_part}.pdf")
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "wb") as f:
            writer.write(f)
        now = datetime.now(timezone.utc).isoformat()
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE vet_claims SET invoice_file_path = ?, updated_at = ?, "
                "flag = CASE WHEN flag = ? THEN NULL ELSE flag END WHERE id = ?",
                (output_path, now, _AWAITING_INVOICE_FLAG, claim["id"]),
            )
        # The invoice states its patient — reading a printed fact, not guessing.
        if claim["pet_id"] is None:
            patient = _PATIENT_RE.search("\n".join(page_texts[start : end + 1]))
            if patient:
                with db.get_connection() as conn:
                    pet = conn.execute("SELECT id FROM pets WHERE lower(name) = ?", (patient.group(1).lower(),)).fetchone()
                if pet:
                    assign_pet(claim["id"], pet["id"])
        return

    flag = (
        "vet attachment isn't a per-visit itemised invoice (statement/running-total or unreadable) — "
        f"ask {claim['txn_merchant']} for individual invoices"
    )
    if claim["flag"] != flag:
        _flag(claim["id"], flag)


def process_claim_batch(claim_ids: list[int], continuation: bool | None = None) -> None:
    """Bundles up to 4 matched claims for the SAME pet into one filled claim
    document and one Gmail draft (never sends) — mirrors real submissions,
    which list up to 4 invoice line items on a single Petcover form."""
    if not claim_ids or len(claim_ids) > 4:
        raise ValueError("claim batch must be 1-4 claims")

    with db.get_connection() as conn:
        claims = [conn.execute("SELECT * FROM vet_claims WHERE id = ?", (cid,)).fetchone() for cid in claim_ids]
        if any(c is None or c["status"] != "matched" for c in claims):
            return
        pet_ids = {c["pet_id"] for c in claims}
        if len(pet_ids) != 1 or None in pet_ids:
            raise ClaimFillError("batch claims must share exactly one pet")
        pet = conn.execute("SELECT * FROM pets WHERE id = ?", (claims[0]["pet_id"],)).fetchone()
        transactions = {
            c["id"]: conn.execute(
                "SELECT * FROM bank_transactions WHERE id = ?", (c["transaction_id"],)
            ).fetchone()
            for c in claims
        }

    if not pet["claim_process_defined"]:
        for c in claims:
            _flag(c["id"], f"{pet['insurer']} claim process not yet defined")
        return

    missing_condition = [c["id"] for c in claims if not c["condition_text"]]
    if missing_condition:
        for cid in missing_condition:
            _flag(cid, "condition text missing — enter manually on dashboard")
        return

    # Petcover requires every invoice attached — don't draft the batch until all
    # claims have their invoice PDF on file (awaiting the vet's reply).
    missing_invoice = [c["id"] for c in claims if not c["invoice_file_path"]]
    if missing_invoice:
        for cid in missing_invoice:
            _flag(cid, _AWAITING_INVOICE_FLAG)
        return

    data = _shared_fields(pet, continuation)
    for i, c in enumerate(claims, start=1):
        invoice = json.loads(c["invoice_data"]) if c["invoice_data"] else {}
        txn = transactions[c["id"]]
        if _charge(invoice, txn) == 0:
            _flag(c["id"], "routine care only — not claimable")
            return
        data[f"condition_{i}"] = c["condition_text"]
        data[f"treatment_date_{i}"] = invoice.get("date") or txn["date"]
        data[f"first_signs_date_{i}"] = invoice.get("date") or txn["date"]
        data[f"charge_{i}"] = _charge(invoice, txn)

    output_path = str(Path(config.CLAIM_OUTPUT_DIR) / f"claim-batch-{'-'.join(map(str, claim_ids))}.pdf")
    try:
        fill_petcover_form(data, output_path)
    except ClaimFillError as exc:
        for c in claims:
            _flag(c["id"], str(exc))
        return

    attachment_paths = [output_path] + [c["invoice_file_path"] for c in claims if c["invoice_file_path"]]
    try:
        draft_message_id = create_claim_draft(
            to=pet["claim_email"],
            subject=f"Vet claim — {pet['name']}",
            body="Please find attached the completed claim form and invoices.",
            attachment_paths=attachment_paths,
        )
    except Exception as exc:  # Gmail API failure — not silent (spec)
        for c in claims:
            _flag(c["id"], f"Gmail draft creation failed: {exc}")
        return

    with db.get_connection() as conn:
        for c in claims:
            conn.execute(
                "UPDATE vet_claims SET status = 'drafted', claim_file_path = ?, draft_id = ?, "
                "flag = NULL, updated_at = ? WHERE id = ?",
                (output_path, draft_message_id, datetime.now(timezone.utc).isoformat(), c["id"]),
            )


def set_condition_text(claim_id: int, condition_text: str) -> dict:
    """Shared update path for condition text — used by the dashboard route and
    the Telegram /mark command so both stay identical."""
    with db.get_connection() as conn:
        claim = conn.execute("SELECT * FROM vet_claims WHERE id = ?", (claim_id,)).fetchone()
        if claim is None:
            return {"ok": False, "message": f"No claim #{claim_id} found."}
        conn.execute(
            "UPDATE vet_claims SET condition_text = ?, updated_at = ? WHERE id = ?",
            (condition_text, datetime.now(timezone.utc).isoformat(), claim_id),
        )
    process_claim(claim_id)
    return {"ok": True, "message": f"Claim #{claim_id} condition set: {condition_text}"}


def assign_pet(claim_id: int, pet_id: int) -> dict:
    """Shared update path for pet assignment — used by the dashboard route and
    the Telegram /pet command so both stay identical."""
    with db.get_connection() as conn:
        claim = conn.execute("SELECT * FROM vet_claims WHERE id = ?", (claim_id,)).fetchone()
        if claim is None:
            return {"ok": False, "message": f"No claim #{claim_id} found."}
        pet = conn.execute("SELECT * FROM pets WHERE id = ?", (pet_id,)).fetchone()
        if pet is None:
            return {"ok": False, "message": f"No pet #{pet_id} found."}
        conn.execute(
            "UPDATE vet_claims SET pet_id = ?, updated_at = ? WHERE id = ?",
            (pet_id, datetime.now(timezone.utc).isoformat(), claim_id),
        )
    return {"ok": True, "message": f"Claim #{claim_id} assigned to {pet['name']}."}


def mark_reviewed(claim_id: int) -> dict:
    """Telegram-only action: records that Justin has reviewed a drafted claim.
    Never touches status or the draft itself — sending stays manual (spec:
    no autonomous send via Telegram)."""
    with db.get_connection() as conn:
        claim = conn.execute("SELECT * FROM vet_claims WHERE id = ?", (claim_id,)).fetchone()
        if claim is None:
            return {"ok": False, "message": f"No claim #{claim_id} found."}
        if claim["status"] != "drafted":
            return {
                "ok": False,
                "message": f"Claim #{claim_id} isn't drafted yet (status: {claim['status']}) — nothing to review.",
            }
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE vet_claims SET reviewed_at = ?, updated_at = ? WHERE id = ?",
            (now, now, claim_id),
        )
    return {"ok": True, "message": f"Claim #{claim_id} marked reviewed. Send the Gmail draft yourself when ready."}


def process_and_report(claim_id: int) -> dict:
    """Telegram /process: runs the matched->drafted advance for one claim on
    demand instead of waiting for the scheduled pipeline tick, and reports the
    resulting state (reuses process_claim's own validation, doesn't duplicate it)."""
    with db.get_connection() as conn:
        claim = conn.execute("SELECT * FROM vet_claims WHERE id = ?", (claim_id,)).fetchone()
    if claim is None:
        return {"ok": False, "message": f"No claim #{claim_id} found."}
    if claim["status"] != "matched":
        return {
            "ok": False,
            "message": f"Claim #{claim_id} is at status '{claim['status']}' — nothing to process.",
        }
    process_claim(claim_id)
    with db.get_connection() as conn:
        claim = conn.execute("SELECT * FROM vet_claims WHERE id = ?", (claim_id,)).fetchone()
    if claim["status"] == "drafted":
        return {"ok": True, "message": f"Claim #{claim_id} drafted — check Gmail drafts."}
    return {
        "ok": False,
        "message": f"Claim #{claim_id} still matched — {claim['flag'] or 'missing a required field'}.",
    }


def process_claim(claim_id: int, continuation: bool | None = None) -> None:
    """Advances a claim from 'matched' to 'drafted' if pet/process/condition/invoice
    fields are all present; otherwise flags what's missing and stays at 'matched'
    (spec: never guess a required field, never auto-advance without it)."""
    with db.get_connection() as conn:
        claim = conn.execute("SELECT * FROM vet_claims WHERE id = ?", (claim_id,)).fetchone()
        if claim is None or claim["status"] != "matched":
            return
        pet = (
            conn.execute("SELECT * FROM pets WHERE id = ?", (claim["pet_id"],)).fetchone()
            if claim["pet_id"]
            else None
        )
        transaction = conn.execute(
            "SELECT * FROM bank_transactions WHERE id = ?", (claim["transaction_id"],)
        ).fetchone()

    if pet is None:
        return  # awaiting pet attribution (vet-payment-detection) — not a failure, just not ready

    if not pet["claim_process_defined"]:
        _flag(claim_id, f"{pet['insurer']} claim process not yet defined")
        return

    if not claim["condition_text"]:
        _flag(claim_id, "condition text missing — enter manually on dashboard")
        return

    # Petcover requires the itemised invoice attached, not just the form — so
    # don't draft until the invoice PDF is on file (awaiting the vet's reply).
    if not claim["invoice_file_path"]:
        _flag(claim_id, _AWAITING_INVOICE_FLAG)
        return

    invoice = json.loads(claim["invoice_data"]) if claim["invoice_data"] else {}
    if not invoice.get("services"):
        _flag(claim_id, "invoice missing itemized services — enter manually")
        return

    if _charge(invoice, transaction) == 0:
        _flag(claim_id, "routine care only — not claimable")
        return

    output_path = str(Path(config.CLAIM_OUTPUT_DIR) / f"claim-{claim_id}.pdf")
    if claim["item_conditions"]:
        # one invoice spanning several conditions → one form row per condition
        data = _build_grouped_form_data(pet, transaction, json.loads(claim["item_conditions"]), continuation)
    else:
        data = _build_form_data(pet, transaction, invoice, claim["condition_text"], continuation)
    try:
        fill_petcover_form(data, output_path)
    except ClaimFillError as exc:
        _flag(claim_id, str(exc))
        return

    attachment_paths = [output_path] + ([claim["invoice_file_path"]] if claim["invoice_file_path"] else [])
    try:
        draft_message_id = create_claim_draft(
            to=pet["claim_email"],
            subject=f"Vet claim — {pet['name']}",
            body="Please find attached the completed claim form and invoice details.",
            attachment_paths=attachment_paths,
        )
    except Exception as exc:  # Gmail API failure — not silent (spec)
        _flag(claim_id, f"Gmail draft creation failed: {exc}")
        return

    with db.get_connection() as conn:
        conn.execute(
            "UPDATE vet_claims SET status = 'drafted', claim_file_path = ?, draft_id = ?, "
            "flag = NULL, updated_at = ? WHERE id = ?",
            (output_path, draft_message_id, datetime.now(timezone.utc).isoformat(), claim_id),
        )
