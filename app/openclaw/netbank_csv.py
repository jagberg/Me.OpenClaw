import csv
from datetime import datetime, timezone

from . import db

EXPECTED_COLUMNS = 4


class CsvParseError(Exception):
    """Raised when a row doesn't match the expected 4-column positional NetBank layout."""


def _parse_row(row: list[str]) -> dict:
    if len(row) != EXPECTED_COLUMNS:
        raise CsvParseError(f"expected {EXPECTED_COLUMNS} columns, got {len(row)}: {row}")
    date_str, amount_str, description, _balance = row
    try:
        date = datetime.strptime(date_str.strip(), "%d/%m/%Y").date()
    except ValueError as exc:
        raise CsvParseError(f"unparsable date {date_str!r}") from exc
    try:
        amount = float(amount_str.strip())
    except ValueError as exc:
        raise CsvParseError(f"unparsable amount {amount_str!r}") from exc
    # description is fixed-width padded ("merchant name + location" with no
    # reliable delimiter) — collapsing whitespace is the only normalization
    # that's reliable across rows.
    merchant = " ".join(description.split())
    if not merchant:
        raise CsvParseError(f"empty merchant/description field in row: {row}")
    return {"date": date.isoformat(), "amount": amount, "merchant": merchant}


def parse(csv_text: str) -> list[dict]:
    """Parses NetBank's no-header 4-column export. Raises CsvParseError on the first
    row that doesn't fit — never inserts partial/garbage data (see spec scenario)."""
    rows = []
    for row in csv.reader(csv_text.splitlines()):
        if not row:
            continue
        rows.append(_parse_row(row))
    return rows


def import_rows(rows: list[dict]) -> int:
    """Inserts parsed rows, skipping ones already stored by date+amount+merchant.
    Overlapping re-uploads are the normal case (spec), so silent skip is correct."""
    inserted = 0
    now = datetime.now(timezone.utc).isoformat()
    with db.get_connection() as conn:
        for r in rows:
            cur = conn.execute(
                "INSERT OR IGNORE INTO bank_transactions (date, amount, merchant, created_at) "
                "VALUES (?, ?, ?, ?)",
                (r["date"], r["amount"], r["merchant"], now),
            )
            if cur.rowcount:
                inserted += 1
    return inserted
