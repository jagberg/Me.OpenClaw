"""Runnable smoke checks — not a full suite. Run with: python tests/test_core.py"""

import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_tmpdir = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_tmpdir, "test.db")
os.environ.setdefault("GEMINI_API_KEY", "")
# Keep the suite hermetic: force every LLM backend unconfigured so extraction
# fails visibly (the intended assertion) instead of making a real API call from
# a key that happens to be in .env. load_dotenv(override=False) won't overwrite
# these explicit empties.
os.environ["CEREBRAS_API_KEY"] = ""
os.environ["GROQ_API_KEY"] = ""
os.environ["OPENAI_API_KEY"] = ""

from openclaw import claim_status, db, gemini, invoice_matching, llm, netbank_csv, reminders, tasks, vet_detection  # noqa: E402
from openclaw.scheduler import scheduler  # noqa: E402


def test_init_db_creates_tables():
    db.init_db()
    with db.get_connection() as conn:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"tasks", "reminders", "llm_calls", "processed_emails"} <= names


def test_rate_limiter_throttles_at_capacity():
    limiter = gemini._RateLimiter(max_per_minute=2)
    limiter.acquire()
    limiter.acquire()
    # simulate the first call happened 59.8s ago so the window nearly resets
    limiter._calls[0] = time.monotonic() - 59.8
    start = time.monotonic()
    limiter.acquire()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.15, "third call within the window should have been throttled"


def test_extract_follow_up_handles_markdown_fenced_json():
    original_extract = llm.extract
    llm.extract = lambda *a, **k: '```json\n{"follow_up_at": "2026-07-10T09:00:00+00:00"}\n```'
    try:
        result = tasks._extract_follow_up("call painter, follow up Friday")
    finally:
        llm.extract = original_extract
    assert result == datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc)


def test_create_task_without_gemini_key_raises_visibly():
    db.init_db()
    try:
        tasks.create_task("call painter", source="chat")
        raised = False
    except llm.LLMUnavailableError:
        raised = True
    assert raised, "create_task must surface LLM failures, not swallow them"


def test_schedule_reminder_marks_due():
    db.init_db()
    scheduler.start()
    with db.get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (description, status, source, created_at) VALUES (?, 'open', 'chat', ?)",
            ("test task", datetime.now(timezone.utc).isoformat()),
        )
        task_id = cur.lastrowid

    when = datetime.now(timezone.utc) + timedelta(seconds=1)
    reminder_id = reminders.schedule_reminder(task_id, when)
    time.sleep(2)

    with db.get_connection() as conn:
        row = conn.execute("SELECT status FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
    assert row["status"] == "due"


def test_netbank_csv_parses_and_dedups_on_reupload():
    db.init_db()
    csv_text = (
        '09/07/2026,"-19.64","EXAMPLE MERCHANT PTY LT  SYDNEY      AUS",""\n'
        '10/07/2026,"-85.00","CITY VET CLINIC          SYDNEY      AUS",""\n'
    )
    rows = netbank_csv.parse(csv_text)
    assert rows[0]["merchant"] == "EXAMPLE MERCHANT PTY LT SYDNEY AUS"
    assert rows[1]["amount"] == -85.00

    inserted_first = netbank_csv.import_rows(rows)
    inserted_second = netbank_csv.import_rows(rows)  # overlapping re-upload, the normal case
    assert inserted_first == 2
    assert inserted_second == 0, "re-upload of the same rows must not duplicate"


def test_netbank_csv_bad_layout_raises_visibly():
    try:
        netbank_csv.parse('09/07/2026,"-19.64","EXTRA","COLUMN","HERE"\n')
        raised = False
    except netbank_csv.CsvParseError:
        raised = True
    assert raised, "unrecognized CSV layout must surface a visible failure, not silently skip"


def test_classify_obvious_vet_merchant_skips_gemini():
    called = []
    original_extract = llm.extract
    llm.extract = lambda *a, **k: called.append(1) or "yes"
    try:
        assert vet_detection.classify("CITY VET CLINIC SYDNEY") is True
    finally:
        llm.extract = original_extract
    assert not called, "obvious vet keyword match must not call Gemini"


def test_classify_obvious_non_vet_merchant_skips_gemini():
    called = []
    original_extract = llm.extract
    llm.extract = lambda *a, **k: called.append(1) or "yes"
    try:
        assert vet_detection.classify("WOOLWORTHS SUPERMARKET", category="groceries") is False
    finally:
        llm.extract = original_extract
    assert not called, "clearly unrelated merchant must not call Gemini"


def test_classify_ambiguous_merchant_triggers_gemini():
    called = []
    original_extract = llm.extract
    llm.extract = lambda *a, **k: called.append(1) or "yes"
    try:
        assert vet_detection.classify("SUBURBAN PET SUPPLIES", category="medical") is True
    finally:
        llm.extract = original_extract
    assert called, "ambiguous medical/pet category with no keyword hit must call Gemini"


def test_reference_regex_old_format():
    # real (redacted) sample: acknowledgement body
    text = "Policy Number: GABR-0305-ELD-00000002 Pet Name: Loki Hi Justin, Claim Received - Claim Number ELD-24-2146 Thank you for taking your time to"
    assert claim_status.extract_reference(text) == "ELD-24-2146"


def test_reference_regex_new_format_from_subject():
    subject = "Petcover Claim DC1-27-5628 SR1 Request for information"
    assert claim_status.extract_reference(subject) == "DC1-27-5628"


def test_reference_regex_does_not_match_bare_policy_number():
    # policy number alone (no "Claim Number"/"Claim Reference" context) must not match
    assert claim_status.extract_reference("Policy Number: GABR-0306-DC1-00000001R") is None


def test_classify_acknowledgement_letter():
    assert claim_status.classify("PetCover - Acknowledgement Letter", "") == "acknowledged"


def test_classify_suspended():
    assert claim_status.classify("Petcover Claim DC1-27-5628 SR1 - Claim suspended", "") == "suspended"


def test_classify_info_requested():
    assert claim_status.classify("GABR-0305-Request for consult note -First Request", "") == "info_requested"


def test_classify_settled():
    assert claim_status.classify("PetCover Letter - Claim Settlement EFT Template", "") == "settled"


def test_classify_declined():
    assert claim_status.classify("ELD-25-2728 - Declined - Invoices over 12 months", "") == "declined"


def test_classify_automatic_reply_is_ignored_not_unclassified():
    assert claim_status.classify("Automatic reply: Loki Goldberg - GOLD094 - Claim -23 Jun 2025 - 1", "") == "ignore"


def test_classify_falls_back_to_body_when_subject_generic():
    assert claim_status.classify("Re: your claim", "we require a copy of consult notes, claim suspended") == "suspended"


def test_extract_settlement_amounts_from_real_pdf_text():
    # real (redacted) sample: settlement PDF text
    text = "Amount Claimed $624.89 Non-Claimable Amount $124.94 Total Payable : $324.97"
    amounts = claim_status.extract_settlement_amounts(text)
    assert amounts == {"claimed_amount": 624.89, "paid_amount": 324.97}


def test_pet_nickname_matches():
    # real pattern: Petcover wrote "Ari" for Aari
    assert claim_status._mentions_pet("claim submitted for treatment provided to Ari.", "Aari")
    assert not claim_status._mentions_pet("treatment provided to Echo", "Aari")


def _insert_sent_claim(conn, pet_id: int, txn_date: str, draft_id: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO bank_transactions (date, amount, merchant, created_at) VALUES (?, -50.0, 'TEST BATCH VET', ?)",
        (txn_date, now),
    )
    txn_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO vet_claims (transaction_id, pet_id, status, draft_id, created_at, updated_at) "
        "VALUES (?, ?, 'sent', ?, ?, ?)",
        (txn_id, pet_id, draft_id, now, now),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def test_batch_claims_correlate_and_learn_reference_together():
    """One submission = several vet_claims sharing a draft_id. The ack (no
    reference known yet, txn dates ~1 year old — no date window) must attach
    to ALL of them and teach them the reference; the settlement must then
    correlate by that reference to all of them too."""
    db.init_db()
    with db.get_connection() as conn:
        aari = conn.execute("SELECT id FROM pets WHERE name='Aari'").fetchone()[0]
        claim_ids = [_insert_sent_claim(conn, aari, f"2025-08-{10 + i:02d}", "draft-batch-1") for i in range(3)]

    claim_status.process_reply(
        "msg-ack-1", "PetCover - Acknowledgement Letter",
        "Pet Name: Aari Hi Justin, Claim Received - Claim Number DC1-99-0001 Thank you",
    )
    with db.get_connection() as conn:
        rows = conn.execute(
            f"SELECT status, petcover_reference FROM vet_claims WHERE id IN ({','.join('?' * 3)})", claim_ids
        ).fetchall()
    assert all(r["status"] == "acknowledged" for r in rows)
    assert all(r["petcover_reference"] == "DC1-99-0001" for r in rows)

    claim_status.process_reply(
        "msg-settle-1", "PetCover Letter - Claim Settlement EFT Template",
        "Claim Reference: DC1-99-0001 Amount Claimed $150.00 Total Payable : $100.00",
    )
    with db.get_connection() as conn:
        rows = conn.execute(
            f"SELECT status FROM vet_claims WHERE id IN ({','.join('?' * 3)})", claim_ids
        ).fetchall()
        settled_events = conn.execute(
            "SELECT count(*) FROM claim_status_events WHERE event_type = 'settled'"
        ).fetchone()[0]
    assert all(r["status"] == "settled" for r in rows)
    assert settled_events == 3


def test_ambiguous_match_never_guessed_then_manually_linked():
    """Two separate submissions for the same pet: a reply naming only the pet
    must be stored unlinked (never guessed), then manual linking attaches it
    and applies the status."""
    db.init_db()
    with db.get_connection() as conn:
        aari = conn.execute("SELECT id FROM pets WHERE name='Aari'").fetchone()[0]
        claim_a = _insert_sent_claim(conn, aari, "2026-01-05", "draft-a")
        _insert_sent_claim(conn, aari, "2026-02-05", "draft-b")

    claim_status.process_reply(
        "msg-ambig-1", "GABR-0306- First request for consult note",
        "We recently received a claim for treatment provided to Ari. Please provide consult notes.",
    )
    with db.get_connection() as conn:
        event = conn.execute(
            "SELECT * FROM claim_status_events WHERE raw_email_id = 'msg-ambig-1'"
        ).fetchone()
        status_a = conn.execute("SELECT status FROM vet_claims WHERE id = ?", (claim_a,)).fetchone()[0]
    assert event["claim_id"] is None, "ambiguous reply must not be attached to any claim"
    assert event["event_type"] == "info_requested"
    assert status_a == "sent", "ambiguous reply must not change any claim's status"

    assert claim_status.link_event(event["id"], 999999) is False, "linking to a nonexistent claim must refuse"
    assert claim_status.link_event(event["id"], claim_a) is True
    with db.get_connection() as conn:
        event = conn.execute("SELECT * FROM claim_status_events WHERE id = ?", (event["id"],)).fetchone()
        status_a = conn.execute("SELECT status FROM vet_claims WHERE id = ?", (claim_a,)).fetchone()[0]
    assert event["claim_id"] == claim_a
    assert status_a == "sent", "manual link must NOT rewrite the claim's status (late-linked old emails must not regress it)"


def test_ceiling_match_and_remainder():
    # surcharge case (real): $580.74 invoice charged as $585.39 — matches, no remainder flag
    assert invoice_matching._within_ceiling(580.74, -585.39)
    assert invoice_matching._unexplained_remainder(580.74, -585.39) is None
    # invoice larger than the charge can't be the right one
    assert not invoice_matching._within_ceiling(600.00, -585.39)
    # split charge (real): $177.50 charge covering a $35 invoice — matches, flags the $142.50 gap
    assert invoice_matching._within_ceiling(35.00, -177.50)
    assert invoice_matching._unexplained_remainder(35.00, -177.50) == 142.50


def test_claimable_amount_filters_routine_care():
    invoice = {
        "amount": 152.50,
        "items": [
            {"description": "C5 2nd Vaccination", "amount": 142.50},
            {"description": "Milbemax Dog Tablet", "amount": 10.00},
        ],
    }
    assert invoice_matching.claimable_amount(invoice) == 0.0
    invoice = {
        "amount": 191.50,
        "items": [
            {"description": "Arthritis - Pentosan Injection Booster", "amount": 45.00},
            {"description": "Previcox 227mg", "amount": 50.00},
            {"description": "C5 Vaccination", "amount": 96.50},
        ],
    }
    assert invoice_matching.claimable_amount(invoice) == 95.00
    # no itemization from extraction — fall back to the invoice total
    assert invoice_matching.claimable_amount({"amount": 45.00, "items": []}) == 45.00


def test_unclassified_reply_never_overwrites_status():
    db.init_db()
    with db.get_connection() as conn:
        aari = conn.execute("SELECT id FROM pets WHERE name='Aari'").fetchone()[0]
        claim_id = _insert_sent_claim(conn, aari, "2026-03-01", "draft-uncls")
        conn.execute(
            "UPDATE vet_claims SET status = 'acknowledged', petcover_reference = 'DC1-88-0001' WHERE id = ?",
            (claim_id,),
        )

    claim_status.process_reply(
        "msg-uncls-1", "Petcover Claim DC1-88-0001 SR2", "A new template we have never seen before."
    )
    with db.get_connection() as conn:
        event = conn.execute("SELECT * FROM claim_status_events WHERE raw_email_id='msg-uncls-1'").fetchone()
        status = conn.execute("SELECT status FROM vet_claims WHERE id = ?", (claim_id,)).fetchone()[0]
    assert event["event_type"] == "unclassified"
    assert event["claim_id"] == claim_id, "unclassified reply with a known reference still links for review"
    assert status == "acknowledged", "unclassified is a review-queue entry, not a lifecycle stage"


def _insert_txn(conn, date, amount, merchant="KINGS VET CLINIC"):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO bank_transactions (date, amount, merchant, vet_flag, created_at) VALUES (?, ?, ?, 1, ?)",
        (date, amount, merchant, now),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _insert_claim(conn, txn_id, pet_id, status, condition=None, claimable=None):
    import json as _json
    now = datetime.now(timezone.utc).isoformat()
    invoice = _json.dumps({"claimable_amount": claimable}) if claimable is not None else None
    conn.execute(
        "INSERT INTO vet_claims (transaction_id, pet_id, condition_text, invoice_data, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (txn_id, pet_id, condition, invoice, status, now, now),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def test_visit_ledger_four_shapes():
    """One entry per vet charge; flat vs split vs no-invoice; excess drained
    across the arthritis batch (all under $150 -> $0 expected); Echo (no
    Petcover excess) flagged unavailable, never guessed."""
    db.init_db()
    with db.get_connection() as conn:
        aari = conn.execute("SELECT id FROM pets WHERE name='Aari'").fetchone()[0]
        echo = conn.execute("SELECT id FROM pets WHERE name='Echo'").fetchone()[0]
        # flat: Aari arthritis, claimable 44.75
        t1 = _insert_txn(conn, "2025-08-08", -44.75)
        _insert_claim(conn, t1, aari, "drafted", "Arthritis", 44.75)
        # two more arthritis charges same year -> batch totals 124.75 (< 150 excess)
        t2 = _insert_txn(conn, "2025-09-26", -45.00)
        _insert_claim(conn, t2, aari, "drafted", "Arthritis", 45.00)
        # split: one $177.50 charge -> Aari arthritis 35.00 + Echo vaccination excluded
        t3 = _insert_txn(conn, "2025-09-11", -177.50)
        _insert_claim(conn, t3, aari, "drafted", "Arthritis", 35.00)
        _insert_claim(conn, t3, echo, "matched", "Vaccination", 0.0)
        # no-invoice: Aari charge, pending_match, no invoice_data
        t4 = _insert_txn(conn, "2025-07-28", -45.00)
        _insert_claim(conn, t4, aari, "pending_match")
        # missing-excess: Echo charge with a real claimable but no policy excess/cap
        t5 = _insert_txn(conn, "2025-12-22", -679.50, "VETWEST")
        _insert_claim(conn, t5, echo, "matched", "Injury", 600.00)

    ledger = claim_status.visit_ledger()

    # one entry per charge, newest first
    assert [e["txn"]["id"] for e in ledger] == [t5, t2, t3, t1, t4]

    by_txn = {e["txn"]["id"]: e for e in ledger}
    assert by_txn[t3]["claim_count"] == 2, "split charge nests both claims under one entry"
    assert by_txn[t1]["claim_count"] == 1

    # arthritis batch (44.75 + 35 + 45 = 124.75) all under the $150 excess -> $0 each
    arth = [c for e in ledger for c in e["claims"] if c["condition_text"] == "Arthritis"]
    assert len(arth) == 3
    assert all(c["expected"]["available"] and c["expected"]["value"] == 0.0 for c in arth)

    # no-invoice claim -> unavailable, not guessed
    noinv = by_txn[t4]["claims"][0]
    assert noinv["status"] == "pending_match"
    assert noinv["expected"]["available"] is False

    # Echo (no excess/cap on file) -> unavailable even with a real claimable
    echo_claim = by_txn[t5]["claims"][0]
    assert echo_claim["claimable"] == 600.00
    assert echo_claim["expected"]["available"] is False


def test_visit_ledger_expected_after_excess_and_settled_actual():
    """Once the batch exceeds the excess, expected = claimable beyond it; a
    settled claim shows Petcover's actual paid, overriding the estimate."""
    db.init_db()
    with db.get_connection() as conn:
        aari = conn.execute("SELECT id FROM pets WHERE name='Aari'").fetchone()[0]
        # single arthritis claim of 200 in a fresh year -> expected 200 - 150 = 50
        t1 = _insert_txn(conn, "2024-03-12", -210.00, "EASTSIDE ANIMAL HOSPITAL")
        c1 = _insert_claim(conn, t1, aari, "settled", "Gastroenteritis", 200.00)
    claim_status._record_event(c1, "settled", "msg-x", {"paid_amount": 124.97})

    ledger = claim_status.visit_ledger()
    claim = next(c for e in ledger for c in e["claims"] if c["id"] == c1)
    # settled actual overrides the estimate
    assert claim["expected"]["value"] == 124.97
    assert claim["expected"]["estimate"] is False


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"{name} OK")
    print("ALL TESTS PASSED")
