import csv
import logging
from datetime import datetime, timezone

from . import db

logger = logging.getLogger(__name__)

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
    for line_number, row in enumerate(csv.reader(csv_text.splitlines()), start=1):
        if not row:
            continue
        try:
            rows.append(_parse_row(row))
        except CsvParseError as exc:
            raise CsvParseError(f"row {line_number}: {exc}") from exc
    return rows


def import_rows(rows: list[dict]) -> tuple[int, int, int]:
    """Inserts parsed rows, skipping ones already stored by date+amount+merchant.
    Overlapping re-uploads are the normal case (spec), so silent skip is correct.

    Returns `(read, inserted, skipped)` — the reply has to state all three
    (csv-upload-via-telegram task 3.2), not just how many were new.
    """
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
    return len(rows), inserted, len(rows) - inserted


def latest_transaction_date() -> str | None:
    """The coverage watermark: how far the held transactions reach, derived
    fresh from `bank_transactions` rather than stored (design.md Decision 3).

    `None` for an empty table — callers must say "no transactions held" rather
    than rendering a blank (task 5.3), and must never phrase this as "covered
    through": `MAX(date)` says how far coverage reaches, not that it is
    continuous. A missing middle week is invisible to this query.
    """
    with db.get_connection() as conn:
        row = conn.execute("SELECT MAX(date) FROM bank_transactions").fetchone()
    return row[0] if row else None


def _watermark_line() -> str:
    watermark = latest_transaction_date()
    return f"Latest transaction held: {watermark}." if watermark else "No transactions held."


def _claims_count() -> int:
    with db.get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM vet_claims").fetchone()[0]


def ingest_upload(csv_text: str) -> str:
    """The one entrypoint both upload channels call (task 3.3): parse, import,
    scan for claims under the tick's own lock, and build a reply that states
    what actually happened. **The lock name must be `"tick"`** — matching
    `/internal/tick` — or the mutual exclusion `main.upload_transactions` needs
    (design.md Decision 4) is decorative.

    Deliberately imports `internal_api` and `pipeline` inside the function, not
    at module level: `internal_api` will import this module for the new
    `/internal/transactions/csv` route, and a module-level import back would be
    a cycle. `commands.dispatch` takes the same shape for the same reason.
    """
    from . import internal_api, pipeline

    try:
        rows = parse(csv_text)
    except CsvParseError as exc:
        logger.error("NetBank CSV parse failure: %s", exc)
        return f"Upload rejected: {exc}"

    read, inserted, skipped = import_rows(rows)
    summary = f"Imported {inserted} new transaction(s) ({read} read, {skipped} already held)."

    claims_before = _claims_count()
    ran, error = True, None
    try:
        ran, _ = internal_api.run_exclusive("tick", pipeline.run_once)
    except Exception as exc:  # noqa: BLE001 — a scan failure must not look like success
        error = str(exc)
        logger.exception("csv upload: claim scan failed")

    if not ran:
        # `ran=False` means a tick was already in flight, not that this one
        # failed. Reported exactly as `/internal/tick` does (design.md Decision
        # 4) — a skipped run stated as a completed one is the silent no-op the
        # hard rules forbid.
        return f"{summary} A scan was already running; this upload will be covered by it. {_watermark_line()}"
    if error is not None:
        # Import succeeded and must not be reported as a plain failure — but a
        # scan that raised must not be reported as a plain success either.
        return f"{summary} Import succeeded but the claim scan failed: {error} {_watermark_line()}"

    claims_found = max(0, _claims_count() - claims_before)
    return f"{summary} {claims_found} claim(s) found. {_watermark_line()}"
