import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    source TEXT NOT NULL DEFAULT 'chat',
    source_message_id TEXT,
    follow_up_at TEXT,
    outcome TEXT,
    outcome_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    scheduled_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'scheduled',
    job_id TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    purpose TEXT NOT NULL,
    success INTEGER NOT NULL,
    latency_ms INTEGER NOT NULL,
    error TEXT
);

CREATE TABLE IF NOT EXISTS processed_emails (
    message_id TEXT PRIMARY KEY,
    processed_at TEXT NOT NULL,
    task_id INTEGER REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS pets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    insurer TEXT NOT NULL,
    claim_email TEXT,
    claim_process_defined INTEGER NOT NULL DEFAULT 0,
    policy_number TEXT,
    dob TEXT,
    insured_elsewhere INTEGER NOT NULL DEFAULT 0,
    -- Policy anniversary "MM-DD": excess ($150/condition) and the $10k annual
    -- cap reset here, NOT at calendar year (settlement validation, ADR-0011).
    policy_anniversary TEXT
);

CREATE TABLE IF NOT EXISTS bank_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    amount REAL NOT NULL,
    merchant TEXT NOT NULL,
    category TEXT,
    vet_flag INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE(date, amount, merchant)
);

CREATE TABLE IF NOT EXISTS vet_contacts (
    merchant TEXT PRIMARY KEY,
    email TEXT NOT NULL
);

-- Merchants the keyword heuristic wrongly flags as vet (retail/online stores
-- with "vet"/"pets" in the name, e.g. "sp vets love pets"). Justin adds these
-- from Telegram; vet_detection checks them (substring, lowercased) before the
-- keyword list so they never become claims.
CREATE TABLE IF NOT EXISTS non_vet_merchants (
    pattern TEXT PRIMARY KEY,
    added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vet_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id INTEGER NOT NULL REFERENCES bank_transactions(id),
    pet_id INTEGER REFERENCES pets(id),
    matched_email_id TEXT,
    invoice_data TEXT,
    invoice_file_path TEXT,
    condition_text TEXT,
    claim_file_path TEXT,
    draft_id TEXT,
    invoice_request_sent_at TEXT,
    petcover_reference TEXT,
    status TEXT NOT NULL DEFAULT 'pending_match',
    flag TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Append-only: a claim's status can flip back and forth (suspended, resolved,
-- settled) — a single mutable column can't represent that history, this can.
-- event_type: acknowledged | info_requested | suspended | settled | declined
--            | unclassified | confirmed_resolved
CREATE TABLE IF NOT EXISTS claim_status_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id INTEGER REFERENCES vet_claims(id),
    event_type TEXT NOT NULL,
    raw_email_id TEXT,
    detail TEXT,
    created_at TEXT NOT NULL
);

-- One LLM extraction per email, ever: invoice candidates are re-tested against
-- claims every pipeline tick and across claims; caching the parsed invoices
-- makes re-tests free (deterministic gates only) and stops quota burn.
CREATE TABLE IF NOT EXISTS email_extractions (
    message_id TEXT PRIMARY KEY,
    extracted_json TEXT NOT NULL,
    extracted_at TEXT NOT NULL
);

-- Vision-OCR fallback budget for scanned (image-only) invoice PDFs: hard cap
-- on extraction attempts per email so a scan the model can't read doesn't
-- burn tokens every tick forever (successes cache in email_extractions).
CREATE TABLE IF NOT EXISTS vision_ocr_attempts (
    message_id TEXT PRIMARY KEY,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT NOT NULL
);

-- One vet invoice paid over several card charges (confirmed live: $2,521.46
-- invoice = $551.06 + $1,970.40 charges, same day). Which claim carries the
-- invoice is Justin's call — the proposal holds the invoice + candidate claim
-- ids until he picks one on Telegram. status: open | resolved
CREATE TABLE IF NOT EXISTS split_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id TEXT NOT NULL,
    invoice_json TEXT NOT NULL,
    claim_ids TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    notified_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS telegram_registrations (
    username TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    registered_at TEXT NOT NULL
);

-- Operational alerts sent to Telegram (currently only Gmail auth death).
-- One row per alert actually sent; used to rate-limit (≤5/24h) and to know a
-- failure is outstanding so recovery is confirmed exactly once. Survives
-- container restarts, so a restart can't re-spam (ADR-0011 ops-alerting).
CREATE TABLE IF NOT EXISTS ops_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    sent_at TEXT NOT NULL
);
"""

# vet_claims columns added after the table's initial release — CREATE TABLE IF
# NOT EXISTS won't add these to an already-created DB, so they're migrated in
# explicitly (see _migrate_vet_claims_columns).
_VET_CLAIMS_ADDED_COLUMNS = {
    "telegram_notified_status": "TEXT",
    "telegram_notified_flag": "TEXT",
    "reviewed_at": "TEXT",
    "petcover_reference": "TEXT",
    "rejected_email_ids": "TEXT",  # JSON list of invoice emails Justin unmatched — never re-match these
    "item_conditions": "TEXT",  # JSON [{description, amount, condition}] when one invoice spans >1 condition
    "petcover_sr": "INTEGER",  # Petcover's per-document serial within a Condition Thread ("DC1-27-5628 Sr 3")
}

# pets columns added after the table's initial release — same migration reason
# as _VET_CLAIMS_ADDED_COLUMNS.
_PETS_ADDED_COLUMNS = {
    "policy_anniversary": "TEXT",
}

# Echo's claim_email stays NULL until Justin supplies Bow Wow Insurance's process
# (tasks.md 6.0) — claim_process_defined=0 blocks fill/draft for Echo's claims.
SEED_PETS = """
INSERT OR IGNORE INTO pets (name, insurer, claim_email, claim_process_defined)
VALUES ('Aari', 'Petcover', 'claims.au@petcovergroup.com', 1),
       ('Echo', 'Bow Wow Insurance', NULL, 0);
"""


def _migrate_added_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for column, col_type in columns.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


def init_db(path: str | None = None) -> None:
    path = path or config.DATABASE_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        _migrate_added_columns(conn, "vet_claims", _VET_CLAIMS_ADDED_COLUMNS)
        _migrate_added_columns(conn, "pets", _PETS_ADDED_COLUMNS)
        conn.executescript(SEED_PETS)


@contextmanager
def get_connection(path: str | None = None):
    path = path or config.DATABASE_PATH
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_non_vet_patterns() -> list[str]:
    """Lowercased merchant substrings that must never be classified as vet."""
    with get_connection() as conn:
        return [r[0] for r in conn.execute("SELECT pattern FROM non_vet_merchants")]


def add_non_vet_pattern(pattern: str) -> bool:
    """Records a non-vet merchant substring (lowercased). Returns False if the
    pattern was blank or already present."""
    pattern = pattern.strip().lower()
    if not pattern:
        return False
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO non_vet_merchants (pattern, added_at) VALUES (?, ?)",
            (pattern, now),
        )
        return cur.rowcount > 0
