"""One-off: draft a single consolidated invoice-request email to each vet used
in the past 12 months, asking for itemised invoices for ALL their visits in that
window. Creates Gmail drafts only — never sends (Justin reviews and sends).

Vets with no email on file (vet_contacts) get a draft with a blank To: and the
merchant name in the subject so Justin can add the address — we never guess a
recipient. Run once from the app dir (or `docker exec` in the container):

    python scripts/draft_yearly_invoice_requests.py
"""

import base64
import sys
from datetime import date, timedelta

sys.path.insert(0, ".")

from openclaw import config, db, gmail_client  # noqa: E402

WINDOW_START = (date.today() - timedelta(days=365)).isoformat()


def _visits_by_vet():
    with db.get_connection() as conn:
        rows = conn.execute(
            """
            SELECT bt.merchant,
                   bt.date  AS visit_date,
                   ABS(bt.amount) AS amount,
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


def _body(merchant: str, visits: list[tuple[str, float]], pets: set[str]) -> str:
    lines = "\n".join(f"  - {d}  —  ${a:,.2f}" for d, a in visits)
    total = sum(a for _, a in visits)
    pet_str = ", ".join(sorted(pets)) if pets else "our pets"
    return (
        f"Hi,\n\n"
        f"We're compiling pet-insurance claims and need itemised tax invoices for "
        f"all visits over the past 12 months for {pet_str}. Could you please send "
        f"through the itemised invoices for the following visits?\n\n"
        f"{lines}\n\n"
        f"  Total across {len(visits)} visit(s): ${total:,.2f}\n\n"
        f"An itemised breakdown (individual line items per visit) is what the insurer "
        f"needs — a plain total isn't enough. If a visit covered more than one pet, a "
        f"split by pet would help too.\n\n"
        f"Thanks,\n{config.OWNER_NAME or ''}".rstrip()
        + "\n"
    )


def _create_draft(service, to: str, subject: str, body: str) -> str:
    headers = f"To: {to}\r\nSubject: {subject}\r\n\r\n"
    raw = base64.urlsafe_b64encode((headers + body).encode()).decode()
    draft = service.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
    return draft["message"]["id"]


def main() -> None:
    vets = _visits_by_vet()
    if not vets:
        print("No vet visits in the past year — nothing to draft.")
        return
    service = gmail_client.build_service()
    missing = []
    for merchant, v in vets.items():
        to = v["email"] or ""
        # Merchant in the subject so blank-recipient drafts stay identifiable.
        subject = f"Invoice request (past 12 months) — {merchant}"
        body = _body(merchant, v["visits"], v["pets"])
        draft_id = _create_draft(service, to, subject, body)
        tag = to or "NO EMAIL — add recipient"
        print(f"drafted: {merchant[:35]:35} {len(v['visits'])} visits -> {tag}  (draft {draft_id})")
        if not to:
            missing.append(merchant)
    print(f"\n{len(vets)} drafts created (0 sent).")
    if missing:
        print("Add an address before sending these (no vet email on file):")
        for m in missing:
            print(f"  - {m}")


if __name__ == "__main__":
    main()
