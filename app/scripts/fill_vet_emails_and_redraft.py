"""One-off follow-up to draft_yearly_invoice_requests.py: record the vet email
addresses Justin confirmed/looked up into vet_contacts, delete the first batch of
blank-recipient drafts, and re-run the drafter so To: fills in. Gmail drafts only
— never sends.

Emails were verified on each clinic's own contact page. Boundary Road Vet
(Peakhurst; the "BANKSTOWN VET" bank descriptor) publishes no email — left out,
so its draft stays blank rather than guessing an address. SP Vets Love Pets is
Justin's to look up.
"""

import base64
import sys
from datetime import date, timedelta
from email.header import decode_header, make_header
from email.message import EmailMessage

sys.path.insert(0, ".")

from openclaw import config, db, gmail_client  # noqa: E402

WINDOW_START = (date.today() - timedelta(days=365)).isoformat()
DRAFT_SUBJECT_PREFIX = "Invoice request (past 12 months) —"

# merchant (exact bank descriptor) -> verified clinic email
FOUND = {
    "MediPaws Sydney Leichhardt NSW": "reception@medipaws.com.au",
    "THE SHIRE VETERINARY CARINGBAH NSW": "admin@theshirevet.com.au",
    "SAH INNER WEST PTY LT Stanmore NSW": "innerwest@sydneyanimalhospitals.com.au",
    "BANKSTOWN VET PEAKHURST NSW": "pets@boundaryroadvet.com.au",  # Boundary Road Vet
}

# Not a vet — SP Vets Love Pets is an online retail store, mis-detected. No draft.
EXCLUDE = {"SP VETS LOVE PETS WEST PERTH WA"}


def _upsert_contacts():
    with db.get_connection() as conn:
        for merchant, email in FOUND.items():
            conn.execute(
                "INSERT INTO vet_contacts (merchant, email) VALUES (?, ?) "
                "ON CONFLICT(merchant) DO UPDATE SET email = excluded.email",
                (merchant, email),
            )
    print(f"vet_contacts upserted: {len(FOUND)}")


def _delete_old_drafts(service):
    resp = service.users().drafts().list(userId="me", maxResults=100).execute()
    deleted = 0
    for d in resp.get("drafts", []):
        msg = service.users().messages().get(
            userId="me", id=d["message"]["id"], format="metadata", metadataHeaders=["Subject"]
        ).execute()
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        # Subject may be RFC-2047 encoded (the em-dash makes it non-ASCII), so
        # decode before matching — match the ASCII prefix that has no em-dash.
        subject = str(make_header(decode_header(headers.get("Subject", ""))))
        if subject.startswith("Invoice request (past 12 months)"):
            service.users().drafts().delete(userId="me", id=d["id"]).execute()
            deleted += 1
    print(f"old drafts deleted: {deleted}")


# --- drafting (same shape as draft_yearly_invoice_requests.py) ---

def _visits_by_vet():
    with db.get_connection() as conn:
        rows = conn.execute(
            """
            SELECT bt.merchant, bt.date AS visit_date, ABS(bt.amount) AS amount,
                   (SELECT email FROM vet_contacts vc WHERE vc.merchant = bt.merchant) AS email,
                   GROUP_CONCAT(DISTINCT p.name) AS pets
            FROM vet_claims cl
            JOIN bank_transactions bt ON bt.id = cl.transaction_id
            LEFT JOIN pets p ON p.id = cl.pet_id
            WHERE bt.date >= ?
            GROUP BY bt.merchant, bt.date, bt.amount
            ORDER BY bt.merchant, bt.date
            """,
            (WINDOW_START,),
        ).fetchall()
    vets: dict[str, dict] = {}
    for r in rows:
        v = vets.setdefault(r["merchant"], {"email": r["email"], "visits": [], "pets": set()})
        v["visits"].append((r["visit_date"], r["amount"]))
        if r["pets"]:
            v["pets"].update(r["pets"].split(","))
    return vets


def _dog_phrase(pets) -> str:
    """'our dogs Aari Goldberg and Echo Goldberg' — surname added so the vet knows
    whose pets these are; falls back to 'our dogs' when no pet is assigned."""
    surname = config.OWNER_NAME.split()[-1] if config.OWNER_NAME else ""
    named = [f"{p} {surname}".strip() for p in sorted(pets)]
    if not named:
        return "our dogs"
    if len(named) == 1:
        return f"our dog {named[0]}"
    joined = ", ".join(named[:-1]) + " and " + named[-1]
    return f"our dogs {joined}"


def _body(visits, pets):
    lines = "\n".join(f"  - {d}  —  ${a:,.2f}" for d, a in visits)
    total = sum(a for _, a in visits)
    dogs = _dog_phrase(pets)
    return (
        f"Hi,\n\nWe're compiling pet-insurance claims and need itemised tax invoices for "
        f"all visits over the past 12 months for {dogs}. Could you please send through "
        f"the itemised invoices for the following visits?\n\n{lines}\n\n"
        f"  Total across {len(visits)} visit(s): ${total:,.2f}\n\n"
        f"An itemised breakdown (individual line items per visit) is what the insurer needs "
        f"— a plain total isn't enough. If a visit covered more than one dog, a split by dog "
        f"would help too.\n\nThanks,\n{config.OWNER_NAME or ''}".rstrip() + "\n"
    )


def _create(service, to, merchant, body):
    # EmailMessage sets Content-Type + charset and RFC-2047-encodes the Subject,
    # so the em-dash renders correctly instead of mojibake (â€”) in both fields.
    msg = EmailMessage()
    if to:
        msg["To"] = to
    msg["Subject"] = f"{DRAFT_SUBJECT_PREFIX} {merchant}"
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()


def main():
    _upsert_contacts()
    service = gmail_client.build_service()
    _delete_old_drafts(service)
    missing = []
    for merchant, v in _visits_by_vet().items():
        if merchant in EXCLUDE:
            print(f"skipped: {merchant[:35]:35} -> not a vet, no draft")
            continue
        to = v["email"] or ""
        _create(service, to, merchant, _body(v["visits"], v["pets"]))
        print(f"drafted: {merchant[:35]:35} -> {to or 'NO EMAIL — add recipient'}")
        if not to:
            missing.append(merchant)
    print(f"\ndone, 0 sent. still need an address: {missing or 'none'}")


if __name__ == "__main__":
    main()
