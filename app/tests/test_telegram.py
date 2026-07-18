"""Runnable smoke checks for the Telegram claim actions — no live network calls,
no telegram Update objects (pure handler functions only). Run with:
python tests/test_telegram.py"""

import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_tmpdir = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_tmpdir, "test_telegram.db")
os.environ.setdefault("GEMINI_API_KEY", "")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")

from openclaw import claim_forms, db, pipeline, telegram_bot  # noqa: E402

AUTHORIZED_USER = "jagberg"
UNAUTHORIZED_USER = "someone_else"


def _seed_matched_claim(merchant: str, condition_text: str | None = "ear infection") -> int:
    db.init_db()
    now = datetime.now(timezone.utc).isoformat()
    with db.get_connection() as conn:
        pet = conn.execute("SELECT * FROM pets WHERE name = 'Aari'").fetchone()
        txn_id = conn.execute(
            "INSERT INTO bank_transactions (date, amount, merchant, category, vet_flag, created_at) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            ("2026-07-01", -100.0, merchant, "medical", now),
        ).lastrowid
        claim_id = conn.execute(
            "INSERT INTO vet_claims (transaction_id, pet_id, invoice_data, condition_text, status, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, 'matched', ?, ?)",
            (
                txn_id,
                pet["id"],
                json.dumps({"date": "2026-07-01", "amount": 100.0, "services": ["consult"]}),
                condition_text,
                now,
                now,
            ),
        ).lastrowid
    return claim_id


def _seed_drafted_claim(merchant: str, draft_id: str = "draft-abc") -> int:
    claim_id = _seed_matched_claim(merchant)
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE vet_claims SET status = 'drafted', draft_id = ? WHERE id = ?",
            (draft_id, claim_id),
        )
    return claim_id


def _with_stubbed_claim_fill(fn):
    """Swaps claim_forms' PDF-fill and Gmail-draft calls for stubs so
    process_claim can run without a real template file or Gmail credentials."""
    original_fill = claim_forms.fill_petcover_form
    original_draft = claim_forms.create_claim_draft
    claim_forms.fill_petcover_form = lambda data, output_path: Path(output_path).parent.mkdir(
        parents=True, exist_ok=True
    ) or Path(output_path).write_text("stub")
    claim_forms.create_claim_draft = lambda **kwargs: "draft-stub-id"
    try:
        fn()
    finally:
        claim_forms.fill_petcover_form = original_fill
        claim_forms.create_claim_draft = original_draft


def test_start_registers_matching_username():
    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM telegram_registrations")
    telegram_bot.handle_start(AUTHORIZED_USER, 111222)
    assert telegram_bot.get_registered_chat_id() == 111222


def test_start_ignores_non_matching_username():
    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM telegram_registrations")
    telegram_bot.handle_start(UNAUTHORIZED_USER, 999999)
    assert telegram_bot.get_registered_chat_id() is None


def test_command_rejected_for_unauthorized_user():
    claim_id = _seed_matched_claim("UNAUTHORIZED TEST VET")
    result = telegram_bot.handle_mark(UNAUTHORIZED_USER, claim_id, "should not apply")
    assert result["ok"] is False
    with db.get_connection() as conn:
        row = conn.execute("SELECT condition_text FROM vet_claims WHERE id = ?", (claim_id,)).fetchone()
    assert row["condition_text"] == "ear infection", "unauthorized /mark must not change the claim"


def test_mark_condition_matches_dashboard_path():
    claim_id = _seed_matched_claim("CONDITION TEST VET", condition_text=None)

    def run():
        claim_forms.set_condition_text(claim_id, "broken leg")

    _with_stubbed_claim_fill(run)
    with db.get_connection() as conn:
        row = conn.execute("SELECT condition_text FROM vet_claims WHERE id = ?", (claim_id,)).fetchone()
    assert row["condition_text"] == "broken leg"


def test_process_advances_ready_claim():
    claim_id = _seed_matched_claim("PROCESS READY VET")

    def run():
        result = claim_forms.process_and_report(claim_id)
        assert result["ok"] is True
        with db.get_connection() as conn:
            row = conn.execute("SELECT status FROM vet_claims WHERE id = ?", (claim_id,)).fetchone()
        assert row["status"] == "drafted"

    _with_stubbed_claim_fill(run)


def test_process_leaves_incomplete_claim_matched():
    claim_id = _seed_matched_claim("PROCESS INCOMPLETE VET", condition_text=None)
    result = claim_forms.process_and_report(claim_id)
    assert result["ok"] is False
    assert "condition" in result["message"].lower()
    with db.get_connection() as conn:
        row = conn.execute("SELECT status FROM vet_claims WHERE id = ?", (claim_id,)).fetchone()
    assert row["status"] == "matched"


def test_notification_dedup():
    # notify_claim_states scans the whole table by design (correct for real
    # use) — the shared test DB may hold other un-notified claims from earlier
    # tests, so filter sent messages down to this test's own claim.
    claim_id = _seed_matched_claim("DEDUP VET")
    with db.get_connection() as conn:
        conn.execute("UPDATE vet_claims SET flag = ? WHERE id = ?", ("condition text missing", claim_id))
    sent = []
    pipeline.notify_claim_states(send_fn=sent.append)
    pipeline.notify_claim_states(send_fn=sent.append)
    own_sent = [t for t in sent if re.search(rf"#{claim_id}(?!\d)", t)]
    assert len(own_sent) == 1, "unchanged state must not notify twice"


def test_notification_fires_on_new_state():
    claim_id = _seed_matched_claim("NEW STATE VET")
    with db.get_connection() as conn:
        conn.execute("UPDATE vet_claims SET flag = ? WHERE id = ?", ("condition text missing", claim_id))
    sent = []
    pipeline.notify_claim_states(send_fn=sent.append)
    with db.get_connection() as conn:
        conn.execute("UPDATE vet_claims SET flag = ? WHERE id = ?", ("invoice missing itemized services", claim_id))
    pipeline.notify_claim_states(send_fn=sent.append)
    own_sent = [t for t in sent if re.search(rf"#{claim_id}(?!\d)", t)]
    assert len(own_sent) == 2, "a genuinely new flag/status must notify again"


def test_reviewed_mark_requires_drafted():
    claim_id = _seed_matched_claim("REVIEW GUARD VET")
    result = claim_forms.mark_reviewed(claim_id)
    assert result["ok"] is False
    with db.get_connection() as conn:
        row = conn.execute("SELECT reviewed_at FROM vet_claims WHERE id = ?", (claim_id,)).fetchone()
    assert row["reviewed_at"] is None


def test_reviewed_mark_sets_timestamp_on_drafted():
    claim_id = _seed_drafted_claim("REVIEW OK VET")

    original_draft = claim_forms.create_claim_draft

    def _fail_if_called(**kwargs):
        raise AssertionError("mark_reviewed must never call create_claim_draft")

    claim_forms.create_claim_draft = _fail_if_called
    try:
        result = claim_forms.mark_reviewed(claim_id)
    finally:
        claim_forms.create_claim_draft = original_draft

    assert result["ok"] is True
    with db.get_connection() as conn:
        row = conn.execute("SELECT reviewed_at FROM vet_claims WHERE id = ?", (claim_id,)).fetchone()
    assert row["reviewed_at"] is not None


def test_notification_skipped_when_unregistered():
    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM telegram_registrations")
    telegram_bot.send_message_sync("should be skipped, no registered chat")  # must not raise


def test_vetemail_upserts_contact():
    db.init_db()
    telegram_bot.handle_vetemail(AUTHORIZED_USER, "TEST VET CLINIC SYDNEY", "vet@example.com")
    telegram_bot.handle_vetemail(AUTHORIZED_USER, "TEST VET CLINIC SYDNEY", "reception@example.com")
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT email FROM vet_contacts WHERE merchant = 'TEST VET CLINIC SYDNEY'"
        ).fetchall()
    assert len(rows) == 1, "second /vetemail for the same merchant must update, not duplicate"
    assert rows[0]["email"] == "reception@example.com"


def test_vetemail_rejected_for_unauthorized_user():
    db.init_db()
    result = telegram_bot.handle_vetemail(UNAUTHORIZED_USER, "SNEAKY VET", "evil@example.com")
    assert result["ok"] is False
    with db.get_connection() as conn:
        row = conn.execute("SELECT 1 FROM vet_contacts WHERE merchant = 'SNEAKY VET'").fetchone()
    assert row is None


def test_notification_fires_on_info_requested():
    claim_id = _seed_matched_claim("INFO REQ VET")
    with db.get_connection() as conn:
        conn.execute("UPDATE vet_claims SET status = 'info_requested' WHERE id = ?", (claim_id,))
    sent = []
    pipeline.notify_claim_states(send_fn=sent.append)
    pipeline.notify_claim_states(send_fn=sent.append)
    own_sent = [t for t in sent if re.search(rf"#{claim_id}(?!\d)", t)]
    assert len(own_sent) == 1, "info_requested must notify exactly once"
    assert "information" in own_sent[0]


def test_settled_notification_includes_amounts():
    claim_id = _seed_matched_claim("SETTLED VET")
    now = datetime.now(timezone.utc).isoformat()
    with db.get_connection() as conn:
        conn.execute("UPDATE vet_claims SET status = 'settled' WHERE id = ?", (claim_id,))
        conn.execute(
            "INSERT INTO claim_status_events (claim_id, event_type, detail, created_at) VALUES (?, 'settled', ?, ?)",
            (claim_id, json.dumps({"claimed_amount": 100.0, "paid_amount": 80.0}), now),
        )
    sent = []
    pipeline.notify_claim_states(send_fn=sent.append)
    own_sent = [t for t in sent if re.search(rf"#{claim_id}(?!\d)", t)]
    assert len(own_sent) == 1
    assert "100.00" in own_sent[0] and "80.00" in own_sent[0]


def test_sent_command_advances_batch():
    claim_a = _seed_drafted_claim("BATCH VET A", draft_id="draft-batch-1")
    claim_b = _seed_drafted_claim("BATCH VET B", draft_id="draft-batch-1")
    result = telegram_bot.handle_sent(AUTHORIZED_USER, claim_a)
    assert result["ok"] is True
    with db.get_connection() as conn:
        statuses = [
            r["status"]
            for r in conn.execute(
                "SELECT status FROM vet_claims WHERE id IN (?, ?)", (claim_a, claim_b)
            ).fetchall()
        ]
    assert statuses == ["sent", "sent"], "one /sent must advance every claim sharing the draft"


def test_resolved_records_event():
    claim_id = _seed_drafted_claim("RESOLVED VET", draft_id="draft-resolved-1")
    result = telegram_bot.handle_resolved(AUTHORIZED_USER, claim_id)
    assert result["ok"] is True
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM claim_status_events WHERE claim_id = ? AND event_type = 'confirmed_resolved'",
            (claim_id,),
        ).fetchone()
    assert row is not None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"{name} OK")
    print("ALL TESTS PASSED")
