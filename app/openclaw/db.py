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
    -- Policy limits driving expected-reimbursement math. Nullable: a pet whose
    -- excess/cap we don't know (e.g. Echo, no Petcover policy) leaves these NULL
    -- and the dashboard flags reimbursement unavailable rather than guessing.
    -- Excess is per-condition, per policy year; cap is per policy year.
    annual_excess REAL,
    annual_cap REAL,
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
-- container restarts, so a restart can't re-spam (ADR-0015; earlier comments
-- cited ADR-0011, which is about Petcover correlation, not alerting).
CREATE TABLE IF NOT EXISTS ops_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    sent_at TEXT NOT NULL
);

-- Every Telegram message in and out. Three jobs in one table: the training
-- dataset (payload + app_version), the audit trail that answers "did my tap
-- register?", and the replay queue (processed_at IS NULL = still owed).
-- A morning of taps changed nothing and left no evidence of why, because
-- nothing durable recorded them.
-- update_id is Telegram's own, so UNIQUE + INSERT OR IGNORE dedupes replays
-- and Telegram redeliveries for free. NULL for outbound rows.
CREATE TABLE IF NOT EXISTS telegram_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    update_id INTEGER UNIQUE,
    direction TEXT NOT NULL,
    kind TEXT,
    summary TEXT,
    payload TEXT NOT NULL,
    app_version TEXT NOT NULL,
    received_at TEXT NOT NULL,
    processed_at TEXT,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_telegram_messages_pending
    ON telegram_messages(direction, processed_at);

-- A mutation the model proposed, waiting for Justin to tap Confirm.
-- Durable rather than in-process because the proposal and the tap are now two
-- separate requests in two separate runtimes: the MCP call arrives from the
-- gateway's agent, the tap arrives later through the plugin. `telegram_bot`'s
-- in-memory `_pending_actions` dict cannot span that, and a restart in between
-- would silently turn a proposal into a tap that does nothing.
-- `confirmed_at` is what makes a tap single-use — a double tap must not commit
-- twice, and Telegram redelivers.
CREATE TABLE IF NOT EXISTS pending_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    origin TEXT NOT NULL,
    action TEXT NOT NULL,
    claim_id INTEGER,
    task_id INTEGER,
    arg TEXT,
    label TEXT NOT NULL,
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    result TEXT
);

CREATE INDEX IF NOT EXISTS idx_pending_proposals_open
    ON pending_proposals(confirmed_at, created_at);
"""

# vet_claims columns added after the table's initial release — CREATE TABLE IF
# NOT EXISTS won't add these to an already-created DB, so they're migrated in
# explicitly (see _migrate_added_columns).
_VET_CLAIMS_ADDED_COLUMNS = {
    "telegram_notified_status": "TEXT",
    "telegram_notified_flag": "TEXT",
    "reviewed_at": "TEXT",
    "petcover_reference": "TEXT",
    "rejected_email_ids": "TEXT",  # JSON list of invoice emails Justin unmatched — never re-match these
    "item_conditions": "TEXT",  # JSON [{description, amount, condition}] when one invoice spans >1 condition
    "petcover_sr": "INTEGER",  # Petcover's per-document serial within a Condition Thread ("DC1-27-5628 Sr 3")
}

# Echo's claim_email stays NULL until Justin supplies Bow Wow Insurance's process
# (tasks.md 6.0) — claim_process_defined=0 blocks fill/draft for Echo's claims.
SEED_PETS = """
INSERT OR IGNORE INTO pets (name, insurer, claim_email, claim_process_defined, annual_excess, annual_cap)
VALUES ('Aari', 'Petcover', 'claims.au@petcovergroup.com', 1, 150, 10000),
       ('Echo', 'Bow Wow Insurance', NULL, 0, NULL, NULL);
"""


def _migrate_added_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for column, col_type in columns.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


# pets columns added after the table's initial release — same reasoning as
# _VET_CLAIMS_ADDED_COLUMNS above.
_PETS_ADDED_COLUMNS = {
    "annual_excess": "REAL",
    "annual_cap": "REAL",
    "policy_anniversary": "TEXT",
}


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
    # The host and the container both open this bind-mounted file. In the default
    # rollback journal a writer blocks readers outright, which is how a host-side
    # write during a container read produced a "disk I/O error". WAL lets them
    # coexist; busy_timeout waits its turn instead of failing instantly. Verified
    # working (and persisting) on the Docker bind mount — not all virtual
    # filesystems support WAL, so re-check if the mount ever changes.
    #
    # The sidecars WAL needs (-wal, -shm) are the fragile part, and a *read* is
    # enough to break them: a host-side read-write open (sqlite3's default) that
    # closes cleanly checkpoints and deletes them, after which Docker Desktop's
    # bind-mount cache can hold those names as present-but-absent and this very
    # PRAGMA fails with "unable to open database file" until the container is
    # restarted. Total outage, 51 minutes, 2026-07-25. Host queries must use
    # mode=ro — ADR-0018.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def registered_chat_id() -> int | None:
    """The chat Justin registered with /start. THE reader — do not inline it.

    Lived in `telegram_bot`, which the gateway cutover deletes, while being a
    plain DB read three unrelated callers need. Same move as `list_pet_names`
    below, for the same reason.
    """
    from . import config

    with get_connection() as conn:
        row = conn.execute("SELECT chat_id FROM telegram_registrations WHERE username = ?",
                           (config.TELEGRAM_USERNAME,)).fetchone()
    return row["chat_id"] if row else None


def latest_inbound_text() -> str:
    """The most recent thing Justin typed, as the message log recorded it.

    The MCP surface needs this and cannot see the conversation: the gateway's
    agent calls a tool, and the tool gets its arguments and nothing else. The
    two-pets refusal keys on what he actually wrote, so taking it from the
    model's paraphrase would let the model paraphrase the refusal away — which
    is the exact failure the refusal exists for (2026-07-27).

    `summary` rather than `payload`: it is already the extracted text, written
    by the same `_describe` for both transports. Empty string when there is
    nothing, never None — every caller substring-matches.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT summary FROM telegram_messages WHERE direction = 'in' AND kind IN ('text', 'command') "
            "ORDER BY id DESC LIMIT 1").fetchone()
    return (row["summary"] or "") if row else ""


def list_pet_names() -> list[str]:
    """The pets on file, alphabetical. THE reader — do not inline this query.

    It was written out four times (three in `agent.py`, one in `mcp_server.py`),
    which is the "second answer to the same question" shape this codebase has
    been bitten by repeatedly. Collapsed here on review, 2026-08-02.

    It matters more than a tidy-up because of what consumes it: this list is
    what tells the model which pets exist. Left to guess, it invented 'Whiskers'
    and 'Fluffy'. A copy that drifts does not fail loudly — it produces a
    confident answer about a pet that is not there.
    """
    with get_connection() as conn:
        return [r["name"] for r in conn.execute("SELECT name FROM pets ORDER BY name")]


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
