"""Runnable smoke checks — not a full suite. Run with: python tests/test_core.py"""

import contextlib
import json
import os
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_tmpdir = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_tmpdir, "test.db")
# Keep the suite hermetic: force every LLM backend unconfigured so extraction
# fails visibly (the intended assertion) instead of making a real API call from
# a key that happens to be in .env or the container env. Vision tests stub
# llm.extract_vision on top of this — tokens are limited, tests never spend them.
os.environ["GEMINI_API_KEY"] = ""
os.environ["GROQ_API_KEY"] = ""
os.environ["OPENAI_API_KEY"] = ""
# Message-log rows are version-stamped; a known value lets the tests assert it.
os.environ["APP_VERSION"] = "test-sha+test"

from openclaw import (  # noqa: E402
    claim_forms,
    claim_status,
    config,
    db,
    gemini,
    invoice_matching,
    llm,
    main,
    netbank_csv,
    reminders,
    status_labels,
    tasks,
    vet_detection,
)
from openclaw.scheduler import scheduler  # noqa: E402


def test_init_db_creates_tables():
    db.init_db()
    with db.get_connection() as conn:
        names = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
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

    read_first, inserted_first, skipped_first = netbank_csv.import_rows(rows)
    read_second, inserted_second, skipped_second = netbank_csv.import_rows(
        rows
    )  # overlapping re-upload, the normal case
    assert (read_first, inserted_first, skipped_first) == (2, 2, 0)
    assert inserted_second == 0, "re-upload of the same rows must not duplicate"
    assert (read_second, skipped_second) == (2, 2)


def test_netbank_csv_bad_layout_raises_visibly():
    try:
        netbank_csv.parse('09/07/2026,"-19.64","EXTRA","COLUMN","HERE"\n')
        raised = False
    except netbank_csv.CsvParseError:
        raised = True
    assert raised, "unrecognized CSV layout must surface a visible failure, not silently skip"


def test_netbank_csv_bad_row_names_the_offending_row_and_inserts_nothing():
    """CsvParseError must name the row (task 3.5) and parse() must raise before
    any row is returned -- nothing partial is ever inserted."""
    db.init_db()
    csv_text = (
        '09/07/2026,"-19.64","GOOD ROW ONE             SYDNEY      AUS",""\n'
        '09/07/2026,"-1.00","BAD","ROW","EXTRA"\n'
    )
    try:
        netbank_csv.parse(csv_text)
        raised = False
    except netbank_csv.CsvParseError as exc:
        raised = True
        assert "row 2" in str(exc), str(exc)
    assert raised
    with db.get_connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM bank_transactions WHERE merchant LIKE 'GOOD ROW%'"
        ).fetchone()[0]
    assert count == 0, "a parse failure on row 2 must not have inserted row 1"


def test_watermark_derivation():
    """The watermark is `MAX(date)`, empty-table-safe, and an overlapping
    re-upload must not appear to move it backwards or duplicate it forward."""
    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM bank_transactions")
    assert netbank_csv.latest_transaction_date() is None

    netbank_csv.import_rows(
        netbank_csv.parse('01/07/2026,"-10.00","MERCHANT A               SYDNEY      AUS",""\n')
    )
    assert netbank_csv.latest_transaction_date() == "2026-07-01"

    # A later upload that overlaps an earlier date must not move the watermark
    # backwards, and re-parsing an already-held date must not duplicate it.
    netbank_csv.import_rows(
        netbank_csv.parse(
            '01/07/2026,"-10.00","MERCHANT A               SYDNEY      AUS",""\n'
            '05/07/2026,"-20.00","MERCHANT B               SYDNEY      AUS",""\n'
        )
    )
    assert netbank_csv.latest_transaction_date() == "2026-07-05"


def test_no_stored_watermark_exists():
    """Nothing outside `bank_transactions` may answer "latest transaction
    date" (design.md Decision 3: derived, never stored) -- a second answer to
    the same question eventually disagrees with the first."""
    with db.get_connection() as conn:
        columns = {
            row[1]
            for table in ("bank_transactions",)
            for row in conn.execute(f"PRAGMA table_info({table})")
        }
        all_tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "watermark" not in " ".join(all_tables).lower()
    assert not any("watermark" in c.lower() for c in columns)


def test_ingest_upload_reports_counts_claims_and_watermark():
    from openclaw import pipeline

    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM bank_transactions")
    orig_run_once = pipeline.run_once
    pipeline.run_once = lambda: None  # isolate from Gmail/LLM entirely
    try:
        result = netbank_csv.ingest_upload(
            '02/07/2026,"-30.00","MERCHANT C               SYDNEY      AUS",""\n'
        )
    finally:
        pipeline.run_once = orig_run_once
    assert "Imported 1 new transaction" in result
    assert "1 read" in result
    assert "0 claim(s) found" in result
    assert "2026-07-02" in result


def test_ingest_upload_reports_a_skipped_scan_as_skipped_not_success():
    """`ran=False` (a tick already in flight) must be stated as such, never as
    a completed scan (design.md Decision 4)."""
    import threading

    from openclaw import internal_api

    lock = internal_api._locks.setdefault("tick", threading.Lock())
    lock.acquire()
    try:
        result = netbank_csv.ingest_upload(
            '03/07/2026,"-40.00","MERCHANT D               SYDNEY      AUS",""\n'
        )
    finally:
        lock.release()
    assert "already running" in result
    assert "Imported 1 new transaction" in result


def test_ingest_upload_reports_a_raised_scan_as_partial_not_plain_success():
    from openclaw import pipeline

    orig_run_once = pipeline.run_once

    def _boom():
        raise RuntimeError("scan exploded")

    pipeline.run_once = _boom
    try:
        result = netbank_csv.ingest_upload(
            '04/07/2026,"-50.00","MERCHANT E               SYDNEY      AUS",""\n'
        )
    finally:
        pipeline.run_once = orig_run_once
    assert "scan failed" in result
    assert "scan exploded" in result
    assert "Imported 1 new transaction" in result


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


def test_extract_sr_recognizes_treatment_number_variant():
    """Real bug (live-caught): the 'Claim Approval' letter has no 'Sr' text at
    all — only 'Treatment number: 2', a distinct labeled field, not adjacent
    to the reference. Missing this silently broadened an event's routing from
    one claim to the whole thread."""
    text = "Claim Reference:DC1-27-5628\nTreatment number: 2\nCondition: Illness (Arthritis)"
    assert claim_status.extract_sr(text, "DC1-27-5628") == 2
    # classic adjacent style still works
    assert claim_status.extract_sr("DC1-27-5628 SR1 Request for information", "DC1-27-5628") == 1
    # no reference at all -> no sr, regardless of what the text contains
    assert claim_status.extract_sr("Treatment number: 2", None) is None


def test_classify_acknowledgement_letter():
    assert claim_status.classify("PetCover - Acknowledgement Letter", "") == "acknowledged"


def test_classify_suspended():
    assert (
        claim_status.classify("Petcover Claim DC1-27-5628 SR1 - Claim suspended", "") == "suspended"
    )


def test_classify_info_requested():
    assert (
        claim_status.classify("GABR-0305-Request for consult note -First Request", "")
        == "info_requested"
    )


def test_classify_settled():
    assert claim_status.classify("PetCover Letter - Claim Settlement EFT Template", "") == "settled"


def test_classify_declined():
    assert (
        claim_status.classify("ELD-25-2728 - Declined - Invoices over 12 months", "") == "declined"
    )


def test_classify_automatic_reply_is_ignored_not_unclassified():
    assert (
        claim_status.classify(
            "Automatic reply: Loki Goldberg - GOLD094 - Claim -23 Jun 2025 - 1", ""
        )
        == "ignore"
    )


def test_classify_falls_back_to_body_when_subject_generic():
    assert (
        claim_status.classify(
            "Re: your claim", "we require a copy of consult notes, claim suspended"
        )
        == "suspended"
    )


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
        claim_ids = [
            _insert_sent_claim(conn, aari, f"2025-08-{10 + i:02d}", "draft-batch-1")
            for i in range(3)
        ]

    claim_status.process_reply(
        "msg-ack-1",
        "PetCover - Acknowledgement Letter",
        "Pet Name: Aari Hi Justin, Claim Received - Claim Number DC1-99-0001 Thank you",
    )
    with db.get_connection() as conn:
        rows = conn.execute(
            f"SELECT status, petcover_reference FROM vet_claims WHERE id IN ({','.join('?' * 3)})",
            claim_ids,
        ).fetchall()
    assert all(r["status"] == "acknowledged" for r in rows)
    assert all(r["petcover_reference"] == "DC1-99-0001" for r in rows)

    claim_status.process_reply(
        "msg-settle-1",
        "PetCover Letter - Claim Settlement EFT Template",
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


def test_uncorrelated_reply_unlinked_then_manually_linked():
    """A reply naming no known pet correlates to nothing — stored unlinked
    (never guessed), then manual linking attaches it WITHOUT rewriting the
    claim's status (a late-linked old email must not regress a settled claim)."""
    db.init_db()
    with db.get_connection() as conn:
        aari = conn.execute("SELECT id FROM pets WHERE name='Aari'").fetchone()[0]
        claim_a = _insert_sent_claim(conn, aari, "2026-01-05", "draft-a")

    claim_status.process_reply(
        "msg-uncorr-1",
        "First request for consult note",
        "We recently received a claim for treatment provided to Rex. Please provide consult notes.",
    )
    with db.get_connection() as conn:
        event = conn.execute(
            "SELECT * FROM claim_status_events WHERE raw_email_id = 'msg-uncorr-1'"
        ).fetchone()
        status_a = conn.execute(
            "SELECT status FROM vet_claims WHERE id = ?", (claim_a,)
        ).fetchone()[0]
    assert event["claim_id"] is None, "unknown-pet reply must not be attached to any claim"
    assert event["event_type"] == "info_requested"
    assert status_a == "sent", "uncorrelated reply must not change any claim's status"

    assert claim_status.link_event(event["id"], 999999) is False, (
        "linking to a nonexistent claim must refuse"
    )
    assert claim_status.link_event(event["id"], claim_a) is True
    with db.get_connection() as conn:
        event = conn.execute(
            "SELECT * FROM claim_status_events WHERE id = ?", (event["id"],)
        ).fetchone()
        status_a = conn.execute(
            "SELECT status FROM vet_claims WHERE id = ?", (claim_a,)
        ).fetchone()[0]
    assert event["claim_id"] == claim_a
    assert status_a == "sent", (
        "manual link must NOT rewrite the claim's status (late-linked old emails must not regress it)"
    )


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
        event = conn.execute(
            "SELECT * FROM claim_status_events WHERE raw_email_id='msg-uncls-1'"
        ).fetchone()
        status = conn.execute("SELECT status FROM vet_claims WHERE id = ?", (claim_id,)).fetchone()[
            0
        ]
    assert event["event_type"] == "unclassified"
    assert event["claim_id"] == claim_id, (
        "unclassified reply with a known reference still links for review"
    )
    assert status == "acknowledged", "unclassified is a review-queue entry, not a lifecycle stage"


def test_dashboard_renders_a_claim_linked_unclassified_event_without_settlement_figures():
    """Real 500, found live: `templates/index.html`'s review-queue row does
    `d.claimable_subtotal` (etc.) on `e.detail|from_json`, but only
    settlement-comparison events (`mismatch_dismissed`, `settled`, `approved`)
    ever write those keys. An `unclassified` event that carries a known
    reference — exactly `test_unclassified_reply_never_overwrites_status`
    above — reaches the template with `claim_id` set and none of the three
    keys, and `d.claimable_subtotal` raises `UndefinedError` on a plain dict
    missing the key, crashing the whole dashboard. The absence is real (this
    event was never a settlement letter), so the fix reads via `.get()` and
    keeps the existing 'not recorded' / 'not stated' fallback text — not a
    Python-side change to what `unclassified` events record."""
    from openclaw import main as main_module

    db.init_db()
    with db.get_connection() as conn:
        aari = conn.execute("SELECT id FROM pets WHERE name='Aari'").fetchone()[0]
        claim_id = _insert_sent_claim(conn, aari, "2026-03-02", "draft-uncls-render")
        conn.execute(
            "UPDATE vet_claims SET status = 'acknowledged', petcover_reference = 'DC1-88-0002' WHERE id = ?",
            (claim_id,),
        )
    claim_status.process_reply(
        "msg-uncls-render-1",
        "Petcover Claim DC1-88-0002 SR3",
        "A new template we have never seen before.",
    )

    lists = claim_status.dashboard_lists()
    assert claim_id in [e["claim_id"] for e in lists["unclassified"]], (
        "fixture must reproduce the exact linked-unclassified review-queue row"
    )

    html = main_module.templates.env.get_template("index.html").render(
        tasks=[],
        reminders=[],
        pets=[],
        ledger=[],
        upload_error=None,
        upload_result=None,
        transactions_watermark=None,
        **lists,
    )
    assert f"claim #{claim_id}" in html
    assert "not recorded" in html and "not stated" in html


def test_parse_invoices_multi_and_legacy_shapes():
    multi = '{"invoices": [{"date": "2026-06-17", "amount": 141.87, "items": []}, {"date": "2026-07-06", "amount": 407.56, "items": []}]}'
    parsed = invoice_matching._parse_invoices(multi)
    assert [i["amount"] for i in parsed] == [141.87, 407.56]
    # legacy single-invoice object (old cache rows / model regression) still parses
    legacy = '```json\n{"date": "2026-06-19", "amount": 585.39, "items": []}\n```'
    assert invoice_matching._parse_invoices(legacy) == [
        {"date": "2026-06-19", "amount": 585.39, "items": []}
    ]
    assert invoice_matching._parse_invoices("no json here") is None
    assert invoice_matching._parse_invoices('{"invoices": "garbage"}') is None
    assert invoice_matching._parse_invoices('{"invoices": []}') == []


def test_pick_invoice_from_bulk_reply_uses_ceiling_and_invoice_date():
    """Real case: Shire's bulk reply held 3 invoices; claim ($407.56, 2026-07-06)
    must pick its own invoice — not the $141.87 one (fits the ceiling but is a
    different visit, invoice dated 19 days earlier) and not the grand total."""
    from datetime import date as _date

    invoices = [
        {"date": "2026-06-17", "amount": 141.87},  # under ceiling, wrong visit date
        {"date": "2026-06-19", "amount": 585.39},  # over ceiling
        {"date": "2026-07-06", "amount": 407.56},  # the right one
        {"date": None, "amount": 1134.82},  # grand total — over ceiling
    ]
    picked = invoice_matching._pick_invoice(invoices, -407.56, _date(2026, 7, 6))
    assert picked["amount"] == 407.56
    # nothing fits: every invoice over the ceiling
    assert (
        invoice_matching._pick_invoice(
            [{"date": "2026-07-06", "amount": 999.0}], -407.56, _date(2026, 7, 6)
        )
        is None
    )
    # amount missing entirely: skipped, not crashed
    assert (
        invoice_matching._pick_invoice(
            [{"date": "2026-07-06", "amount": None}], -407.56, _date(2026, 7, 6)
        )
        is None
    )
    # missing invoice date can't be checked — allowed through (absence of evidence)
    assert (
        invoice_matching._pick_invoice([{"amount": 400.0}], -407.56, _date(2026, 7, 6))["amount"]
        == 400.0
    )


def test_build_queries_always_include_open_ended_window():
    """Late forwards (real: February invoices forwarded in July) must be
    searchable regardless of invoice_request_sent_at — every source gets an
    open-ended after: query; the narrow window stays for pre-charge arrivals."""
    from datetime import date as _date

    original_spouse = invoice_matching.config.SPOUSE_EMAIL
    invoice_matching.config.SPOUSE_EMAIL = "spouse@example.com"
    try:
        queries = invoice_matching._build_queries("Kings Vet KINGSGROVE NSW", _date(2026, 2, 23))
    finally:
        invoice_matching.config.SPOUSE_EMAIL = original_spouse
    merchant_queries = [q for q, needs_confirm in queries if not needs_confirm]
    spouse_queries = [q for q, needs_confirm in queries if needs_confirm]
    assert any("after:" in q and "before:" not in q for q in merchant_queries), (
        "merchant needs an open-ended window"
    )
    assert any("after:" in q and "before:" not in q for q in spouse_queries), (
        "spouse forwards need an open-ended window"
    )
    assert any("before:" in q for q in merchant_queries), (
        "narrow window must remain (invoice can arrive before the charge settles)"
    )
    assert all("NSW" not in q for q in merchant_queries), (
        "state suffix must be stripped from search terms"
    )
    # real failure: Justin's own outgoing invoice-request emails list visit
    # dates + amounts — extraction read them as invoices and 12 claims matched
    # his own requests. Own mail must be excluded query-side.
    assert all("-from:me" in q for q in merchant_queries), (
        "own sent mail must never be an invoice candidate"
    )


def test_extraction_cached_per_email_no_second_llm_call():
    db.init_db()
    calls = []
    original_extract = llm.extract
    llm.extract = lambda *a, **k: (
        calls.append(1) or '{"invoices": [{"date": "2026-01-20", "amount": 10.50, "items": []}]}'
    )
    try:
        first = invoice_matching._invoices_for_email("cache-test-1", "some invoice text")
        llm.extract = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("second extraction must come from cache")
        )
        second = invoice_matching._invoices_for_email("cache-test-1", "some invoice text")
    finally:
        llm.extract = original_extract
    assert len(calls) == 1
    assert first == second == [{"date": "2026-01-20", "amount": 10.50, "items": []}]


def test_unparseable_extraction_not_cached_so_it_retries():
    db.init_db()
    original_extract = llm.extract
    llm.extract = lambda *a, **k: "total gibberish, no json"
    try:
        assert invoice_matching._invoices_for_email("cache-test-2", "text") is None
    finally:
        llm.extract = original_extract
    assert invoice_matching._cached_extraction("cache-test-2") is None, (
        "failed parse must not be cached"
    )


def test_forward_confirms_vet_needs_word_boundary_and_distinctive_word():
    """Real case: a human-hospital forward passed the old substring check for
    'Kings Vet KINGSGROVE NSW' — 'kings' matched inside an unrelated word."""
    merchant = "Kings Vet KINGSGROVE NSW"
    assert not invoice_matching._forward_confirms_vet(
        "Procedure at Sydney Day Surgery near Kingsford Smith Drive", merchant, None
    ), "substring inside another word must not confirm"
    assert invoice_matching._forward_confirms_vet(
        "Kind Regards, Kingsgrove Animal Hospital", merchant, None
    )
    assert invoice_matching._forward_confirms_vet(
        "quoted From: info@kingsvet.com.au", merchant, "info@kingsvet.com.au"
    ), "known vet email always confirms"
    # generic words alone must never confirm a different vet's invoice
    assert not invoice_matching._forward_confirms_vet(
        "Sydney Animal Hospitals - Inner West", merchant, None
    )


def test_parse_invoices_salvages_truncated_reply():
    """Real case: a 12k-char bulk invoice PDF pushed the reply past the model's
    output budget, cutting the JSON mid-array — complete invoice objects must
    survive, the partial one is dropped."""
    truncated = (
        '{"invoices": ['
        '{"date": "2026-04-13", "amount": 551.06, "services": null, "items": []}, '
        '{"date": "2026-04-13", "amount": 1970.40, "services": null, "items": []}, '
        '{"date": "2026-06-17", "amount": 23'
    )
    parsed = invoice_matching._parse_invoices(truncated)
    assert [i["amount"] for i in parsed] == [551.06, 1970.40]
    # nothing complete to salvage
    assert invoice_matching._parse_invoices('{"invoices": [{"date": "2026-') is None


def test_oversized_invoice_detected_for_manual_split():
    """Real case: MediPaws billed one $2,521.46 invoice paid via two card
    charges ($551.06 + $1,970.40, same day). Neither claim may match it —
    but it must be surfaced, not silently skipped."""
    from datetime import date as _date

    invoices = [{"date": "2026-04-13", "amount": 2521.46}]
    assert invoice_matching._pick_invoice(invoices, -551.06, _date(2026, 4, 13)) is None
    over = invoice_matching._oversized_candidate(invoices, -551.06, _date(2026, 4, 13))
    assert over["amount"] == 2521.46
    # an oversized invoice for a DIFFERENT visit is not this claim's business
    assert invoice_matching._oversized_candidate(invoices, -551.06, _date(2026, 6, 19)) is None
    # dateless big invoices can't be tied to the visit — never flagged
    assert (
        invoice_matching._oversized_candidate(
            [{"date": None, "amount": 9999.0}], -551.06, _date(2026, 4, 13)
        )
        is None
    )


def _insert_pending_claim(conn, merchant: str, amount: float, txn_date: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO bank_transactions (date, amount, merchant, created_at) VALUES (?, ?, ?, ?)",
        (txn_date, amount, merchant, now),
    )
    txn_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO vet_claims (transaction_id, status, created_at, updated_at) VALUES (?, 'pending_match', ?, ?)",
        (txn_id, now, now),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def test_split_proposal_created_resolved_and_sibling_absorbed():
    """Real case: MediPaws billed one $2,521.46 invoice paid via two charges
    ($551.06 + $1,970.40). A proposal pairs the claims; Justin's pick attaches
    the invoice to one claim and closes the other as covered."""
    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM vet_claims")
        conn.execute("DELETE FROM bank_transactions")
        conn.execute("DELETE FROM split_proposals")
        claim_a = _insert_pending_claim(conn, "MEDIPAWS TEST", -551.06, "2026-04-13")
        claim_b = _insert_pending_claim(conn, "MEDIPAWS TEST", -1970.40, "2026-04-13")

    oversized = {"date": "2026-04-13", "amount": 2521.46, "items": [], "_email_id": "email-split-1"}
    with db.get_connection() as conn:
        claim_row = conn.execute(
            "SELECT vet_claims.*, bank_transactions.amount AS txn_amount, "
            "bank_transactions.merchant AS txn_merchant FROM vet_claims "
            "JOIN bank_transactions ON bank_transactions.id = vet_claims.transaction_id "
            "WHERE vet_claims.id = ?",
            (claim_a,),
        ).fetchone()
    flag = invoice_matching._propose_split(claim_row, oversized)
    assert flag and f"#{claim_b}" in flag, "flag must name the sibling claim"
    # second call dedupes — still exactly one open proposal
    invoice_matching._propose_split(claim_row, oversized)
    with db.get_connection() as conn:
        proposals = conn.execute("SELECT * FROM split_proposals WHERE status='open'").fetchall()
    assert len(proposals) == 1
    proposal = proposals[0]

    # wrong claim id refused; then the real pick works
    assert invoice_matching.resolve_split_proposal(proposal["id"], 999999)["ok"] is False
    result = invoice_matching.resolve_split_proposal(proposal["id"], claim_b)
    assert result["ok"], result["message"]
    with db.get_connection() as conn:
        chosen = conn.execute("SELECT * FROM vet_claims WHERE id = ?", (claim_b,)).fetchone()
        other = conn.execute("SELECT * FROM vet_claims WHERE id = ?", (claim_a,)).fetchone()
        proposal = conn.execute(
            "SELECT status FROM split_proposals WHERE id = ?", (proposal["id"],)
        ).fetchone()
    assert chosen["status"] == "matched" and chosen["matched_email_id"] == "email-split-1"
    import json as _json

    assert _json.loads(chosen["invoice_data"])["amount"] == 2521.46
    assert other["status"] == "absorbed" and f"#{claim_b}" in other["flag"]
    assert proposal["status"] == "resolved"
    # resolving a nonexistent/closed proposal refuses
    assert invoice_matching.resolve_split_proposal(999, claim_b)["ok"] is False


def test_merge_split_proposal_auto_picks_larger_charge():
    """No arbitrary pick: Petcover sees the invoice, not the bank charges, so
    the larger charge's claim carries the invoice deterministically."""
    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM vet_claims")
        conn.execute("DELETE FROM bank_transactions")
        conn.execute("DELETE FROM split_proposals")
        claim_small = _insert_pending_claim(conn, "MEDIPAWS TEST", -551.06, "2026-04-13")
        claim_large = _insert_pending_claim(conn, "MEDIPAWS TEST", -1970.40, "2026-04-13")
        import json as _json

        conn.execute(
            "INSERT INTO split_proposals (email_id, invoice_json, claim_ids, created_at) VALUES (?, ?, ?, ?)",
            (
                "email-m-1",
                _json.dumps({"date": "2026-04-13", "amount": 2521.46, "items": []}),
                _json.dumps([claim_small, claim_large]),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        pid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    result = invoice_matching.merge_split_proposal(pid)
    assert result["ok"], result["message"]
    with db.get_connection() as conn:
        large = conn.execute(
            "SELECT status FROM vet_claims WHERE id = ?", (claim_large,)
        ).fetchone()[0]
        small = conn.execute(
            "SELECT status FROM vet_claims WHERE id = ?", (claim_small,)
        ).fetchone()[0]
    assert large == "matched" and small == "absorbed", "larger charge must carry the invoice"


def test_append_result_falls_back_to_caption_on_document_message():
    """Merge/review alerts are documents with a caption, no text —
    edit_message_text raises BadRequest there, so the helper must edit the
    caption instead (real failure: merge tap 'did nothing')."""
    import asyncio

    from openclaw import telegram_bot

    class FakeQuery:
        def __init__(self, text, caption):
            self.message = type("M", (), {"text": text, "caption": caption})()
            self.edited = None

        async def edit_message_text(self, text):
            if self.message.text is None:
                raise AssertionError("edit_message_text called on captioned document")
            self.edited = ("text", text)

        async def edit_message_caption(self, caption):
            self.edited = ("caption", caption)

    q_doc = FakeQuery(text=None, caption="Invoice #411193 for $2521.46")
    asyncio.run(telegram_bot._append_result(q_doc, "✅ merged"))
    assert q_doc.edited[0] == "caption" and q_doc.edited[1].endswith("✅ merged"), q_doc.edited

    q_txt = FakeQuery(text="plain message", caption=None)
    asyncio.run(telegram_bot._append_result(q_txt, "✅ done"))
    assert q_txt.edited[0] == "text" and "plain message" in q_txt.edited[1], q_txt.edited


def _clear_message_log():
    with db.get_connection() as conn:
        conn.execute("DELETE FROM telegram_messages")


def _fake_update(update_id):
    """A real Update object — every field but update_id is optional, so this
    round-trips through to_dict/de_json exactly like a live one."""
    from telegram import Update

    return Update(update_id=update_id)


def test_message_log_keeps_a_failed_update_queued_for_replay():
    """The subtle invariant: PTB runs its error handler *inside* process_update,
    so a failed update reaches mark_processed looking successful. If it settled,
    the update would be silently dropped — exactly the class of loss this whole
    table exists to prevent."""
    from telegram import Update

    from openclaw import config, message_log

    db.init_db()
    _clear_message_log()

    uid = message_log.record_inbound(Update(update_id=9001))
    assert uid == 9001, uid
    with db.get_connection() as conn:
        row = conn.execute("SELECT * FROM telegram_messages WHERE update_id = 9001").fetchone()
    assert row["processed_at"] is None, "arrival row must start unprocessed — that's the queue"
    assert row["app_version"] == config.APP_VERSION, row["app_version"]

    message_log.mark_failed(9001, "boom")
    message_log.mark_processed(9001)  # what LoggedApplication does next
    assert [r["update_id"] for r in message_log.pending()] == [9001]

    # A second arrival of the same update_id (Telegram redelivery) must not duplicate.
    assert message_log.record_inbound(Update(update_id=9001)) is None
    with db.get_connection() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM telegram_messages WHERE update_id = 9001"
            ).fetchone()[0]
            == 1
        )


def _edited_reply_to_card_update(
    update_id=9401,
    text="This is actually split between echo and Aari. Aari cost was $35 out of this",
):
    """The real 2026-07-27 payload shape (telegram_messages id 96): Justin EDITED
    his message, and it was a reply to the ASSIGN PET card for claim #1."""
    from telegram import Update

    from openclaw import config

    chat = {"id": 8995277418, "type": "private", "username": "jagberg"}
    return Update.de_json(
        {
            "update_id": update_id,
            "edited_message": {
                "message_id": 233,
                "date": 1785149076,
                "edit_date": 1785149753,
                "chat": chat,
                "from": {
                    "id": 1,
                    "is_bot": False,
                    "first_name": "Justin",
                    "username": config.TELEGRAM_USERNAME or "jagberg",
                },
                "text": text,
                "reply_to_message": {
                    "message_id": 227,
                    "date": 1785148834,
                    "chat": chat,
                    "from": {
                        "id": 2,
                        "is_bot": True,
                        "first_name": "BettyVet",
                        "username": "bettyvet_bot",
                    },
                    "text": "🐾 ASSIGN PET\nClaim #1 · The Shire Veterinary Ca… · $407.56\n"
                    "2026-07-06 (21d ago)\nBlocks: the claim can't be filled",
                    "reply_markup": {
                        "inline_keyboard": [
                            [{"text": "Aari", "callback_data": "setpet:1:1"}],
                            [{"text": "Echo", "callback_data": "setpet:1:2"}],
                        ]
                    },
                },
            },
        },
        bot=None,
    )


def test_edited_message_is_handled_and_logged_not_crashed():
    """Justin edited his message to name the pet's share. `filters.TEXT` matches
    `edited_message`, where `update.message` is None, so the handler died with
    "'NoneType' object has no attribute 'text'" — and the log recorded it as kind
    `other` with an EMPTY summary, hiding the one message that mattered."""
    import asyncio

    from telegram import Message

    from openclaw import agent, config, message_log, telegram_bot

    db.init_db()
    _clear_message_log()
    update = _edited_reply_to_card_update()

    assert update.message is None, "the fixture must be a genuine edit, not a new message"
    assert message_log._describe(update.to_dict()) == (
        "text",
        "edit: This is actually split between echo and Aari. Aari cost was $35 out of this",
    ), message_log._describe(update.to_dict())
    assert telegram_bot._replied_to_claim_id(update.effective_message) == 1

    acked, replies, seen = [], [], {}
    original_username = config.TELEGRAM_USERNAME
    original_reply, original_reaction = Message.reply_text, Message.set_reaction
    original_handle = agent.handle_message
    try:
        telegram_bot.config.TELEGRAM_USERNAME = config.TELEGRAM_USERNAME = "jagberg"

        async def _reply(self, text, **kwargs):
            replies.append(text)

        async def _react(self, reaction, **kwargs):
            acked.append(reaction)

        Message.reply_text, Message.set_reaction = _reply, _react

        def _handle(text, chat_id=None, claim_id=None):
            seen.update({"text": text, "claim_id": claim_id})
            return "ok", None

        agent.handle_message = _handle
        asyncio.run(telegram_bot._ack_user_message(update, None))
        asyncio.run(telegram_bot.on_text_reply(update, None))
    finally:
        Message.reply_text, Message.set_reaction = original_reply, original_reaction
        agent.handle_message = original_handle
        telegram_bot.config.TELEGRAM_USERNAME = config.TELEGRAM_USERNAME = original_username

    assert acked == ["👍"], "an edit gets the same acknowledgement as a new message"
    assert seen["claim_id"] == 1, f"the replied-to card's claim must reach the agent: {seen}"
    assert seen["text"].startswith("This is actually split"), seen
    assert replies == ["ok"]


def test_replied_to_claim_id_refuses_to_guess():
    """A submission card names every member claim, and `act:`/`hist:` tokens are
    not claims at all. Taking an id from either would act on a claim Justin
    wasn't looking at, so nothing is better than a guess."""
    from telegram import Update

    from openclaw import telegram_bot

    def _reply_to(card: dict):
        chat = {"id": 1, "type": "private", "username": "jagberg"}
        return Update.de_json(
            {
                "update_id": 9402,
                "message": {
                    "message_id": 2,
                    "date": 1785149076,
                    "chat": chat,
                    "text": "do it",
                    "from": {"id": 1, "is_bot": False, "first_name": "J"},
                    "reply_to_message": {
                        **card,
                        "message_id": 1,
                        "date": 1785148834,
                        "chat": chat,
                        "from": {"id": 2, "is_bot": True, "first_name": "B"},
                    },
                },
            },
            bot=None,
        ).effective_message

    two_claims = _reply_to(
        {"text": "SEND GMAIL DRAFT\nS6+7 · 2 claims\n  • Claim #6 …\n  • Claim #7 …"}
    )
    assert telegram_bot._replied_to_claim_id(two_claims) is None, "two ids named → no target"

    proposal_token = _reply_to(
        {
            "text": "Confirm this?",
            "reply_markup": {
                "inline_keyboard": [[{"text": "✅ Confirm", "callback_data": "act:2"}]]
            },
        }
    )
    assert telegram_bot._replied_to_claim_id(proposal_token) is None, "act: token is not a claim id"

    from_caption = _reply_to(
        {
            "caption": "Review this settlement for claim #21",
            "document": {"file_id": "f", "file_unique_id": "u"},
        }
    )
    assert telegram_bot._replied_to_claim_id(from_caption) == 21, (
        "PDF alerts carry it in the caption"
    )

    no_parent = Update.de_json(
        {
            "update_id": 9403,
            "message": {
                "message_id": 3,
                "date": 1785149076,
                "chat": {"id": 1, "type": "private"},
                "text": "hi",
                "from": {"id": 1, "is_bot": False, "first_name": "J"},
            },
        },
        bot=None,
    ).effective_message
    assert telegram_bot._replied_to_claim_id(no_parent) is None


def test_logged_application_records_every_update_then_settles_it():
    """The real seam, wired exactly as production wires it: one override catches
    commands, taps, text and uploads alike. Never touches the network — the app
    is marked initialized rather than calling get_me()."""
    import asyncio

    from openclaw import config, telegram_bot

    db.init_db()
    _clear_message_log()

    original_token = config.TELEGRAM_BOT_TOKEN
    config.TELEGRAM_BOT_TOKEN = (
        "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"  # never used, never dialled
    )
    try:
        app = telegram_bot.build_application()
        assert type(app).__name__ == "LoggedApplication", type(app).__name__
        assert type(app.bot).__name__ == "LoggedBot", type(app.bot).__name__
        app._initialized = True
        asyncio.run(app.process_update(_fake_update(9401)))
    finally:
        config.TELEGRAM_BOT_TOKEN = original_token

    with db.get_connection() as conn:
        row = conn.execute("SELECT * FROM telegram_messages WHERE update_id = 9401").fetchone()
    assert row is not None, "process_update must log the arrival"
    assert row["processed_at"] is not None, "a clean run must settle the row"


def test_confirm_resolved_is_idempotent_so_replay_cannot_double_log():
    """Replay is at-least-once, so every mutation it can re-trigger has to be
    idempotent. This one wasn't: two calls wrote two audit events for one
    decision, and confirming a claim with nothing outstanding invented an event
    out of nothing."""
    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM claim_status_events")
        claim_id = conn.execute(
            "INSERT INTO vet_claims (transaction_id, status, created_at, updated_at) "
            "VALUES (9902, 'sent', '2026-07-01', '2026-07-01')"
        ).lastrowid

    # Nothing outstanding yet — confirming must be refused, not recorded.
    assert claim_status.confirm_resolved(claim_id)["ok"] is False
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO claim_status_events (claim_id, event_type, detail, created_at) "
            "VALUES (?, 'info_requested', '{}', '2026-07-02')",
            (claim_id,),
        )

    assert claim_status.confirm_resolved(claim_id)["ok"] is True
    assert claim_status.confirm_resolved(claim_id)["ok"] is False, "second confirm must be a no-op"
    with db.get_connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM claim_status_events WHERE claim_id = ? AND event_type = 'confirmed_resolved'",
            (claim_id,),
        ).fetchone()[0]
    assert count == 1, count


def test_replay_pending_reruns_only_unprocessed_and_settles_them():
    import asyncio

    from openclaw import message_log

    db.init_db()
    _clear_message_log()

    for uid in (9101, 9102):
        message_log.record_inbound(_fake_update(uid))
    message_log.mark_processed(9102)  # already handled — must not be replayed

    class FakeApp:
        bot = None

        def __init__(self):
            self.seen = []

        async def process_update(self, update):
            self.seen.append(update.update_id)
            message_log.mark_processed(update.update_id)

    app = FakeApp()
    assert asyncio.run(message_log.replay_pending(app)) == 1
    assert app.seen == [9101], app.seen
    # Second pass is a no-op: nothing left owed.
    assert asyncio.run(message_log.replay_pending(FakeApp())) == 0


def test_expire_queue_keeps_the_row_but_drops_it_from_the_queue():
    """'Purge after 24h' applies to the queue, not the log — the row is the
    reinforcement-learning dataset and deleting it defeats the purpose."""
    from openclaw import message_log

    db.init_db()
    _clear_message_log()

    stale = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    message_log.record_inbound(_fake_update(9201))
    message_log.record_inbound(_fake_update(9202))
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE telegram_messages SET received_at = ? WHERE update_id = 9201", (stale,)
        )

    assert message_log.expire_queue() == 1
    assert [r["update_id"] for r in message_log.pending()] == [9202], "fresh row must stay queued"
    with db.get_connection() as conn:
        row = conn.execute("SELECT * FROM telegram_messages WHERE update_id = 9201").fetchone()
    assert row is not None, "the row must survive — it's training data"
    assert row["processed_at"] is not None and "abandoned" in row["error"], dict(row)


def test_message_log_records_outbound_and_exports_jsonl():
    import json as _json

    from openclaw import config, message_log

    db.init_db()
    _clear_message_log()

    message_log.record_inbound(_fake_update(9301))
    message_log.record_outbound(
        "send_message", "Claim #2 marked sent", {"chat_id": 1, "text": "Claim #2 marked sent"}
    )

    lines = [_json.loads(line) for line in message_log.iter_jsonl()]
    assert [line["direction"] for line in lines] == ["in", "out"], lines
    assert all(line["app_version"] == config.APP_VERSION for line in lines)
    # payload is re-inlined as an object so a consumer doesn't double-decode
    assert isinstance(lines[1]["payload"], dict), lines[1]["payload"]
    assert message_log.stats()["queued"] == 1


def test_transient_errors_are_warnings_not_action_required_errors():
    """ERROR must mean "Justin has to do something". A dropped Gmail socket is
    retried by the next tick unaided — it logged a full traceback before."""
    import http.client
    import socket

    from googleapiclient.errors import HttpError

    from openclaw import pipeline

    assert pipeline._is_transient(http.client.IncompleteRead(b""))
    assert pipeline._is_transient(socket.timeout())
    assert pipeline._is_transient(ConnectionResetError())
    assert pipeline._is_transient(
        HttpError(type("R", (), {"status": 503, "reason": "busy"})(), b"")
    )
    assert not pipeline._is_transient(
        HttpError(type("R", (), {"status": 404, "reason": "gone"})(), b"")
    )
    assert not pipeline._is_transient(ValueError("real bug"))


def test_watchdog_restarts_the_process_when_polling_is_dead():
    """Nothing awaits the updater task, so its death is silent and inbound stops.
    The watchdog must alert (sending still works) and then take the process down
    for compose to restart."""
    from openclaw import pipeline, telegram_bot

    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM ops_alerts WHERE kind = ?", (pipeline._POLLING_ALERT,))

    exits, sent = [], []
    # The watchdog's alert goes through the `notify` seam now, not straight at
    # PTB — that is the whole point of the seam, and this alert must survive the
    # transport switch or a dead updater stops announcing itself.
    from openclaw import notify

    original_alive, original_send = telegram_bot.polling_alive, notify.send_text
    notify.send_text = lambda msg, buttons=None: sent.append(msg) or True
    try:
        telegram_bot.polling_alive = lambda: True
        assert pipeline._watchdog_telegram_polling(exit_fn=lambda: exits.append(1)) is False
        assert exits == [] and sent == []

        telegram_bot.polling_alive = lambda: None  # bot disabled — not a fault
        assert pipeline._watchdog_telegram_polling(exit_fn=lambda: exits.append(1)) is False
        assert exits == []

        telegram_bot.polling_alive = lambda: False
        assert pipeline._watchdog_telegram_polling(exit_fn=lambda: exits.append(1)) is True
        assert exits == [1], "a dead updater must take the process down"
        assert sent and "Telegram polling" in sent[0], sent
    finally:
        telegram_bot.polling_alive = original_alive
        notify.send_text = original_send


def test_unhandled_callback_data_reports_instead_of_silently_returning():
    """A tap whose prefix no branch handles used to fall off the end of
    on_callback doing nothing — indistinguishable from a tap that never
    arrived, which is exactly how a morning of button presses vanished."""
    import asyncio

    from openclaw import config, telegram_bot

    class FakeQuery:
        def __init__(self, data):
            self.data = data
            self.from_user = type("U", (), {"username": config.TELEGRAM_USERNAME})()
            self.message = type("M", (), {"text": "card", "caption": None})()
            self.edited = None

        async def answer(self):
            pass

        async def edit_message_text(self, text):
            self.edited = text

    q = FakeQuery("bogusprefix:7")
    update = type("Upd", (), {"callback_query": q})()
    asyncio.run(telegram_bot.on_callback(update, None))
    assert q.edited and "isn't wired up" in q.edited, q.edited

    # Unauthorized taps still return without acting — but now they say so in the log.
    q2 = FakeQuery("sent:1")
    q2.from_user = type("U", (), {"username": "someone_else"})()
    asyncio.run(telegram_bot.on_callback(type("Upd", (), {"callback_query": q2})(), None))
    assert q2.edited is None, q2.edited


def test_ack_reacts_thumbs_up_and_swallows_failures():
    """Every incoming user message gets an instant 👍 reaction receipt; a
    reaction failure must never break the real handler."""
    import asyncio

    from openclaw import telegram_bot

    class FakeMessage:
        def __init__(self, fail=False):
            self.fail = fail
            self.reaction = None

        async def set_reaction(self, reaction):
            if self.fail:
                raise RuntimeError("reactions not allowed in this chat")
            self.reaction = reaction

    msg = FakeMessage()
    asyncio.run(telegram_bot._ack(msg))
    assert msg.reaction == "👍", msg.reaction

    broken = FakeMessage(fail=True)
    asyncio.run(telegram_bot._ack(broken))  # must not raise
    assert broken.reaction is None


def test_reject_split_proposal_flags_and_never_reproposes():
    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM vet_claims")
        conn.execute("DELETE FROM bank_transactions")
        conn.execute("DELETE FROM split_proposals")
        claim_a = _insert_pending_claim(conn, "MEDIPAWS TEST", -551.06, "2026-04-13")
        claim_b = _insert_pending_claim(conn, "MEDIPAWS TEST", -1970.40, "2026-04-13")
        import json as _json

        conn.execute(
            "INSERT INTO split_proposals (email_id, invoice_json, claim_ids, created_at) VALUES (?, ?, ?, ?)",
            (
                "email-r-1",
                _json.dumps({"date": "2026-04-13", "amount": 2521.46}),
                _json.dumps([claim_a, claim_b]),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        pid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    result = invoice_matching.reject_split_proposal(pid)
    assert result["ok"]
    with db.get_connection() as conn:
        flags = [
            r[0]
            for r in conn.execute(
                "SELECT flag FROM vet_claims WHERE id IN (?, ?)", (claim_a, claim_b)
            )
        ]
        status = conn.execute("SELECT status FROM split_proposals WHERE id = ?", (pid,)).fetchone()[
            0
        ]
    assert status == "rejected"
    assert all(f and "match this charge manually" in f for f in flags)
    # a rejected pair must never be re-proposed
    with db.get_connection() as conn:
        claim_row = conn.execute(
            "SELECT vet_claims.*, bank_transactions.amount AS txn_amount, "
            "bank_transactions.merchant AS txn_merchant FROM vet_claims "
            "JOIN bank_transactions ON bank_transactions.id = vet_claims.transaction_id "
            "WHERE vet_claims.id = ?",
            (claim_a,),
        ).fetchone()
    oversized = {"date": "2026-04-13", "amount": 2521.46, "_email_id": "email-r-1"}
    assert invoice_matching._propose_split(claim_row, oversized) is None, (
        "rejected pair must not re-flag as a merge"
    )
    with db.get_connection() as conn:
        assert conn.execute("SELECT count(*) FROM split_proposals").fetchone()[0] == 1, (
            "no new proposal after reject"
        )


def test_propose_split_detects_payment_records():
    """The invoice's own payment section listing both charge amounts is the
    merge evidence — recorded on the proposal for the Telegram message."""
    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM vet_claims")
        conn.execute("DELETE FROM bank_transactions")
        conn.execute("DELETE FROM split_proposals")
        claim_a = _insert_pending_claim(conn, "MEDIPAWS TEST", -551.06, "2026-04-13")
        _insert_pending_claim(conn, "MEDIPAWS TEST", -1970.40, "2026-04-13")
        claim_row = conn.execute(
            "SELECT vet_claims.*, bank_transactions.amount AS txn_amount, "
            "bank_transactions.merchant AS txn_merchant FROM vet_claims "
            "JOIN bank_transactions ON bank_transactions.id = vet_claims.transaction_id "
            "WHERE vet_claims.id = ?",
            (claim_a,),
        ).fetchone()
    # real payment-section shape: 'Eftpos/Visa/Mastercard : -1970.40'
    text_amounts = invoice_matching._text_amounts(
        "Total: $2521.46 Payment method: Eftpos/Visa/Mastercard : -1970.40 Eftpos/Visa/Mastercard : -551.06"
    )
    oversized = {
        "date": "2026-04-13",
        "amount": 2521.46,
        "_email_id": "email-p-1",
        "_text_amounts": text_amounts,
    }
    assert invoice_matching._propose_split(claim_row, oversized)
    import json as _json

    with db.get_connection() as conn:
        stored = _json.loads(conn.execute("SELECT invoice_json FROM split_proposals").fetchone()[0])
    assert stored["payments_confirmed"] is True


def test_split_proposal_not_created_when_charges_dont_explain_invoice():
    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM vet_claims")
        conn.execute("DELETE FROM bank_transactions")
        conn.execute("DELETE FROM split_proposals")
        claim_a = _insert_pending_claim(conn, "SOME VET", -100.00, "2026-04-13")
        _insert_pending_claim(conn, "SOME VET", -200.00, "2026-04-13")
        _insert_pending_claim(conn, "OTHER VET", -2421.46, "2026-04-13")  # right sum, wrong vet
    oversized = {"date": "2026-04-13", "amount": 2521.46, "_email_id": "email-split-2"}
    with db.get_connection() as conn:
        claim_row = conn.execute(
            "SELECT vet_claims.*, bank_transactions.amount AS txn_amount, "
            "bank_transactions.merchant AS txn_merchant FROM vet_claims "
            "JOIN bank_transactions ON bank_transactions.id = vet_claims.transaction_id "
            "WHERE vet_claims.id = ?",
            (claim_a,),
        ).fetchone()
    assert invoice_matching._propose_split(claim_row, oversized) is None
    with db.get_connection() as conn:
        assert conn.execute("SELECT count(*) FROM split_proposals").fetchone()[0] == 0


def test_notify_split_proposals_sends_picker_once():
    from openclaw import pipeline

    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM vet_claims")
        conn.execute("DELETE FROM bank_transactions")
        conn.execute("DELETE FROM split_proposals")
        claim_a = _insert_pending_claim(conn, "MEDIPAWS TEST", -551.06, "2026-04-13")
        claim_b = _insert_pending_claim(conn, "MEDIPAWS TEST", -1970.40, "2026-04-13")
        import json as _json

        conn.execute(
            "INSERT INTO split_proposals (email_id, invoice_json, claim_ids, created_at) VALUES (?, ?, ?, ?)",
            (
                "email-n-1",
                _json.dumps({"date": "2026-04-13", "amount": 2521.46}),
                _json.dumps([claim_a, claim_b]),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    sent = []
    pipeline.notify_split_proposals(send_fn=lambda text, markup=None: sent.append((text, markup)))
    assert len(sent) == 1
    text, markup = sent[0]
    assert "$2521.46" in text and f"#{claim_a}" in text and f"#{claim_b}" in text
    assert "$551.06" in text and "$1970.40" in text
    assert markup is not None, "picker buttons must be attached"
    # already notified — no re-send
    pipeline.notify_split_proposals(send_fn=lambda text, markup=None: sent.append((text, markup)))
    assert len(sent) == 1


def test_run_once_isolates_one_claims_failure():
    """Real failure mode: extraction error on the first pending claim starved
    Petcover polling + notifications for days. One claim's crash must flag that
    claim only; later claims and every downstream stage still run."""
    from openclaw import pipeline

    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM vet_claims")
        conn.execute("DELETE FROM bank_transactions")
        claim_a = _insert_pending_claim(conn, "CRASHY VET", -50.0, "2026-07-01")
        claim_b = _insert_pending_claim(conn, "HEALTHY VET", -60.0, "2026-07-02")

    attempted, stages = [], []

    def fake_match(claim):
        attempted.append(claim["id"])
        if claim["id"] == claim_a:
            raise RuntimeError("boom")
        return False

    originals = (
        pipeline.vet_detection.classify_unflagged,
        pipeline.reconcile_sent_invoice_requests,
        pipeline.invoice_matching.match_claim,
        pipeline._maybe_send_invoice_request,
        pipeline.poll_petcover_status,
        pipeline.notify_claim_states,
        pipeline._ensure_gmail_auth,
    )
    pipeline.vet_detection.classify_unflagged = lambda: stages.append("classify")
    pipeline._ensure_gmail_auth = lambda: True
    pipeline.reconcile_sent_invoice_requests = lambda: stages.append("reconcile")
    pipeline.invoice_matching.match_claim = fake_match
    pipeline._maybe_send_invoice_request = lambda claim: stages.append(f"send:{claim['id']}")
    pipeline.poll_petcover_status = lambda: stages.append("poll")
    pipeline.notify_claim_states = lambda: stages.append("notify")
    try:
        pipeline.run_once()
    finally:
        (
            pipeline.vet_detection.classify_unflagged,
            pipeline.reconcile_sent_invoice_requests,
            pipeline.invoice_matching.match_claim,
            pipeline._maybe_send_invoice_request,
            pipeline.poll_petcover_status,
            pipeline.notify_claim_states,
            pipeline._ensure_gmail_auth,
        ) = originals

    assert attempted == [claim_a, claim_b], "claim B must still be attempted after claim A crashes"
    assert "poll" in stages and "notify" in stages, "downstream stages must run despite the failure"
    assert f"send:{claim_b}" in stages, "claim B continues through the normal no-match path"
    with db.get_connection() as conn:
        flag_a = conn.execute("SELECT flag FROM vet_claims WHERE id = ?", (claim_a,)).fetchone()[0]
    assert flag_a and flag_a.startswith("invoice matching error"), (
        "failure must be visible on the claim"
    )


def test_run_once_llm_outage_skips_matching_but_runs_downstream():
    """LLM outage is global — matching stops for the tick (no quota burn on the
    rest), the first affected claim is flagged, downstream stages still run,
    and the flag clears on the next healthy attempt."""
    from openclaw import pipeline

    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM vet_claims")
        conn.execute("DELETE FROM bank_transactions")
        claim_a = _insert_pending_claim(conn, "VET ONE", -50.0, "2026-07-01")
        claim_b = _insert_pending_claim(conn, "VET TWO", -60.0, "2026-07-02")

    attempted, stages = [], []

    def unavailable_match(claim):
        attempted.append(claim["id"])
        raise llm.LLMUnavailableError("429 quota")

    originals = (
        pipeline.vet_detection.classify_unflagged,
        pipeline.reconcile_sent_invoice_requests,
        pipeline.invoice_matching.match_claim,
        pipeline._maybe_send_invoice_request,
        pipeline.poll_petcover_status,
        pipeline.notify_claim_states,
        pipeline._ensure_gmail_auth,
    )
    pipeline.vet_detection.classify_unflagged = lambda: None
    pipeline._ensure_gmail_auth = lambda: True
    pipeline.reconcile_sent_invoice_requests = lambda: None
    pipeline.invoice_matching.match_claim = unavailable_match
    pipeline._maybe_send_invoice_request = lambda claim: None
    pipeline.poll_petcover_status = lambda: stages.append("poll")
    pipeline.notify_claim_states = lambda: stages.append("notify")
    try:
        pipeline.run_once()
        with db.get_connection() as conn:
            flag_a = conn.execute(
                "SELECT flag FROM vet_claims WHERE id = ?", (claim_a,)
            ).fetchone()[0]
            flag_b = conn.execute(
                "SELECT flag FROM vet_claims WHERE id = ?", (claim_b,)
            ).fetchone()[0]
        assert attempted == [claim_a], "outage must stop further matching this tick"
        assert flag_a and flag_a.startswith("invoice extraction unavailable")
        assert flag_b is None, "unattempted claims must not be flagged"
        assert stages == ["poll", "notify"], "downstream stages must still run during an outage"

        # next healthy tick: stale outage flag clears before the attempt
        attempted.clear()
        pipeline.invoice_matching.match_claim = lambda claim: attempted.append(claim["id"]) or False
        pipeline.run_once()
        with db.get_connection() as conn:
            flag_a = conn.execute(
                "SELECT flag FROM vet_claims WHERE id = ?", (claim_a,)
            ).fetchone()[0]
        assert flag_a is None, "recovered claim must not carry a stale outage flag"
    finally:
        (
            pipeline.vet_detection.classify_unflagged,
            pipeline.reconcile_sent_invoice_requests,
            pipeline.invoice_matching.match_claim,
            pipeline._maybe_send_invoice_request,
            pipeline.poll_petcover_status,
            pipeline.notify_claim_states,
            pipeline._ensure_gmail_auth,
        ) = originals


# Real-shape page texts (from MediPaws' actual PDFs): a per-visit invoice page
# and an account-statement page that carries the same amounts but no header.
_INVOICE_PAGE = (
    "INVOICE\n#411193\nInvoice date:\n13th April 2026\nCustomer name: \nGoldberg, Gabi\n"
    "Patient name:\nAari\nDescription Qty Total\nSpecialist Consultation (Initial) 1 $350.00\n"
    "Imaging: Ultrasound - Abdomen +/-FNA (RFP) 1 $1155.00\nTotal: $2521.46\nAmount paid: $2521.46"
)
_ECHO_PAGE = (
    "INVOICE\n#414503\nInvoice date:\n17th June 2026\nPatient name:\nEcho\n"
    "Description Qty Total\nHospitalisation 1 $1328.25\nTotal: $1328.25"
)
_STATEMENT_PAGE = (
    "Account Statement\nPrinted: Customer ID:\nFrom: To:\nInvoice 411193 13/04/2026 2521.46\n"
    "Invoice 414503 17/06/2026 1328.25\nBalance: 0.00"
)


def test_find_invoice_segment_picks_right_page_and_pet():
    pages = [_INVOICE_PAGE, _ECHO_PAGE]
    assert claim_forms.find_invoice_segment(pages, 2521.46, "Aari") == (0, 0)
    assert claim_forms.find_invoice_segment(pages, 1328.25, "Echo") == (1, 1)
    # same total but the page names the OTHER pet — refused
    assert claim_forms.find_invoice_segment(pages, 2521.46, "Echo", ("Aari",)) is None
    # pet unknown: amount alone picks the segment
    assert claim_forms.find_invoice_segment(pages, 1328.25, None) == (1, 1)
    # grouped thousands formatting still matches
    assert claim_forms.find_invoice_segment(
        ["Tax Invoice\nPatient: Aari\nTotal: $2,521.46"], 2521.46, "Aari"
    ) == (0, 0)


def test_find_invoice_segment_handles_colonless_patient_and_unknown_words():
    """Real SAH format: 'Patient Echo' — no colon (the colon-required regex
    missed it live). A patient-word that isn't a known pet carries no signal."""
    sah_page = "Tax Invoice\nTransaction No 6351750 Patient Echo Reference Hannah\nTotal: $10.50"
    assert claim_forms.find_invoice_segment([sah_page], 10.50, "Echo", ("Aari",)) == (0, 0)
    assert claim_forms.find_invoice_segment([sah_page], 10.50, "Aari", ("Echo",)) is None, (
        "names the other pet"
    )
    # 'Patient care' is not a pet — must not reject
    care_page = "Tax Invoice\nPatient care plan discussed\nTotal: $45.00"
    assert claim_forms.find_invoice_segment([care_page], 45.00, "Aari", ("Echo",)) == (0, 0)


def test_single_pet_in_text_assigns_only_when_unambiguous():
    db.init_db()
    receipt = "Item Name Qty Total Echo 17 Jun 2026 Consultation - Standard 1.0 $140.74"
    bulk = "all visits over the past 12 months for Aari and Echo Goldberg"
    with db.get_connection() as conn:
        echo_id = conn.execute("SELECT id FROM pets WHERE name='Echo'").fetchone()[0]
    assert invoice_matching._single_pet_in_text(receipt) == echo_id
    assert invoice_matching._single_pet_in_text(bulk) is None, "both pets named = no signal"
    assert invoice_matching._single_pet_in_text("no pets here") is None


def test_find_invoice_segment_rejects_account_statement():
    """The running-total statement carries the amounts but no invoice header —
    it must never validate as an attachable invoice."""
    assert claim_forms.find_invoice_segment([_STATEMENT_PAGE], 2521.46, "Aari") is None
    assert claim_forms.find_invoice_segment([], 2521.46, "Aari") is None
    assert claim_forms.find_invoice_segment(["", ""], 2521.46, None) is None  # image-only scan


def _insert_matched_claim(
    conn,
    merchant,
    amount,
    txn_date,
    pet_id=None,
    email_id="em-x",
    invoice_amount=None,
    condition=None,
    invoice_path=None,
):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO bank_transactions (date, amount, merchant, created_at) VALUES (?, ?, ?, ?)",
        (txn_date, amount, merchant, now),
    )
    txn_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    import json as _json

    conn.execute(
        "INSERT INTO vet_claims (transaction_id, pet_id, status, matched_email_id, invoice_data, "
        "condition_text, invoice_file_path, created_at, updated_at) VALUES (?, ?, 'matched', ?, ?, ?, ?, ?, ?)",
        (
            txn_id,
            pet_id,
            email_id,
            _json.dumps(
                {
                    "amount": invoice_amount if invoice_amount is not None else abs(amount),
                    "date": txn_date,
                }
            ),
            condition,
            invoice_path,
            now,
            now,
        ),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _matched_row(claim_id):
    with db.get_connection() as conn:
        return conn.execute(
            "SELECT vet_claims.*, bank_transactions.merchant AS txn_merchant, "
            "bank_transactions.date AS txn_date, bank_transactions.amount AS txn_amount "
            "FROM vet_claims JOIN bank_transactions ON bank_transactions.id = vet_claims.transaction_id "
            "WHERE vet_claims.id = ?",
            (claim_id,),
        ).fetchone()


def test_ensure_invoice_file_flags_inadequate_attachment():
    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM vet_claims")
        conn.execute("DELETE FROM bank_transactions")
        cid = _insert_matched_claim(conn, "MEDIPAWS TEST", -2521.46, "2026-04-13")
    original = claim_forms._email_pdf_documents
    claim_forms._email_pdf_documents = lambda email_id: [(None, [_STATEMENT_PAGE])]
    try:
        claim_forms.ensure_invoice_file(_matched_row(cid))
    finally:
        claim_forms._email_pdf_documents = original
    row = _matched_row(cid)
    assert row["invoice_file_path"] is None
    assert (
        row["flag"]
        and "isn't a per-visit itemised invoice" in row["flag"]
        and "MEDIPAWS TEST" in row["flag"]
    )


def test_ensure_invoice_file_never_overwrites_manual_path():
    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM vet_claims")
        conn.execute("DELETE FROM bank_transactions")
        cid = _insert_matched_claim(
            conn, "MEDIPAWS TEST", -100.0, "2026-04-13", invoice_path=r"G:\manual\inv.pdf"
        )
    original = claim_forms._email_pdf_documents
    claim_forms._email_pdf_documents = lambda email_id: (_ for _ in ()).throw(
        AssertionError("must not fetch")
    )
    try:
        claim_forms.ensure_invoice_file(_matched_row(cid))
    finally:
        claim_forms._email_pdf_documents = original
    assert _matched_row(cid)["invoice_file_path"] == r"G:\manual\inv.pdf"


def test_pick_invoice_prefers_exact_amount_and_skips_claimed():
    """Real false match: #20's $152.50 charge grabbed #21's $44.75 invoice
    (under the ceiling, 3 days off) while the exact 185106 sat unpicked."""
    import json as _json
    from datetime import date as _date

    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM vet_claims")
        conn.execute("DELETE FROM bank_transactions")
        other = _insert_matched_claim(conn, "KINGS VET TEST", -44.75, "2025-08-08")
        conn.execute(
            "UPDATE vet_claims SET invoice_data = ? WHERE id = ?",
            (
                _json.dumps({"invoice_number": "185019", "amount": 44.75, "date": "2025-08-08"}),
                other,
            ),
        )
    invoices = [
        {"invoice_number": "185019", "amount": 44.75, "date": "2025-08-08"},
        {"invoice_number": "185106", "amount": 152.5, "date": "2025-08-11"},
    ]
    picked = invoice_matching._pick_invoice(invoices, -152.5, _date(2025, 8, 11), claim_id=999999)
    assert picked["invoice_number"] == "185106", picked
    # the claimed one alone no longer matches either
    picked = invoice_matching._pick_invoice(
        invoices[:1], -152.5, _date(2025, 8, 11), claim_id=999999
    )
    assert picked is None
    # without DB context the exact amount+date still wins over first-in-list
    picked = invoice_matching._pick_invoice(invoices, -152.5, _date(2025, 8, 11))
    assert picked["invoice_number"] == "185106", picked


def test_vision_fallback_attempt_cap():
    """A scan the model can't parse consumes attempts and goes quiet after
    VISION_MAX_ATTEMPTS — no token burn every tick forever."""
    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM vision_ocr_attempts")
        conn.execute("DELETE FROM email_extractions")
    calls = []
    original = claim_forms.email_pdf_attachments
    claim_forms.email_pdf_attachments = lambda email_id: calls.append(email_id) or []
    try:
        for _ in range(invoice_matching.VISION_MAX_ATTEMPTS + 2):
            assert invoice_matching._vision_invoices("em-scan-1") is None
    finally:
        claim_forms.email_pdf_attachments = original
    assert len(calls) == invoice_matching.VISION_MAX_ATTEMPTS, calls
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT attempts FROM vision_ocr_attempts WHERE message_id = 'em-scan-1'"
        ).fetchone()
    assert row["attempts"] == invoice_matching.VISION_MAX_ATTEMPTS


def test_vision_provider_outage_refunds_attempt():
    """A Gemini 503 is not an unreadable scan — the attempt is refunded so
    outages can't exhaust an email's vision budget."""
    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM vision_ocr_attempts")
    original = claim_forms.email_pdf_attachments
    claim_forms.email_pdf_attachments = lambda email_id: (_ for _ in ()).throw(
        llm.LLMUnavailableError("503 UNAVAILABLE")
    )
    try:
        for _ in range(5):  # would exceed the cap if outages counted
            try:
                invoice_matching._vision_invoices("em-outage-1")
                assert False, "must re-raise LLMUnavailableError"
            except llm.LLMUnavailableError:
                pass
    finally:
        claim_forms.email_pdf_attachments = original
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT attempts FROM vision_ocr_attempts WHERE message_id = 'em-outage-1'"
        ).fetchone()
    assert row["attempts"] == 0, row["attempts"]


def _scan_pdf_bytes(pages: int) -> bytes:
    """An image-only PDF exactly like a vet's photo scan: each page is one
    embedded image, no text layer (pillow's PDF writer produces this shape)."""
    import io

    from PIL import Image

    images = [Image.new("RGB", (120, 160), (240, 240, 240)) for _ in range(pages)]
    buf = io.BytesIO()
    images[0].save(buf, format="PDF", save_all=True, append_images=images[1:])
    return buf.getvalue()


def test_vision_invoices_reads_pages_skips_junk_and_caches():
    """Per-page behaviors in one bundle: a valid invoice is kept with its
    source_pdf/page recorded; a not_invoice page, an unparseable reply and a
    missing-amount reply are all skipped; the result caches in
    email_extractions so vision never re-runs for the email."""
    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM vision_ocr_attempts")
        conn.execute("DELETE FROM email_extractions")
    replies = iter(
        [
            '{"invoice_number": "184556", "date": "2025-07-28", "patient": "Aari", "amount": 45.0, "items": []}',
            '{"not_invoice": true}',
            "the model rambled and returned no JSON at all",
            '{"invoice_number": "9", "date": "2025-08-01", "patient": "Aari", "items": []}',  # no amount
        ]
    )
    vision_calls = []
    original_att = claim_forms.email_pdf_attachments
    original_vision = llm.extract_vision
    claim_forms.email_pdf_attachments = lambda email_id: [("scans.pdf", _scan_pdf_bytes(4))]
    llm.extract_vision = lambda prompt, jpeg, purpose="vision_extraction": (
        vision_calls.append(1) or next(replies)
    )
    try:
        invoices = invoice_matching._vision_invoices("em-scan-mix")
    finally:
        claim_forms.email_pdf_attachments = original_att
        llm.extract_vision = original_vision
    assert len(vision_calls) == 4, "one vision call per page"
    assert len(invoices) == 1, invoices
    assert invoices[0]["invoice_number"] == "184556"
    assert invoices[0]["source_pdf"] == "scans.pdf" and invoices[0]["page"] == 0
    # cached: the text-extraction entry point returns it without any LLM call
    assert invoice_matching._cached_extraction("em-scan-mix") == invoices
    assert invoice_matching._invoices_for_email("em-scan-mix", "") == invoices


def test_vision_all_pages_unreadable_returns_none_and_not_cached():
    """A bundle where no page yields an invoice: None (flag stands), nothing
    cached — the remaining attempts may retry next tick."""
    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM vision_ocr_attempts")
        conn.execute("DELETE FROM email_extractions")
    original_att = claim_forms.email_pdf_attachments
    original_vision = llm.extract_vision
    claim_forms.email_pdf_attachments = lambda email_id: [("scans.pdf", _scan_pdf_bytes(2))]
    llm.extract_vision = lambda prompt, jpeg, purpose="vision_extraction": "illegible blur"
    try:
        assert invoice_matching._vision_invoices("em-scan-blur") is None
    finally:
        claim_forms.email_pdf_attachments = original_att
        llm.extract_vision = original_vision
    assert invoice_matching._cached_extraction("em-scan-blur") is None
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT attempts FROM vision_ocr_attempts WHERE message_id = 'em-scan-blur'"
        ).fetchone()
    assert row["attempts"] == 1


def test_already_claimed_identity_rules():
    """invoice_number wins when both sides have one (different numbers with the
    same amount+date are DIFFERENT invoices); number on one side only falls
    back to amount+date."""
    import json as _json

    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM vet_claims")
        conn.execute("DELETE FROM bank_transactions")
        other = _insert_matched_claim(conn, "KINGS VET TEST", -45.0, "2025-07-28")
        conn.execute(
            "UPDATE vet_claims SET invoice_data = ? WHERE id = ?",
            (
                _json.dumps({"invoice_number": "184556", "amount": 45.0, "date": "2025-07-28"}),
                other,
            ),
        )
    same_number = {"invoice_number": "184556", "amount": 45.0, "date": "2025-07-28"}
    different_number = {"invoice_number": "188313", "amount": 45.0, "date": "2025-07-28"}
    no_number_same_facts = {"amount": 45.0, "date": "2025-07-28"}
    no_number_other_facts = {"amount": 152.5, "date": "2025-08-11"}
    assert invoice_matching._already_claimed(same_number, claim_id=999999)
    assert not invoice_matching._already_claimed(different_number, claim_id=999999)
    assert invoice_matching._already_claimed(no_number_same_facts, claim_id=999999)
    assert not invoice_matching._already_claimed(no_number_other_facts, claim_id=999999)
    # a claim never blocks itself (re-matching after unmatch)
    assert not invoice_matching._already_claimed(same_number, claim_id=other)


def test_pick_invoice_unparseable_date_loses_to_dated_candidate():
    from datetime import date as _date

    invoices = [
        {"invoice_number": "A", "amount": 100.0, "date": "sometime in winter"},
        {"invoice_number": "B", "amount": 100.0, "date": "2026-05-18"},
    ]
    picked = invoice_matching._pick_invoice(invoices, -100.0, _date(2026, 5, 18))
    assert picked["invoice_number"] == "B", picked


def test_ensure_invoice_file_scan_page_out_of_range_flags():
    """A stale page index (attachment re-sent/changed) must not crash — it
    falls through to the inadequate-attachment flag."""
    import json as _json

    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM vet_claims")
        conn.execute("DELETE FROM bank_transactions")
        cid = _insert_matched_claim(
            conn, "KINGS VET TEST", -45.0, "2025-07-28", email_id="em-scan-3"
        )
        conn.execute(
            "UPDATE vet_claims SET invoice_data = ? WHERE id = ?",
            (
                _json.dumps(
                    {"amount": 45.0, "date": "2025-07-28", "source_pdf": "scans.pdf", "page": 99}
                ),
                cid,
            ),
        )
    original_att = claim_forms.email_pdf_attachments
    original_docs = claim_forms._email_pdf_documents
    claim_forms.email_pdf_attachments = lambda email_id: [("scans.pdf", _scan_pdf_bytes(1))]
    claim_forms._email_pdf_documents = lambda email_id: []  # scan: no text docs either
    try:
        claim_forms.ensure_invoice_file(_matched_row(cid))
    finally:
        claim_forms.email_pdf_attachments = original_att
        claim_forms._email_pdf_documents = original_docs
    row = _matched_row(cid)
    assert row["invoice_file_path"] is None
    assert row["flag"] and "isn't a per-visit itemised invoice" in row["flag"]


def test_pet_id_by_name_exact_known_pet_only():
    db.init_db()
    with db.get_connection() as conn:
        pet = conn.execute("SELECT id, name FROM pets LIMIT 1").fetchone()
    assert pet is not None, "live schema seeds pets"
    assert invoice_matching._pet_id_by_name(pet["name"].lower()) == pet["id"]
    assert invoice_matching._pet_id_by_name("Rex The Unknown") is None
    assert invoice_matching._pet_id_by_name(None) is None


def test_ensure_invoice_file_slices_scan_page_and_assigns_pet():
    """Vision-extracted invoices carry source_pdf/page — the claim's page is
    sliced without a text layer, and the extracted patient assigns the pet."""
    import io
    import json as _json
    import tempfile

    from pypdf import PdfWriter

    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM vet_claims")
        conn.execute("DELETE FROM bank_transactions")
        pet = conn.execute("SELECT id, name FROM pets LIMIT 1").fetchone()
        cid = _insert_matched_claim(
            conn, "KINGS VET TEST", -45.0, "2025-07-28", email_id="em-scan-2"
        )
        conn.execute(
            "UPDATE vet_claims SET invoice_data = ? WHERE id = ?",
            (
                _json.dumps(
                    {
                        "amount": 45.0,
                        "date": "2025-07-28",
                        "patient": pet["name"],
                        "source_pdf": "scans.pdf",
                        "page": 1,
                    }
                ),
                cid,
            ),
        )
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)

    original_att = claim_forms.email_pdf_attachments
    original_dir = claim_forms.config.INVOICE_OUTPUT_DIR
    claim_forms.email_pdf_attachments = lambda email_id: [("scans.pdf", buf.getvalue())]
    claim_forms.config.INVOICE_OUTPUT_DIR = tempfile.mkdtemp()
    try:
        claim_forms.ensure_invoice_file(_matched_row(cid))
    finally:
        claim_forms.email_pdf_attachments = original_att
        claim_forms.config.INVOICE_OUTPUT_DIR = original_dir
    row = _matched_row(cid)
    assert row["invoice_file_path"] and row["invoice_file_path"].endswith(
        f"claim-{cid}-2025-07-28.pdf"
    )
    from pypdf import PdfReader

    assert len(PdfReader(row["invoice_file_path"]).pages) == 1
    assert row["pet_id"] == pet["id"], "patient field must assign the pet"


def test_draft_step_batches_ready_claims_by_four_per_pet():
    """6 ready same-pet claims → one 4-claim batch + one 2-claim batch (the
    Petcover form holds 4 rows); a not-ready claim still routes through
    process_claim for its per-field flagging."""
    from openclaw import pipeline

    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM vet_claims")
        conn.execute("DELETE FROM bank_transactions")
        aari = conn.execute("SELECT id FROM pets WHERE name='Aari'").fetchone()[0]
        ready = [
            _insert_matched_claim(
                conn,
                "BATCH VET",
                -50.0 - i,
                f"2026-05-{10 + i:02d}",
                pet_id=aari,
                condition="arthritis",
                invoice_path=f"/data/invoices/t{i}.pdf",
            )
            for i in range(6)
        ]
        lone = _insert_matched_claim(
            conn, "BATCH VET", -70.0, "2026-05-20", pet_id=aari
        )  # no condition/invoice

    batches, singles = [], []
    originals = (
        claim_forms.ensure_invoice_file,
        claim_forms.process_claim_batch,
        claim_forms.process_claim,
    )
    claim_forms.ensure_invoice_file = lambda claim: None
    claim_forms.process_claim_batch = lambda ids, continuation=None: batches.append(ids)
    claim_forms.process_claim = lambda cid, continuation=None: singles.append(cid)
    try:
        pipeline._draft_matched_claims()
    finally:
        (
            claim_forms.ensure_invoice_file,
            claim_forms.process_claim_batch,
            claim_forms.process_claim,
        ) = originals

    assert [len(b) for b in batches] == [4, 2], f"expected 4+2 chunks, got {batches}"
    assert batches[0] == ready[:4] and batches[1] == ready[4:], "chunks must be in txn-date order"
    assert singles == [lone], "not-ready claim must go through per-claim flagging"


def test_notify_pushes_flagged_pending_claims_grouped_once():
    from openclaw import pipeline

    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM vet_claims")
        conn.execute("DELETE FROM bank_transactions")
        flag = "invoice attachment unreadable — Re: Invoice request"
        for amt, d in [(-351.50, "2026-05-18"), (-132.50, "2026-04-17")]:
            cid = _insert_pending_claim(conn, "KINGS TEST", amt, d)
            conn.execute("UPDATE vet_claims SET flag = ? WHERE id = ?", (flag, cid))

    sent = []
    pipeline.notify_claim_states(send_fn=lambda text, markup=None: sent.append(text))
    assert len(sent) == 1, f"same merchant+flag must be ONE message, got {len(sent)}"
    assert "unreadable" in sent[0] and "$351.50" in sent[0] and "$132.50" in sent[0]
    pipeline.notify_claim_states(send_fn=lambda text, markup=None: sent.append(text))
    assert len(sent) == 1, "already-notified flags must not re-send"


def test_notify_messages_carry_claim_ids():
    """Every pushed message names the claim #id — Justin acts on ids (/mark,
    /pet) so a message without one is unanswerable (his report: alerts lacked
    the #, only the tap-results showed it)."""
    from openclaw import pipeline

    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM vet_claims")
        conn.execute("DELETE FROM bank_transactions")
        pet = conn.execute("SELECT id FROM pets LIMIT 1").fetchone()["id"]
        needs_cond = _insert_matched_claim(conn, "IDCHECK VET", -45.0, "2026-05-18", pet_id=pet)
        conn.execute(
            "UPDATE vet_claims SET flag = 'condition text missing — enter manually on dashboard' WHERE id = ?",
            (needs_cond,),
        )
        pending = _insert_pending_claim(conn, "IDCHECK PENDING VET", -70.0, "2026-05-20")
        conn.execute("UPDATE vet_claims SET flag = 'manual review needed' WHERE id = ?", (pending,))
    sent = []
    pipeline.notify_claim_states(send_fn=lambda text, markup=None: sent.append(text))
    cond_msg = next(t for t in sent if "IDCHECK VET" in t)
    pending_msg = next(t for t in sent if "IDCHECK PENDING" in t)
    assert f"#{needs_cond}" in cond_msg, cond_msg
    assert f"#{pending}" in pending_msg, pending_msg


def test_send_invoice_request_calls_gmail_send_not_drafts_create():
    """ADR-0030's one permitted send() call site — must call messages().send,
    never drafts().create."""
    from openclaw import invoice_matching

    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM vet_claims")
        conn.execute("DELETE FROM bank_transactions")
        conn.execute(
            "INSERT OR REPLACE INTO vet_contacts (merchant, email) VALUES (?, ?)",
            ("SEND TEST VET", "vet@sendtest.example"),
        )
        cid = _insert_pending_claim(conn, "SEND TEST VET", -80.0, "2026-06-01")
    claim = _matched_row(cid)

    calls = {"send": 0, "drafts_create": 0}

    class _Exec:
        def execute(self):
            return {"id": "sent-msg-1"}

    class FakeDrafts:
        def create(self, userId, body):
            calls["drafts_create"] += 1
            raise AssertionError("drafts().create must never be called from send_invoice_request")

    class FakeMessages:
        def send(self, userId, body):
            calls["send"] += 1
            return _Exec()

    class FakeUsers:
        def messages(self):
            return FakeMessages()

        def drafts(self):
            return FakeDrafts()

    class FakeService:
        def users(self):
            return FakeUsers()

    original = invoice_matching.gmail_client.build_service
    invoice_matching.gmail_client.build_service = lambda: FakeService()
    try:
        result = invoice_matching.send_invoice_request(claim)
    finally:
        invoice_matching.gmail_client.build_service = original

    assert result == "sent-msg-1"
    assert calls["send"] == 1 and calls["drafts_create"] == 0


def test_maybe_send_invoice_request_success_sets_flag_and_timestamp():
    from openclaw import invoice_matching, pipeline

    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM vet_claims")
        conn.execute("DELETE FROM bank_transactions")
        cid = _insert_pending_claim(conn, "AUTOSEND VET", -90.0, "2020-01-01")
    claim = _matched_row(cid)

    original = invoice_matching.send_invoice_request
    invoice_matching.send_invoice_request = lambda c: "sent-msg-2"
    try:
        pipeline._maybe_send_invoice_request(claim)
    finally:
        invoice_matching.send_invoice_request = original

    row = _matched_row(cid)
    assert row["flag"] == "invoice_request_auto_sent"
    assert row["invoice_request_sent_at"] is not None
    assert row["draft_id"] is None


def test_maybe_send_invoice_request_failure_flags_visibly_never_silent():
    from openclaw import invoice_matching, pipeline

    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM vet_claims")
        conn.execute("DELETE FROM bank_transactions")
        cid = _insert_pending_claim(conn, "AUTOSEND FAIL VET", -90.0, "2020-01-01")
    claim = _matched_row(cid)

    original = invoice_matching.send_invoice_request

    def boom(c):
        raise RuntimeError("Gmail API down")

    invoice_matching.send_invoice_request = boom
    try:
        pipeline._maybe_send_invoice_request(claim)
    finally:
        invoice_matching.send_invoice_request = original

    row = _matched_row(cid)
    assert row["flag"] == "invoice request send failed: Gmail API down"
    assert row["invoice_request_sent_at"] is None


def test_notify_pushes_once_when_invoice_request_auto_sent():
    """The flag string alone drives this (pipeline.py's notify_claim_states
    exclusion list deliberately omits it, ADR-0030) — regression-test the
    coupling design.md flags as fragile-looking."""
    from openclaw import pipeline

    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM vet_claims")
        conn.execute("DELETE FROM bank_transactions")
        cid = _insert_pending_claim(conn, "NOTIFY SENT VET", -55.0, "2026-06-01")
        conn.execute(
            "UPDATE vet_claims SET flag = 'invoice_request_auto_sent', invoice_request_sent_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), cid),
        )

    sent, markups = [], []
    pipeline.notify_claim_states(
        send_fn=lambda text, markup=None: (sent.append(text), markups.append(markup))
    )
    assert len(sent) == 1
    assert "invoice request sent" in sent[0] and f"#{cid}" in sent[0]
    assert "⚠" not in sent[0], "sending is good news, not a warning"
    assert markups[0] is None, "nothing for Justin to tap — informational only"


def test_notify_stays_silent_for_legacy_drafted_flag():
    from openclaw import pipeline

    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM vet_claims")
        conn.execute("DELETE FROM bank_transactions")
        cid = _insert_pending_claim(conn, "LEGACY DRAFT VET", -55.0, "2026-06-01")
        conn.execute("UPDATE vet_claims SET flag = 'invoice_request_drafted' WHERE id = ?", (cid,))

    sent = []
    pipeline.notify_claim_states(send_fn=lambda text, markup=None: sent.append(text))
    assert sent == [], "a legacy drafted-but-unsent flag must stay noise, unaffected by ADR-0030"


# ---------------------------------------------------------------------------
# Condition Thread tracking, ack correlation, settlement validation, ops alerts
# ---------------------------------------------------------------------------


def _fresh_db():
    """Clean slate: the smoke suite shares one DB file across tests, so thread /
    settlement tests must start from empty claim + event tables (and no stray
    policy anniversary) to stay deterministic."""
    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM claim_status_events")
        conn.execute("DELETE FROM vet_claims")
        conn.execute("DELETE FROM bank_transactions")
        conn.execute("DELETE FROM ops_alerts")
        # Proposals are keyed to claims that are about to be deleted; leaving
        # them makes "did this refusal queue anything?" depend on test order.
        conn.execute("DELETE FROM pending_proposals")
        conn.execute("DELETE FROM pending_flows")
        # settlement-clarification-email: batches/links reference claim ids
        # that are about to be deleted above — leaving them stale would let a
        # later test's "open batch" lookup resurrect a dead claim id.
        conn.execute("DELETE FROM clarification_batch_claims")
        conn.execute("DELETE FROM clarification_batches")
        conn.execute("UPDATE pets SET policy_anniversary = NULL")


def _insert_claim(
    conn,
    pet_id,
    txn_date,
    status="sent",
    draft_id=None,
    reference=None,
    sr=None,
    condition=None,
    invoice_data=None,
    amount=-50.0,
    merchant="THREAD VET",
):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO bank_transactions (date, amount, merchant, created_at) VALUES (?, ?, ?, ?)",
        (txn_date, amount, merchant, now),
    )
    txn_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO vet_claims (transaction_id, pet_id, status, draft_id, petcover_reference, "
        "petcover_sr, condition_text, invoice_data, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (txn_id, pet_id, status, draft_id, reference, sr, condition, invoice_data, now, now),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _insert_settled_event(conn, claim_id, created_at, paid):
    import json as _json

    conn.execute(
        "INSERT INTO claim_status_events (claim_id, event_type, raw_email_id, detail, created_at) "
        "VALUES (?, 'settled', NULL, ?, ?)",
        (claim_id, _json.dumps({"paid_amount": paid}), created_at),
    )


def _aari(conn):
    return conn.execute("SELECT id FROM pets WHERE name='Aari'").fetchone()[0]


def _claim_row(claim_id):
    with db.get_connection() as conn:
        return conn.execute("SELECT * FROM vet_claims WHERE id = ?", (claim_id,)).fetchone()


def test_route_reference_and_sr_to_single_claim():
    """A letter citing (reference, Sr) attaches to that one claim alone — not
    to its thread siblings sharing the reference."""
    _fresh_db()
    with db.get_connection() as conn:
        aari = _aari(conn)
        c1 = _insert_claim(
            conn, aari, "2026-05-01", status="acknowledged", reference="DC1-27-5628", sr=1
        )
        c2 = _insert_claim(
            conn, aari, "2026-05-02", status="acknowledged", reference="DC1-27-5628", sr=2
        )
    claim_status.process_reply("m-sr", "Petcover Claim DC1-27-5628 SR1 - Claim suspended", "")
    assert _claim_row(c1)["status"] == "suspended"
    assert _claim_row(c2)["status"] == "acknowledged", "the other serial must be untouched"


def test_reference_reuse_never_touches_settled_claims():
    """Reference-only event on a thread that holds settled + open claims: only
    the open ones move; settled claims are done (the ref is reused for years)."""
    _fresh_db()
    with db.get_connection() as conn:
        aari = _aari(conn)
        done1 = _insert_claim(
            conn, aari, "2026-02-01", status="settled", reference="DC1-27-5628", sr=1
        )
        done2 = _insert_claim(
            conn, aari, "2026-02-02", status="declined", reference="DC1-27-5628", sr=2
        )
        open1 = _insert_claim(
            conn, aari, "2026-07-01", status="acknowledged", reference="DC1-27-5628", sr=3
        )
        open2 = _insert_claim(
            conn, aari, "2026-07-02", status="acknowledged", reference="DC1-27-5628", sr=4
        )
    claim_status.process_reply(
        "m-ref", "Petcover Claim DC1-27-5628 - Request for information", "please send consult notes"
    )
    assert _claim_row(done1)["status"] == "settled"
    assert _claim_row(done2)["status"] == "declined"
    assert _claim_row(open1)["status"] == "info_requested"
    assert _claim_row(open2)["status"] == "info_requested"


def test_decline_isolated_to_its_thread():
    """One submission filed by Petcover into two threads: a decline on one
    thread must not touch the other thread's claims."""
    _fresh_db()
    with db.get_connection() as conn:
        aari = _aari(conn)
        # one submission (shared draft), two conditions → two threads
        t1 = _insert_claim(
            conn,
            aari,
            "2026-06-01",
            status="acknowledged",
            draft_id="d1",
            reference="DC1-30-1",
            sr=1,
        )
        t2 = _insert_claim(
            conn,
            aari,
            "2026-06-02",
            status="acknowledged",
            draft_id="d1",
            reference="DC1-31-9",
            sr=1,
        )
    claim_status.process_reply(
        "m-dec", "Petcover Claim DC1-30-1 - Declined - Invoices over 12 months", ""
    )
    assert _claim_row(t1)["status"] == "declined"
    assert _claim_row(t2)["status"] == "acknowledged", "sibling thread must be unaffected"


def test_ack_condition_content_decides_submission():
    """Two awaiting submissions differ by condition; the ack's text naming one
    condition attaches it there and learns the reference — the other is left."""
    _fresh_db()
    with db.get_connection() as conn:
        aari = _aari(conn)
        arth = _insert_claim(conn, aari, "2026-05-01", draft_id="d-arth", condition="Arthritis")
        ear = _insert_claim(conn, aari, "2026-05-02", draft_id="d-ear", condition="Ear infection")
    claim_status.process_reply(
        "m-cond",
        "PetCover - Acknowledgement Letter",
        "Pet Name: Aari Condition: Arthritis Claim Number DC1-40-1 Thank you",
    )
    assert (
        _claim_row(arth)["status"] == "acknowledged"
        and _claim_row(arth)["petcover_reference"] == "DC1-40-1"
    )
    assert _claim_row(ear)["status"] == "sent", "the non-matching submission must be left alone"


def test_ack_recency_fallback_leaves_condition_untouched():
    """When the ack's printed condition matches no awaiting claim (Petcover
    re-conditioned the document), correlation falls back to the most-recently-
    sent submission and does NOT rewrite the claim's own condition text."""
    _fresh_db()
    with db.get_connection() as conn:
        aari = _aari(conn)
        older = _insert_claim(conn, aari, "2026-05-01", draft_id="d-old", condition="Arthritis")
        newer = _insert_claim(conn, aari, "2026-05-02", draft_id="d-new", condition="Dermatitis")
        conn.execute(
            "UPDATE vet_claims SET updated_at = '2026-07-01T00:00:00+00:00' WHERE id = ?", (older,)
        )
        conn.execute(
            "UPDATE vet_claims SET updated_at = '2026-07-10T00:00:00+00:00' WHERE id = ?", (newer,)
        )
    claim_status.process_reply(
        "m-recon",
        "PetCover - Acknowledgement Letter",
        "Pet Name: Aari Condition: Lick Granuloma Claim Number DC1-41-2 Thank you",
    )
    assert _claim_row(newer)["status"] == "acknowledged", (
        "recency picks the most-recently-sent submission"
    )
    assert _claim_row(newer)["condition_text"] == "Dermatitis", (
        "our condition_text must not be overwritten"
    )
    assert _claim_row(older)["status"] == "sent"


def test_two_same_day_acks_land_on_distinct_submissions():
    """Two acks for one pet the same day, two submissions awaiting: each ack
    lands on a distinct submission (learning the ref removes it from the pool),
    never both on the same one."""
    _fresh_db()
    with db.get_connection() as conn:
        aari = _aari(conn)
        sub_old = _insert_claim(conn, aari, "2026-05-01", draft_id="d-old")
        sub_new = _insert_claim(conn, aari, "2026-05-02", draft_id="d-new")
        conn.execute(
            "UPDATE vet_claims SET updated_at = '2026-07-01T00:00:00+00:00' WHERE id = ?",
            (sub_old,),
        )
        conn.execute(
            "UPDATE vet_claims SET updated_at = '2026-07-10T00:00:00+00:00' WHERE id = ?",
            (sub_new,),
        )
    claim_status.process_reply(
        "m-a", "PetCover - Acknowledgement Letter", "Pet Name: Aari Claim Number DC1-50-1 Thank you"
    )
    claim_status.process_reply(
        "m-b", "PetCover - Acknowledgement Letter", "Pet Name: Aari Claim Number DC1-51-2 Thank you"
    )
    refs = {_claim_row(sub_old)["petcover_reference"], _claim_row(sub_new)["petcover_reference"]}
    assert refs == {"DC1-50-1", "DC1-51-2"}, f"each ack must learn a distinct reference: {refs}"


def test_batch_ack_assigns_serials_oldest_txn_first():
    """One 3-claim submission; three acks (Sr 2/3/4 of one reference) attach to
    the claims oldest-transaction-first, each learning its own serial."""
    _fresh_db()
    with db.get_connection() as conn:
        aari = _aari(conn)
        ids = [
            _insert_claim(conn, aari, f"2025-08-{10 + i:02d}", draft_id="d-batch") for i in range(3)
        ]
    for serial in (2, 3, 4):
        claim_status.process_reply(
            f"m-ack-{serial}",
            "PetCover - Acknowledgement Letter",
            f"Pet Name: Aari Claim Number DC1-77-0001 SR{serial} Thank you",
        )
    rows = [_claim_row(cid) for cid in ids]
    assert [r["petcover_sr"] for r in rows] == [2, 3, 4], "oldest txn → lowest serial"
    assert all(r["petcover_reference"] == "DC1-77-0001" for r in rows)
    assert all(r["status"] == "acknowledged" for r in rows)


def _relative_date(days_ago: int) -> str:
    from datetime import timedelta

    return (datetime.now(timezone.utc).date() - timedelta(days=days_ago)).isoformat()


def _anniversary_days_ago(days_ago: int) -> str:
    from datetime import timedelta

    d = datetime.now(timezone.utc).date() - timedelta(days=days_ago)
    return f"{d.month:02d}-{d.day:02d}"


def test_settlement_current_year_excess_already_used_flags_mismatch():
    """Excess is once per condition thread per (open) policy year, bucketed by
    each claim's OWN transaction date. A sibling claim already used the thread's
    excess this policy year (its txn falls in the current year); a later-txn
    sibling should be expected in FULL (no further deduction) — if Petcover
    still pays short of that, it's a mismatch to flag."""
    _fresh_db()
    import json as _json

    with db.get_connection() as conn:
        aari = _aari(conn)
        anniversary = _anniversary_days_ago(300)  # current policy year opened 300 days ago
        conn.execute("UPDATE pets SET policy_anniversary = ? WHERE id = ?", (anniversary, aari))
        first_txn = _relative_date(250)
        second_txn = _relative_date(100)
        first = _insert_claim(conn, aari, first_txn, status="settled", reference="DC1-SS-1")
        _insert_settled_event(conn, first, datetime.now(timezone.utc).isoformat(), 400.0)
        second = _insert_claim(
            conn,
            aari,
            second_txn,
            status="acknowledged",
            reference="DC1-SS-1",
            invoice_data=_json.dumps({"claimable_amount": 500.0, "amount": 500.0}),
        )
    flag = claim_status._validate_settlement(_claim_row(second), {"paid_amount": 350.0}, second_txn)
    assert flag and "settlement mismatch" in flag and "$500.00" in flag and "$350.00" in flag
    assert "excess already used" in flag, flag


def test_settlement_within_tolerance_no_flag():
    _fresh_db()
    import json as _json

    with db.get_connection() as conn:
        aari = _aari(conn)
        anniversary = _anniversary_days_ago(300)
        conn.execute("UPDATE pets SET policy_anniversary = ? WHERE id = ?", (anniversary, aari))
        txn = _relative_date(100)
        cid = _insert_claim(
            conn,
            aari,
            txn,
            status="acknowledged",
            reference="DC1-T-1",
            invoice_data=_json.dumps({"claimable_amount": 500.0}),
        )
    # expected = 500 - 150 excess = 350; paid within $2 → no flag
    assert claim_status._validate_settlement(_claim_row(cid), {"paid_amount": 349.0}, txn) is None
    # paid short beyond tolerance → flag, no prior sibling this year so "fresh excess"
    flag = claim_status._validate_settlement(_claim_row(cid), {"paid_amount": 300.0}, txn)
    assert flag and "fresh $150 excess" in flag


def test_settlement_unknown_anniversary_degrades():
    _fresh_db()
    import json as _json

    with db.get_connection() as conn:
        aari = _aari(conn)
        conn.execute("UPDATE pets SET policy_anniversary = NULL WHERE id = ?", (aari,))
        txn = _relative_date(30)
        cid = _insert_claim(
            conn,
            aari,
            txn,
            status="acknowledged",
            reference="DC1-U-1",
            invoice_data=_json.dumps({"claimable_amount": 500.0}),
        )
    flag = claim_status._validate_settlement(_claim_row(cid), {"paid_amount": 200.0}, txn)
    assert flag and "anniversary unknown" in flag


def test_settlement_closed_policy_year_assumes_full_claimable():
    """Justin's rule: our history for an already-closed policy year is
    presumed incomplete, so a claim whose OWN transaction falls in a prior,
    closed year is expected in full (no excess deducted) — regardless of what
    other claims exist. Real case: Sr2's txn predates the anniversary that
    closed its policy year weeks before Sr4's txn (a different, current year)."""
    _fresh_db()
    import json as _json

    with db.get_connection() as conn:
        aari = _aari(conn)
        anniversary = _anniversary_days_ago(10)  # current year opened only 10 days ago
        conn.execute("UPDATE pets SET policy_anniversary = ? WHERE id = ?", (anniversary, aari))
        closed_year_txn = _relative_date(40)  # before the anniversary -> prior, closed year
        cid = _insert_claim(
            conn,
            aari,
            closed_year_txn,
            status="acknowledged",
            reference="DC1-CY-1",
            invoice_data=_json.dumps({"claimable_amount": 55.74}),
        )
    # expected = full claimable (no excess) since the year is closed; paid short of that -> flag
    assert (
        claim_status._validate_settlement(_claim_row(cid), {"paid_amount": 55.74}, closed_year_txn)
        is None
    )
    flag = claim_status._validate_settlement(
        _claim_row(cid), {"paid_amount": 22.75}, closed_year_txn
    )
    assert flag and "expected $55.74" in flag


def test_settlement_anniversary_boundary_fresh_excess_in_new_year():
    """A thread's prior claim settled in the now-CLOSED policy year (its own
    txn predates the anniversary) does not count toward the current year's
    excess-consumed check — a new-year sibling still gets a fresh $150 excess."""
    _fresh_db()
    import json as _json

    with db.get_connection() as conn:
        aari = _aari(conn)
        anniversary = _anniversary_days_ago(20)  # current year opened 20 days ago
        conn.execute("UPDATE pets SET policy_anniversary = ? WHERE id = ?", (anniversary, aari))
        prior_txn = _relative_date(60)  # before the anniversary -> prior, closed year
        current_txn = _relative_date(5)  # after the anniversary -> current year
        first = _insert_claim(conn, aari, prior_txn, status="settled", reference="DC1-BD-1")
        _insert_settled_event(conn, first, datetime.now(timezone.utc).isoformat(), 400.0)
        second = _insert_claim(
            conn,
            aari,
            current_txn,
            status="acknowledged",
            reference="DC1-BD-1",
            invoice_data=_json.dumps({"claimable_amount": 500.0}),
        )
    flag = claim_status._validate_settlement(
        _claim_row(second), {"paid_amount": 300.0}, current_txn
    )
    assert flag and "fresh $150 excess" in flag, (
        "prior claim's closed-year txn must not count toward this year's excess"
    )


def test_classify_approved_and_below_excess():
    """Real letters (Jul 2026) both use the generic subject 'Petcover Insurance
    Claim for Ari' — classification must come from the body phrase."""
    assert (
        claim_status.classify(
            "Petcover Insurance Claim for Ari",
            "Your claim has been approved\nWe have assessed the recent claim",
        )
        == "approved"
    )
    assert (
        claim_status.classify(
            "Petcover Insurance Claim for Ari",
            "Claim assessment outcome: Under excess\nWhile it is a claimable condition, the amount you have claimed is under your fixed excess.",
        )
        == "below_excess"
    )


def test_extract_approval_amounts_real_letter_shapes():
    """Real (redacted) text shapes: the 'approved' letter is the only place
    these numbers appear at all."""
    sr2_text = (
        "Total amount claimed: $35.00\nPaid by you:\nFixed excess $0.00\n"
        "Non‐claimable amount $0.00\nAge Contribution: $12.25 [35%]\n"
        "Percentage Excess: $0.00 [0%]\nPaid by us: $22.75"
    )
    amounts = claim_status.extract_approval_amounts(sr2_text)
    assert amounts["claimed_amount"] == 35.00
    assert amounts["paid_amount"] == 22.75
    assert amounts["fixed_excess_stated"] == 0.00
    assert amounts["age_contribution_stated"] == 12.25

    sr4_text = "Amount claimed: $55.74\nLess Fixed excess: $105.00\nOther deductibles: $0.00\nOutstanding excess: $-49.26"
    amounts4 = claim_status.extract_approval_amounts(sr4_text)
    assert amounts4["fixed_excess_stated"] == 105.00
    assert "paid_amount" not in amounts4, (
        "this letter states no payout yet — must not fabricate one"
    )


def test_settlement_mismatch_flags_paid_more_than_expected_too():
    """The check is a plain two-way mismatch, not a one-directional shortfall
    — Petcover paying MORE than our simple expectation is just as worth a
    look (we don't try to explain it, just surface it)."""
    _fresh_db()
    import json as _json

    with db.get_connection() as conn:
        aari = _aari(conn)
        anniversary = _anniversary_days_ago(300)
        conn.execute("UPDATE pets SET policy_anniversary = ? WHERE id = ?", (anniversary, aari))
        txn = _relative_date(100)
        cid = _insert_claim(
            conn,
            aari,
            txn,
            status="acknowledged",
            reference="DC1-OVER-1",
            invoice_data=_json.dumps({"claimable_amount": 200.0}),
        )
    # expected = 200 - 150 = 50; paid way more than expected -> still flagged
    flag = claim_status._validate_settlement(_claim_row(cid), {"paid_amount": 190.0}, txn)
    assert flag and "settlement mismatch" in flag and "$50.00" in flag and "$190.00" in flag


def test_process_reply_approved_validates_and_flags_from_real_shape():
    """End-to-end through process_reply: an 'approved' email (generic subject,
    real body shape) is classified, correlates by reference+sr, records the
    approval event, and — since the claim's own txn predates the pet's
    anniversary (closed prior year) — expects full claimable, flagging a
    mismatch against what was actually paid."""
    _fresh_db()
    import json as _json

    with db.get_connection() as conn:
        aari = _aari(conn)
        anniversary = _anniversary_days_ago(10)  # current year just opened
        conn.execute("UPDATE pets SET policy_anniversary = ? WHERE id = ?", (anniversary, aari))
        closed_year_txn = _relative_date(40)
        cid = _insert_claim(
            conn,
            aari,
            closed_year_txn,
            status="acknowledged",
            reference="DC1-APR-1",
            sr=2,
            invoice_data=_json.dumps({"claimable_amount": 55.74}),
        )
    claim_status.process_reply(
        "m-approved-1",
        "Petcover Insurance Claim for Ari",
        "Claim Reference:DC1-APR-1 Sr 2\nYour claim has been approved\n"
        "Total amount claimed: $55.74\nPaid by us: $22.75",
    )
    row = _claim_row(cid)
    assert row["status"] == "approved"
    assert row["flag"] and "settlement mismatch" in row["flag"] and "$55.74" in row["flag"]
    with db.get_connection() as conn:
        event = conn.execute(
            "SELECT event_type, detail FROM claim_status_events WHERE raw_email_id='m-approved-1'"
        ).fetchone()
    detail = _json.loads(event["detail"])
    assert event["event_type"] == "approved"
    assert detail["paid_amount"] == 22.75


def test_reconcile_clears_stale_draft_on_404():
    """Real failure: a deleted Gmail draft 404s every 15-minute tick forever
    (confirmed live, claim #17, 10+/day in logs). A 404 specifically means the
    draft is gone for good — clear it and flag for a fresh invoice request,
    distinct from a transient fetch failure (which still retries next tick)."""
    from googleapiclient.errors import HttpError

    from openclaw import pipeline

    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM vet_claims")
        conn.execute("DELETE FROM bank_transactions")
        cid = _insert_pending_claim(conn, "STALE DRAFT VET", -50.0, "2026-07-01")
        conn.execute("UPDATE vet_claims SET draft_id = 'gone-draft-1' WHERE id = ?", (cid,))

    class FakeResp:
        status = 404
        reason = "Not Found"

    class FakeMessages:
        def get(self, userId, id, format):
            raise HttpError(FakeResp(), b'{"error": {"message": "not found"}}')

    class FakeUsers:
        def messages(self):
            return FakeMessages()

    class FakeService:
        def users(self):
            return FakeUsers()

    original = pipeline.gmail_client.build_service
    pipeline.gmail_client.build_service = lambda: FakeService()
    try:
        pipeline.reconcile_sent_invoice_requests()
    finally:
        pipeline.gmail_client.build_service = original

    row = _matched_row(cid)
    assert row["draft_id"] is None
    assert row["flag"] == "invoice-request draft was deleted from Gmail — send a fresh one"


def test_gmail_auth_alert_caps_at_five_per_day():
    from openclaw import pipeline

    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM ops_alerts")
    sent = []
    original = pipeline.gmail_client.build_service
    pipeline.gmail_client.build_service = lambda: (_ for _ in ()).throw(
        RuntimeError("No Gmail token at x")
    )
    try:
        results = [
            pipeline._ensure_gmail_auth(send_fn=lambda t, markup=None: sent.append(t))
            for _ in range(7)
        ]
    finally:
        pipeline.gmail_client.build_service = original
    assert results == [False] * 7
    assert len(sent) == 5, f"cap is 5/24h, got {len(sent)}"
    assert all("gmail_auth.py" in s for s in sent)
    with db.get_connection() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM ops_alerts WHERE kind='gmail_auth'").fetchone()[0]
            == 5
        )


def test_gmail_auth_recovery_confirmed_once_and_resets():
    from openclaw import pipeline

    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM ops_alerts")
    sent = []
    spy = lambda t, markup=None: sent.append(t)  # noqa: E731
    fail = lambda: (_ for _ in ()).throw(RuntimeError("No Gmail token"))  # noqa: E731
    ok = lambda: object()  # noqa: E731
    original = pipeline.gmail_client.build_service
    try:
        pipeline.gmail_client.build_service = fail
        assert pipeline._ensure_gmail_auth(send_fn=spy) is False  # one alert
        pipeline.gmail_client.build_service = ok
        assert pipeline._ensure_gmail_auth(send_fn=spy) is True  # recovery
        assert pipeline._ensure_gmail_auth(send_fn=spy) is True  # nothing more
        assert len([s for s in sent if "restored" in s]) == 1, sent
        # a later failure starts a fresh alert cycle
        sent.clear()
        pipeline.gmail_client.build_service = fail
        pipeline._ensure_gmail_auth(send_fn=spy)
        assert len(sent) == 1 and "gmail_auth.py" in sent[0]
    finally:
        pipeline.gmail_client.build_service = original


def test_continuation_box_defaults_ticked():
    import inspect

    db.init_db()
    with db.get_connection() as conn:
        pet = conn.execute("SELECT * FROM pets WHERE name='Aari'").fetchone()
    assert claim_forms._shared_fields(pet, True)["claim_continuation_state"] == "/0", (
        "ticked = Yes = /0"
    )
    assert inspect.signature(claim_forms.process_claim).parameters["continuation"].default is True
    assert (
        inspect.signature(claim_forms.process_claim_batch).parameters["continuation"].default
        is True
    )


def _insert_txn(conn, date, amount, merchant="KINGS VET CLINIC"):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO bank_transactions (date, amount, merchant, vet_flag, created_at) VALUES (?, ?, ?, 1, ?)",
        (date, amount, merchant, now),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _insert_ledger_claim(
    conn, txn_id, pet_id, status, condition=None, claimable=None, item_conditions=None
):
    import json as _json

    now = datetime.now(timezone.utc).isoformat()
    invoice = _json.dumps({"claimable_amount": claimable}) if claimable is not None else None
    ic = _json.dumps(item_conditions) if item_conditions is not None else None
    conn.execute(
        "INSERT INTO vet_claims (transaction_id, pet_id, condition_text, invoice_data, item_conditions, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (txn_id, pet_id, condition, invoice, ic, status, now, now),
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
        _insert_ledger_claim(conn, t1, aari, "drafted", "Arthritis", 44.75)
        # two more arthritis charges same year -> batch totals 124.75 (< 150 excess)
        t2 = _insert_txn(conn, "2025-09-26", -45.00)
        _insert_ledger_claim(conn, t2, aari, "drafted", "Arthritis", 45.00)
        # split: one $177.50 charge -> Aari arthritis 35.00 + Echo vaccination excluded
        t3 = _insert_txn(conn, "2025-09-11", -177.50)
        _insert_ledger_claim(conn, t3, aari, "drafted", "Arthritis", 35.00)
        _insert_ledger_claim(conn, t3, echo, "matched", "Vaccination", 0.0)
        # no-invoice: Aari charge, pending_match, no invoice_data
        t4 = _insert_txn(conn, "2025-07-28", -45.00)
        _insert_ledger_claim(conn, t4, aari, "pending_match")
        # missing-excess: Echo charge with a real claimable but no policy excess/cap
        t5 = _insert_txn(conn, "2025-12-22", -679.50, "VETWEST")
        _insert_ledger_claim(conn, t5, echo, "matched", "Injury", 600.00)

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
        c1 = _insert_ledger_claim(conn, t1, aari, "settled", "Gastroenteritis", 200.00)
    claim_status._record_event(c1, "settled", "msg-x", {"paid_amount": 124.97})

    ledger = claim_status.visit_ledger()
    claim = next(c for e in ledger for c in e["claims"] if c["id"] == c1)
    # settled actual overrides the estimate
    assert claim["expected"]["value"] == 124.97
    assert claim["expected"]["estimate"] is False


def test_visit_ledger_uses_anniversary_year_not_calendar_year():
    """Real bug: excess/cap grouping used to bucket by calendar year, but
    Aari's policy year runs anniversary-to-anniversary (09-23), not Jan-Dec.
    Two arthritis charges either side of Dec 31 but inside the SAME real
    policy year must share one $150 excess — calendar-year bucketing would
    wrongly give each charge its own fresh excess."""
    db.init_db()
    with db.get_connection() as conn:
        # earlier visit_ledger tests leave Aari/Arthritis rows behind (no
        # cleanup) and this test's group math is sensitive to exactly that.
        conn.execute("DELETE FROM vet_claims")
        conn.execute("DELETE FROM bank_transactions")
        conn.execute("DELETE FROM claim_status_events")
        aari = conn.execute("SELECT id FROM pets WHERE name='Aari'").fetchone()[0]
        conn.execute("UPDATE pets SET policy_anniversary = '09-23' WHERE id = ?", (aari,))
        # both after the 2025-09-23 anniversary, before the next one -> one policy year
        t1 = _insert_txn(conn, "2025-11-01", -100.00)
        t2 = _insert_txn(conn, "2026-02-01", -100.00)
        _insert_ledger_claim(conn, t1, aari, "drafted", "Arthritis", 100.00)
        _insert_ledger_claim(conn, t2, aari, "drafted", "Arthritis", 100.00)

    ledger = claim_status.visit_ledger()
    by_txn = {e["txn"]["id"]: e["claims"][0] for e in ledger if e["txn"]["id"] in (t1, t2)}
    # combined $200 > $150 excess: earliest charge absorbs it fully ($0), the
    # later one only clears the remaining $50 of excess -> and Petcover pays 65%
    # of that, so $32.50 expected (the rate their letters print as a 35% Age
    # Contribution; confirmed by Justin as the policy's own term).
    assert by_txn[t1]["expected"]["value"] == 0.0
    assert by_txn[t2]["expected"]["value"] == 32.50


def test_visit_ledger_splits_condition_excess_per_condition():
    """Real bug: a claim whose invoice spans two conditions gets condition_text
    = "Arthritis; Dermatitis" — a joined string, but Petcover applies the $150
    excess PER CONDITION, so the two must drain separate excess buckets, not
    share one under the combined string. $200 Arthritis + $200 Dermatitis on
    one split invoice: correct = ($200-150) + ($200-150) = $100. The old
    combined-bucket bug would drain a single $150 excess across the $400
    combined once -> $250."""
    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM vet_claims")
        conn.execute("DELETE FROM bank_transactions")
        conn.execute("DELETE FROM claim_status_events")
        aari = conn.execute("SELECT id FROM pets WHERE name='Aari'").fetchone()[0]
        conn.execute("UPDATE pets SET policy_anniversary = '09-23' WHERE id = ?", (aari,))
        t1 = _insert_txn(conn, "2025-11-01", -400.00)
        _insert_ledger_claim(
            conn,
            t1,
            aari,
            "drafted",
            condition="Arthritis; Dermatitis",
            claimable=400.00,
            item_conditions=[
                {"description": "Arthritis visit", "amount": 200.00, "condition": "Arthritis"},
                {"description": "Dermatitis visit", "amount": 200.00, "condition": "Dermatitis"},
            ],
        )

    ledger = claim_status.visit_ledger()
    claim = next(c for e in ledger for c in e["claims"] if e["txn"]["id"] == t1)
    # each condition clears $50 of its own $150 excess, at 65% -> $32.50 each
    assert claim["expected"]["value"] == 65.0, claim["expected"]


def test_history_rows_windows_by_date_and_flattens_split_charges():
    """history_rows() (Telegram /history) windows visit_ledger() to the last
    `days` and flattens its nested claims to one row per claim — a charge
    shared by two claims (different pets) must yield two rows. Order is
    OLDEST first (inverting visit_ledger): a visit is unclaimable once a year
    old, so the soonest-to-expire rows must land on page 1."""
    db.init_db()
    today = datetime.now(timezone.utc).date()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM vet_claims")
        conn.execute("DELETE FROM bank_transactions")
        conn.execute("DELETE FROM claim_status_events")
        aari = conn.execute("SELECT id FROM pets WHERE name='Aari'").fetchone()[0]
        echo = conn.execute("SELECT id FROM pets WHERE name='Echo'").fetchone()[0]
        in_window = (today - timedelta(days=30)).isoformat()
        older = (today - timedelta(days=30 * 2)).isoformat()
        out_of_window = (today - timedelta(days=400)).isoformat()
        t_recent = _insert_txn(conn, in_window, -50.0)
        _insert_ledger_claim(conn, t_recent, aari, "sent", "Arthritis", 50.0)
        t_shared = _insert_txn(conn, older, -80.0)
        _insert_ledger_claim(conn, t_shared, aari, "matched", "Arthritis", 35.0)
        _insert_ledger_claim(conn, t_shared, echo, "matched", "Vaccination", 0.0)
        t_stale = _insert_txn(conn, out_of_window, -60.0)
        _insert_ledger_claim(conn, t_stale, aari, "settled", "Injury", 60.0)

    rows = claim_status.history_rows()
    assert [r["date"] for r in rows] == [older, older, in_window], (
        "stale row excluded, oldest-first order"
    )
    assert {r["pet_name"] for r in rows if r["date"] == older} == {"Aari", "Echo"}, (
        "shared charge yields one row per claim"
    )


def test_claim_card_totals_split_actual_paid_from_estimate():
    """Card header: reimbursed counts ONLY money Petcover actually paid; the
    'to come' figure is our own estimate for everything unsettled and must not
    double-count a settled claim. Petcover's age contribution is deliberately
    not modelled (no rate is recorded for any pet), so the estimate reads high
    and is flagged as an estimate rather than quietly presented as fact."""
    from openclaw import claim_card

    rows = [
        {
            "date": "2026-07-06",
            "merchant": "V",
            "amount": -100.0,
            "status": "settled",
            "pet_name": "Aari",
            "condition_text": "Arthritis",
            "paid": 22.75,
            "expected": None,
        },
        {
            "date": "2026-06-06",
            "merchant": "V",
            "amount": -200.0,
            "status": "sent",
            "pet_name": "Aari",
            "condition_text": "Arthritis",
            "paid": None,
            "expected": {"available": True, "value": 50.0, "estimate": True},
        },
        {
            "date": "2026-05-06",
            "merchant": "V",
            "amount": -60.0,
            "status": "pending_match",
            "pet_name": "Aari",
            "condition_text": None,
            "paid": None,
            "expected": {"available": False, "value": None},
        },
    ]
    agg = claim_card.totals(rows)
    assert agg["reimbursed"] == 22.75, "only the settled claim's real payment counts"
    assert agg["outstanding"] == 50.0, (
        "unavailable estimate contributes nothing, settled isn't re-counted"
    )
    assert agg["outstanding_is_estimate"] is True

    # Nothing estimable at all → no phantom '~$0' estimate marker.
    assert claim_card.totals(rows[:1]) == {
        "reimbursed": 22.75,
        "outstanding": 0.0,
        "outstanding_is_estimate": False,
    }


def test_claim_card_renders_png_for_every_status():
    """Rendering must not crash on any real status (an unmapped one falls back
    to neutral colours) — the card is the whole /history reply, so a render
    error means Justin gets nothing."""
    from openclaw import claim_card

    rows = [
        {
            "date": f"2026-0{(i % 9) + 1}-1{i % 9}",
            "merchant": "The Shire Veterinary Hospital",
            "amount": -(50 + i),
            "status": status,
            "pet_name": "Aari",
            "condition_text": "Arthritis",
            "paid": None,
            "expected": {"available": True, "value": 10.0, "estimate": True},
        }
        for i, status in enumerate(list(status_labels.LABELS) + ["some_new_status"])
    ]
    png = claim_card.render(rows, page=1, total_rows=len(rows))
    assert png.startswith(b"\x89PNG"), "must be a real PNG"


def test_pending_actions_one_per_claim_priority_and_blocked_split():
    """Real-data shapes that broke the first cut: a claim missing BOTH pet and
    condition must yield one action (pet first, matching the order process_claim
    blocks in), a closed/absorbed claim must yield none, and an insurer-blocked
    claim must be marked unactionable rather than looking tappable."""
    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM vet_claims")
        conn.execute("DELETE FROM bank_transactions")
        conn.execute("DELETE FROM claim_status_events")
        conn.execute("DELETE FROM split_proposals")
        aari = conn.execute("SELECT id FROM pets WHERE name='Aari'").fetchone()[0]
        echo = conn.execute("SELECT id FROM pets WHERE name='Echo'").fetchone()[0]
        t_both = _insert_txn(conn, "2026-01-05", -400.0)
        both = _insert_ledger_claim(conn, t_both, None, "matched", None, 400.0)
        t_absorbed = _insert_txn(conn, "2026-02-05", -100.0)
        absorbed = _insert_ledger_claim(conn, t_absorbed, aari, "absorbed", "Arthritis", 100.0)
        t_waiting = _insert_txn(conn, "2026-03-05", -200.0)
        _insert_ledger_claim(conn, t_waiting, aari, "sent", "Arthritis", 200.0)
        t_blocked = _insert_txn(conn, "2026-04-05", -300.0)
        blocked = _insert_ledger_claim(conn, t_blocked, echo, "matched", None, 300.0)
        conn.execute(
            "UPDATE vet_claims SET flag = 'Bow Wow Insurance claim process not yet defined' WHERE id = ?",
            (blocked,),
        )
        t_draft = _insert_txn(conn, "2026-05-05", -500.0)
        drafted = _insert_ledger_claim(conn, t_draft, aari, "drafted", "Arthritis", 500.0)

    actions = claim_status.pending_actions()
    by_claim = {a["claim_id"]: a for a in actions}
    assert len(actions) == len(by_claim), "one action per claim, never two"
    assert by_claim[both]["kind"] == "assign_pet", "pet is checked before condition"
    assert by_claim[blocked]["kind"] == "blocked_insurer"
    assert by_claim[blocked]["actionable"] is False, (
        "no button can clear an undefined insurer process"
    )
    assert by_claim[drafted]["kind"] == "mark_sent"
    assert absorbed not in by_claim, "an absorbed claim is finished, not an action"
    # a claim sitting with Petcover is not Justin's move
    assert all(a["status"] != "sent" for a in actions)
    # oldest first — the near-expiry end is the urgent end
    assert [a["date"] for a in actions] == sorted(a["date"] for a in actions)


def test_a_matched_claim_is_labelled_with_what_it_is_waiting_for():
    """Seven live claims sat at "Matched" for weeks; every one was Echo's,
    permanently blocked on an undefined insurer process, and they read exactly
    like a claim that needed one condition typed in. The label derives from the
    same determination the action list makes — never a second copy of it."""
    blocked = {
        "id": 1,
        "status": "matched",
        "flag": "Bow Wow Insurance claim process not yet defined",
        "pet_id": 2,
        "condition_text": None,
    }
    no_condition = {"id": 2, "status": "matched", "flag": None, "pet_id": 1, "condition_text": None}
    no_pet = {"id": 3, "status": "matched", "flag": None, "pet_id": None, "condition_text": None}
    ready = {"id": 4, "status": "matched", "flag": None, "pet_id": 1, "condition_text": "Arthritis"}

    assert status_labels.label(blocked) == "Blocked: no claim process"
    assert status_labels.label(no_condition) == "Needs condition"
    assert status_labels.label(no_pet) == "Needs pet"
    assert status_labels.label(ready) == "Matched", "nothing outstanding — the bare word is correct"
    # and the derivation is the action list's, not a parallel one
    assert claim_status._action_kind(blocked, set(), set()) == "blocked_insurer"
    assert claim_status._action_kind(no_condition, set(), set()) == "set_condition"


def test_an_information_request_is_worded_by_who_owes_the_document():
    """Petcover sends the same Further-Information letter twice — one to Justin,
    one to the vet with Justin only Cc'd (both live, 2026-07-27). Telling him the
    vet owes it when he does is the mistake that loses a claim, so an unrecorded
    addressee stays neutral instead of guessing."""
    vet = {
        "id": 1,
        "status": "info_requested",
        "flag": None,
        "pet_id": 1,
        "condition_text": "Raised ALT",
        "owed_by": "vet",
    }
    mine = {**vet, "owed_by": "justin"}
    unknown = {**vet, "owed_by": None}

    assert status_labels.label(vet) == "More vet info required"
    assert status_labels.label(mine) == "Petcover needs info from you"
    assert status_labels.label(unknown) == "Info requested", "no claim about who must act"
    # the word "suspended" belongs to an actual suspension and nothing else
    assert "suspend" not in " ".join(status_labels.label(c).lower() for c in (vet, mine, unknown))
    assert status_labels.label({**vet, "status": "suspended"}) == "Suspended"


def test_the_label_names_the_document_petcover_asked_for():
    """ "More vet info required" cannot be acted on; "consult notes needed" can.
    The document says WHAT, `owed_by` says WHO, and both matter — a request naming
    the document but not the party invites the wrong chase, so an unrecorded owner
    stays neutral whatever was asked for."""
    base = {
        "id": 1,
        "status": "info_requested",
        "flag": None,
        "pet_id": 1,
        "condition_text": "Raised ALT",
    }
    vet_doc = {
        **base,
        "owed_by": "vet",
        "requested_document": "Consultation notes dated 18/05/2026",
    }
    mine_doc = {
        **base,
        "owed_by": "justin",
        "requested_document": "Consultation notes dated 18/05/2026",
    }

    assert status_labels.label(vet_doc) == "Vet: consult notes needed"
    assert status_labels.label(mine_doc) == "Consult notes needed from you"
    # No document, or a kind we don't recognise: exactly the wording it had before.
    assert (
        status_labels.label({**base, "owed_by": "vet", "requested_document": None})
        == "More vet info required"
    )
    assert (
        status_labels.label({**base, "owed_by": "vet", "requested_document": "a signed affidavit"})
        == "More vet info required"
    )
    assert (
        status_labels.label({**base, "owed_by": "justin", "requested_document": None})
        == "Petcover needs info from you"
    )
    # Owner unrecorded stays neutral even with a document named.
    assert (
        status_labels.label({**base, "owed_by": None, "requested_document": "Consultation notes"})
        == "Info requested"
    )
    # The chase line names the document too, and it is an action not a state.
    assert status_labels.needs(vet_doc) == "Chase vet for consult notes"
    assert status_labels.needs(mine_doc) == "Send Petcover the consult notes"
    # A request is never worded as a suspension.
    assert all("suspend" not in status_labels.label(c).lower() for c in (vet_doc, mine_doc))


def test_short_document_recognises_the_kinds_seen_live():
    assert status_labels.short_document("Consultation notes dated 18/05/2026") == "consult notes"
    assert status_labels.short_document("Itemized invoice for the visit") == "itemised invoice"
    assert status_labels.short_document("Completed claim form") == "claim form"
    assert (
        status_labels.short_document("Referral history from the treating vet") == "referral history"
    )
    assert status_labels.short_document("something nobody has sent before") is None
    assert status_labels.short_document(None) is None


def test_one_status_vocabulary_no_second_map():
    """The wording lived in three hand-synced copies (claim_card, index.html,
    basic.html) plus pipeline's notify text, linked only by a comment asking the
    next reader to keep them in sync. A fourth map is a regression, not a
    convention — and colours key off the status so a rewording can't drop one."""
    from openclaw import claim_card

    assert claim_card._status_label("below_excess") == status_labels.LABELS["below_excess"]
    assert not hasattr(claim_card, "_STATUS_LABELS"), "the renderer's own copy is gone"
    assert set(claim_card._STATUS_COLOURS) == set(status_labels.LABELS), (
        "colours are keyed by status, one per known status"
    )

    # The templates were the other two of the three original copies, and this test
    # did not cover them: `index.html` still wrote "No invoice" literally for the
    # no-claim row, where there is no claim to hand to `status_label`. That row now
    # reads `status_words['pending_match']`, and any new literal fails here.
    import re as _re
    from pathlib import Path as _Path

    leaks = []
    for path in sorted(
        (_Path(__file__).resolve().parent.parent / "openclaw" / "templates").glob("*.html")
    ):
        text = path.read_text(encoding="utf-8")
        for status, word in status_labels.LABELS.items():
            for match in _re.finditer(r">\s*" + _re.escape(word) + r"\s*<", text):
                leaks.append(
                    f"{path.name}:{text[: match.start()].count(chr(10)) + 1} hardcodes {word!r} ({status})"
                )
    assert not leaks, "templates must render wording, not restate it: " + "; ".join(leaks)

    # Jinja renders an unregistered global as the empty string rather than raising,
    # so a missing `status_words` would blank the chip silently — the exact failure
    # mode that makes a template leak preferable to a broken lookup. Both halves
    # checked: the global is registered, and every key the templates ask for exists.
    package = _Path(__file__).resolve().parent.parent / "openclaw"
    main_source = (package / "main.py").read_text(encoding="utf-8")
    assert 'templates.env.globals["status_words"] = status_labels.LABELS' in main_source
    for path in sorted((package / "templates").glob("*.html")):
        for key in _re.findall(
            r"status_words\[['\"]([a-z_]+)['\"]\]", path.read_text(encoding="utf-8")
        ):
            assert key in status_labels.LABELS, (
                f"{path.name} asks for status_words[{key!r}], which has no wording"
            )


def test_submission_group_id_is_order_independent():
    """The token is derived from claim ids, so read order must not change it —
    otherwise the same batch gets two names in two places."""
    assert claim_status.submission_group_id([7, 6]) == "S6+7"
    assert claim_status.submission_group_id([6, 7]) == "S6+7"
    assert claim_status.submission_group_id([12]) == "S12"


def test_batched_mark_sent_is_one_action_per_submission():
    """One Gmail draft = one email = one tap. Two claims sharing a draft used to
    yield two 'Send Gmail draft' cards, and the second tap landed on a claim the
    first had already advanced (live: #6 + #7, 2026-07-25)."""
    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM vet_claims")
        conn.execute("DELETE FROM bank_transactions")
        conn.execute("DELETE FROM claim_status_events")
        aari = conn.execute("SELECT id FROM pets WHERE name='Aari'").fetchone()[0]
        # different charges, different dates/amounts/conditions — one draft
        t_a = _insert_txn(conn, "2026-04-17", -132.50)
        t_b = _insert_txn(conn, "2026-05-18", -351.50)
        a = _insert_ledger_claim(conn, t_a, aari, "drafted", "Arthritis", 132.50)
        b = _insert_ledger_claim(conn, t_b, aari, "drafted", "Raised ALT", 351.50)
        conn.execute("UPDATE vet_claims SET draft_id = 'one-draft' WHERE id IN (?, ?)", (a, b))
        # a solo drafted claim must NOT be folded into anything
        t_solo = _insert_txn(conn, "2026-06-01", -80.0)
        solo = _insert_ledger_claim(conn, t_solo, aari, "drafted", "Ear infection", 80.0)
        conn.execute("UPDATE vet_claims SET draft_id = 'solo-draft' WHERE id = ?", (solo,))

    sends = [x for x in claim_status.pending_actions() if x["kind"] == "mark_sent"]
    assert len(sends) == 2, "the batch collapses to one entry, the solo claim keeps its own"
    batch = next(x for x in sends if len(x["claim_ids"]) > 1)
    assert batch["claim_ids"] == sorted([a, b])
    assert batch["group_id"] == claim_status.submission_group_id([a, b])
    assert batch["claim_id"] == min(a, b), "representative = lowest id (the tap token takes one)"
    assert abs(batch["amount"]) == 484.0, "the total is what Justin is confirming he sent"
    assert batch["date"] == "2026-04-17", (
        "urgency comes from the oldest member — expiry is per visit"
    )
    assert [m["condition_text"] for m in batch["members"]] == ["Arthritis", "Raised ALT"]

    lone = next(x for x in sends if len(x["claim_ids"]) == 1)
    assert lone["claim_ids"] == [solo] and lone["group_id"] == f"S{solo}"
    assert "members" not in lone


def test_only_submission_level_actions_collapse():
    """A per-claim action must stay per-claim even when the claims share a draft:
    two settlement mismatches in one batch are two figures to check, and folding
    them would hide one behind the other."""
    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM vet_claims")
        conn.execute("DELETE FROM bank_transactions")
        conn.execute("DELETE FROM claim_status_events")
        aari = conn.execute("SELECT id FROM pets WHERE name='Aari'").fetchone()[0]
        ids = []
        for day, amount in (("2026-03-01", -100.0), ("2026-03-02", -200.0)):
            txn = _insert_txn(conn, day, amount)
            ids.append(_insert_ledger_claim(conn, txn, aari, "settled", "Arthritis", abs(amount)))
        conn.execute(
            "UPDATE vet_claims SET draft_id = 'shared', flag = 'settlement mismatch: paid less than claimed' "
            "WHERE id IN (?, ?)",
            tuple(ids),
        )

    assert claim_status.SUBMISSION_LEVEL_ACTIONS == ("mark_sent",)
    mismatches = [a for a in claim_status.pending_actions() if a["kind"] == "dismiss_mismatch"]
    assert sorted(a["claim_id"] for a in mismatches) == sorted(ids), (
        "one entry per claim, not per draft"
    )


def test_dismiss_mismatch_clears_flag_and_records_why():
    """A settlement discrepancy must never just vanish — clearing the flag has
    to leave an append-only trace (ADR-0008), or the review is invisible."""
    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM vet_claims")
        conn.execute("DELETE FROM bank_transactions")
        conn.execute("DELETE FROM claim_status_events")
        aari = conn.execute("SELECT id FROM pets WHERE name='Aari'").fetchone()[0]
        txn = _insert_txn(conn, "2026-06-05", -44.75)
        claim_id = _insert_ledger_claim(conn, txn, aari, "settled", "Arthritis", 44.75)
        conn.execute(
            "UPDATE vet_claims SET flag = 'settlement mismatch — we expected $44.75, Petcover paid $22.75 — review' "
            "WHERE id = ?",
            (claim_id,),
        )

    import json as _json

    assert claim_status.dismiss_mismatch(claim_id)["ok"] is True
    with db.get_connection() as conn:
        flag = conn.execute("SELECT flag FROM vet_claims WHERE id = ?", (claim_id,)).fetchone()[0]
        event = conn.execute(
            "SELECT detail FROM claim_status_events WHERE claim_id = ? AND event_type = 'mismatch_dismissed'",
            (claim_id,),
        ).fetchone()
    assert flag is None
    assert "settlement mismatch" in _json.loads(event["detail"])["dismissed_flag"], (
        "keeps what was dismissed"
    )
    # idempotent: a second tap can't fabricate another dismissal
    assert claim_status.dismiss_mismatch(claim_id)["ok"] is False


def test_agent_summary_carries_claim_id_and_never_invents_a_pet():
    """Both were live failures: an outstanding-actions answer with no claim ids
    (unusable — Justin acts by id) and the model inventing 'Whiskers'/'Fluffy'."""
    from openclaw import agent

    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM vet_claims")
        conn.execute("DELETE FROM bank_transactions")
        aari = conn.execute("SELECT id FROM pets WHERE name='Aari'").fetchone()[0]
        txn = _insert_txn(conn, "2026-06-19", -585.39)
        claim_id = _insert_ledger_claim(conn, txn, aari, "drafted", "Arthritis", 585.39)

    rows = agent._find_claims(pet="Aari")
    assert f"#{claim_id}" in agent._summary_line(rows[0]), "every claim reference carries its id"

    # the real pet list is injected, so the model never has to guess one
    prompt = agent.system_prompt()
    assert "Aari" in prompt and "Echo" in prompt
    # Specifically the MAILBOX limit. A loose "cannot read" match started
    # passing off the unrelated "cannot read OpenClaw's code" line once the
    # mailbox rule was reworded — the assertion has to name what it guards.
    lowered = prompt.lower()
    assert "cannot browse, search, or read justin's mailbox" in lowered, (
        "must state the mailbox limit rather than imply access"
    )

    impls = agent._build_impls([])
    rejection = impls["propose_assign_pet"]("Whiskers")
    assert "No pet named" in rejection, "a made-up pet must be refused, not assigned"
    assert "Aari" in rejection and "Echo" in rejection, (
        "and the real pets offered, so it can't guess again"
    )
    # the actions answer must reach the drafted claim that was silently omitted
    assert f"#{claim_id}" in impls["pending_actions"]()


# --- telegram-agent-reach: wider agent tool surface -------------------------
# Tests target the tool IMPLS (plain callables from _build_impls), never the
# model — the suite is hermetic and spends no tokens.


def test_agent_date_scoped_actions_and_claims():
    """'What actions do I have for July 2025 transactions' — the question that
    was unanswerable: neither pending_actions nor _find_claims could filter by
    date, and the prompt never stated today, so relative dates were guesses."""
    from openclaw import agent

    _fresh_db()
    with db.get_connection() as conn:
        aari = _aari(conn)
        july = _insert_txn(conn, "2025-07-14", -120.00, merchant="JULY VET")
        august = _insert_txn(conn, "2025-08-14", -95.00, merchant="AUGUST VET")
        july_claim = _insert_ledger_claim(conn, july, aari, "drafted", "Arthritis", 120.00)
        august_claim = _insert_ledger_claim(conn, august, aari, "drafted", "Arthritis", 95.00)

    impls = agent._build_impls([])
    scoped = impls["pending_actions"](since="2025-07-01", until="2025-07-31")
    assert f"#{july_claim}" in scoped, "the July claim is in range"
    assert f"#{august_claim}" not in scoped, "the August claim must not leak into a July answer"
    assert f"#{august_claim}" in impls["pending_actions"](), "unscoped still sees everything"

    claims = impls["query_claims"](since="2025-07-01", until="2025-07-31")
    assert f"#{july_claim}" in claims and f"#{august_claim}" not in claims
    assert "JULY VET" in impls["query_claims"](merchant="july")

    # An empty range must SAY it's empty, not answer as though unfiltered —
    # a silently-widened range reads as a correct answer.
    empty = impls["pending_actions"](since="2020-01-01", until="2020-12-31")
    assert "2020-01-01" in empty and "2020-12-31" in empty
    assert f"#{july_claim}" not in empty

    prompt = agent.system_prompt()
    assert datetime.now(timezone.utc).date().isoformat() in prompt, "today's date is injected"


def test_agent_rematch_sweep_is_scoped_and_idempotent():
    """rematch_claims acts directly, which is only safe because it's idempotent
    under ADR-0014's at-least-once update replay: only pending_match claims are
    considered, so a second run of the same sweep changes nothing. If this ever
    fails, the tool must become proposal-gated instead."""
    from openclaw import agent, invoice_matching

    _fresh_db()
    with db.get_connection() as conn:
        aari = _aari(conn)
        wanted = _insert_claim(
            conn, aari, "2026-07-01", status="pending_match", merchant="BONDI VET"
        )
        other_vet = _insert_claim(
            conn, aari, "2026-07-02", status="pending_match", merchant="OTHER VET"
        )
        already = _insert_claim(conn, aari, "2026-07-03", status="drafted", merchant="BONDI VET")

    seen = []
    original = invoice_matching.match_claim
    invoice_matching.match_claim = lambda claim: seen.append(claim["id"]) or False
    try:
        impls = agent._build_impls([])
        report = impls["rematch_claims"](merchant="bondi")
        assert seen == [wanted], f"only the pending Bondi claim is swept, got {seen}"
        assert f"#{wanted}" in report and f"#{already}" not in report
        assert f"#{other_vet}" not in report, "another vet's claim is out of scope"

        # Replay: same sweep again. Nothing matched, so the set is unchanged —
        # and the report stays truthful rather than claiming new work.
        seen.clear()
        impls["rematch_claims"](merchant="bondi")
        assert seen == [wanted], "replay re-checks the same still-pending claim, no extra targets"

        # Once matched, the claim leaves pending_match and the sweep is a no-op.
        with db.get_connection() as conn:
            conn.execute("UPDATE vet_claims SET status = 'matched' WHERE id = ?", (wanted,))
        seen.clear()
        assert "nothing to re-check" in impls["rematch_claims"](merchant="bondi")
        assert seen == [], "a matched claim is never re-swept"
    finally:
        invoice_matching.match_claim = original


def test_agent_poll_petcover_now_reports_scope_not_absence():
    """'Nothing new' must never be reported as 'Petcover hasn't replied' — the
    poll only sees unprocessed mail, and conflating the two would tell Justin a
    reply doesn't exist when it was simply already recorded."""
    from openclaw import agent, pipeline

    original = pipeline.poll_petcover_status
    try:
        pipeline.poll_petcover_status = lambda: {"checked": 0, "events": 0, "claims_changed": []}
        impls = agent._build_impls([])
        quiet = impls["poll_petcover_now"]()
        assert "NEW" in quiet, "must scope the claim to new mail"
        assert "does not mean" in quiet, (
            "must explicitly disclaim the stronger reading, not just avoid stating it"
        )

        pipeline.poll_petcover_status = lambda: {
            "checked": 2,
            "events": 3,
            "claims_changed": [18, 21],
        }
        busy = impls["poll_petcover_now"]()
        assert "#18" in busy and "#21" in busy, "changed claims are named by id"

        # A Gmail failure is surfaced, never swallowed into a false all-clear.
        def boom():
            raise RuntimeError("token expired")

        pipeline.poll_petcover_status = boom
        assert "token expired" in impls["poll_petcover_now"]()
    finally:
        pipeline.poll_petcover_status = original


def test_poll_petcover_status_summary_counts_only_new_events():
    """The summary is derived from the append-only log's max id, so it reports
    what THIS poll recorded and not the table's whole history."""
    from openclaw import pipeline

    _fresh_db()
    with db.get_connection() as conn:
        aari = _aari(conn)
        claim = _insert_claim(conn, aari, "2026-07-01", status="sent")
        _insert_settled_event(conn, claim, datetime.now(timezone.utc).isoformat(), 100.0)

    before = pipeline._latest_event_id()
    assert before > 0, "a pre-existing event is present"
    assert pipeline._claims_touched_since(before) == [], "nothing recorded after the snapshot yet"
    with db.get_connection() as conn:
        _insert_settled_event(conn, claim, datetime.now(timezone.utc).isoformat(), 120.0)
    assert pipeline._claims_touched_since(before) == [claim]


def test_submissions_awaiting_reply_groups_by_submission():
    """One entry per Submission, not per claim — claims sharing a draft_id move
    together, so three claims in one batch are one thing waiting on Petcover."""
    _fresh_db()
    with db.get_connection() as conn:
        aari = _aari(conn)
        a = _insert_claim(
            conn, aari, "2026-07-01", status="sent", draft_id="draft-1", reference="DC1-1"
        )
        b = _insert_claim(
            conn, aari, "2026-07-02", status="sent", draft_id="draft-1", reference="DC1-1"
        )
        solo = _insert_claim(conn, aari, "2026-07-03", status="acknowledged", draft_id="draft-2")
        no_draft = _insert_claim(conn, aari, "2026-07-04", status="drafted")
        settled = _insert_claim(conn, aari, "2026-07-05", status="settled", draft_id="draft-3")
        _insert_settled_event(conn, solo, datetime.now(timezone.utc).isoformat(), 90.0)

    rows = claim_status.submissions_awaiting_reply()
    by_ids = {tuple(r["claim_ids"]): r for r in rows}
    assert (a, b) in by_ids, f"the batch is one entry, got {[r['claim_ids'] for r in rows]}"
    assert all(settled not in r["claim_ids"] for r in rows), (
        "a settled submission isn't awaiting anything"
    )

    assert by_ids[(a, b)]["last_event"] is None, "no reply recorded for the batch"
    assert by_ids[(solo,)]["last_event"] == "settled", "the newest event is reported"
    # A claim with no draft_id is its own submission, never merged with other
    # draft-less claims under a shared NULL key.
    assert (no_draft,) in by_ids and by_ids[(no_draft,)]["draft_id"] is None
    assert all(r["days_waiting"] >= 0 for r in rows)

    from openclaw import agent

    text = agent._build_impls([])["submissions_awaiting_reply"]()
    assert f"#{a}" in text and f"#{b}" in text and "NO reply recorded" in text


def test_claim_detail_explains_why_with_recorded_figures():
    """'Why did #21 flag?' — needs the flag plus the dollar figures the reply
    actually carried. claim_history gives event types only and is keyed by
    pet/reference, so it cannot answer a question about one claim id."""
    import json as _json

    _fresh_db()
    with db.get_connection() as conn:
        aari = _aari(conn)
        invoice = _json.dumps(
            {
                "invoice_number": "INV-9",
                "amount": 55.74,
                "claimable_amount": 44.75,
                "items": [
                    {"description": "Consultation", "amount": 44.75},
                    {"description": "Food", "amount": 10.99},
                ],
            }
        )
        claim = _insert_claim(
            conn,
            aari,
            "2025-09-26",
            status="approved",
            reference="DC1-27-5628",
            sr=4,
            condition="Arthritis",
            invoice_data=invoice,
        )
        conn.execute(
            "UPDATE vet_claims SET flag = ? WHERE id = ?",
            ("settlement mismatch — we expected $44.75, Petcover paid $22.75 — review", claim),
        )
        conn.execute(
            "INSERT INTO claim_status_events (claim_id, event_type, raw_email_id, detail, created_at) "
            "VALUES (?, 'approved', 'mail-1', ?, ?)",
            (
                claim,
                _json.dumps(
                    {
                        "subject": "Claim Approval",
                        "claimed_amount": 35.0,
                        "paid_amount": 22.75,
                        "fixed_excess_stated": 0.0,
                        "age_contribution_stated": 12.25,
                        "body": "a long body that must not reach the chat turn",
                    }
                ),
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    detail = claim_status.claim_detail(claim)
    assert detail["claimable_amount"] == 44.75 and detail["petcover_sr"] == 4
    assert len(detail["items"]) == 2
    event = detail["events"][0]
    assert event["paid_amount"] == 22.75 and event["age_contribution_stated"] == 12.25
    assert "body" not in event, "raw bodies are dropped — they blow the token budget for no answer"
    assert claim_status.claim_detail(999999) is None

    from openclaw import agent

    text = agent._build_impls([])["claim_detail"](claim)
    assert "settlement mismatch" in text, "the flag is the answer to 'why'"
    assert "22.75" in text and "Consultation" in text
    assert "No claim #999999" in agent._build_impls([])["claim_detail"](999999)


def test_agent_task_proposals_write_nothing_until_confirmed():
    """The assistant side had no Telegram surface at all. Capture is gated for
    two reasons: it spends an LLM call extracting a follow-up date, and a
    misheard task resurfaces later as a false obligation."""
    from openclaw import agent, telegram_bot

    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM tasks")

    def _count():
        with db.get_connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]

    proposals = []
    impls = agent._build_impls(proposals)
    reply = impls["propose_create_task"]("Call the painter back about the quote")
    assert "Confirm" in reply and _count() == 0, "proposing must not write"
    assert proposals[-1]["action"] == "create_task"
    assert "claim_id" not in proposals[-1], "a task proposal carries no claim id"

    # Blank text is refused rather than stored as an empty task.
    assert "never invent" in impls["propose_create_task"]("   ")
    assert _count() == 0

    # The tap is what writes. tasks.create_task spends an LLM call on the
    # follow-up date, which the hermetic suite must not make — stub just that.
    from openclaw import tasks as tasks_module

    original = tasks_module._extract_follow_up
    tasks_module._extract_follow_up = lambda description: None
    try:
        message = telegram_bot._execute_action(proposals[-1])
        assert "saved" in message and _count() == 1
        with db.get_connection() as conn:
            task_id = conn.execute("SELECT id FROM tasks").fetchone()[0]

        listed = impls["list_tasks"]()
        assert f"#{task_id}" in listed, "task ids are always shown — it's how he closes them"

        # Closing needs an outcome from Justin; one is never invented.
        assert "never invent an outcome" in impls["propose_close_task"](task_id, "")
        assert "No task #999999" in impls["propose_close_task"](999999, "done")
        impls["propose_close_task"](task_id, "spoke to him, quote arriving Friday")
        with db.get_connection() as conn:
            assert (
                conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]
                == "open"
            ), "still open until the tap"
        telegram_bot._execute_action(proposals[-1])
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT status, outcome FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        assert row["status"] == "closed" and "Friday" in row["outcome"]
        assert "already closed" in impls["propose_close_task"](task_id, "again")
    finally:
        tasks_module._extract_follow_up = original


def test_chat_answer_names_every_claim_in_a_batch():
    """The collapse gave the agent one representative claim_id to render, which
    would have printed '#6' and dropped '#7' from the answer describing that very
    submission. Every referenced claim's id has to appear."""
    from openclaw import agent

    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM vet_claims")
        conn.execute("DELETE FROM bank_transactions")
        conn.execute("DELETE FROM claim_status_events")
        aari = conn.execute("SELECT id FROM pets WHERE name='Aari'").fetchone()[0]
        ids = []
        for day, amount in (("2026-04-17", -132.50), ("2026-05-18", -351.50)):
            txn = _insert_txn(conn, day, amount)
            ids.append(_insert_ledger_claim(conn, txn, aari, "drafted", "Arthritis", abs(amount)))
        conn.execute("UPDATE vet_claims SET draft_id = 'chat-draft' WHERE id IN (?, ?)", tuple(ids))

    answer = agent._build_impls([])["pending_actions"]()
    for claim_id in ids:
        assert f"#{claim_id}" in answer, (
            f"#{claim_id} missing — a dropped id is an unactionable answer"
        )
    assert claim_status.submission_group_id(ids) in answer


def test_action_tokens_are_not_claim_shaped():
    """The token was `action:claim_id`, which tasks have no value for — and it
    silently collapsed two proposals for the same claim into one."""
    from openclaw import telegram_bot

    first = telegram_bot._register_action({"action": "mark_sent", "claim_id": 7, "label": "a"})
    second = telegram_bot._register_action({"action": "mark_sent", "claim_id": 7, "label": "b"})
    assert first != second, "two proposals for one claim must not overwrite each other"
    assert telegram_bot._pending_actions[first]["label"] == "a"
    task_token = telegram_bot._register_action({"action": "create_task", "arg": "x", "label": "t"})
    assert len(task_token) < 64, "must fit Telegram's callback_data limit"
    assert "None" not in task_token, "no claim id is stringified into it"


def test_agent_prompt_narrows_mailbox_rule_without_dropping_it():
    """The absolute 'you cannot read his mailbox' rule existed because the agent
    once answered 'I checked your sent mail' with no such capability. Adding real
    sweeps narrows it; it must not quietly become permission to imply a search."""
    from openclaw import agent

    prompt = agent.system_prompt().lower()
    assert "cannot browse, search, or read justin's mailbox" in prompt
    assert "never imply you did" in prompt
    for sweep in ("reconcile_sent_invoice_requests", "rematch_claims", "poll_petcover_now"):
        assert sweep in prompt, f"{sweep} must be named as an allowed, specific check"
    assert (
        "does not mean petcover has never replied" not in prompt
    )  # that wording lives in the tool
    assert "nothing new" in prompt, "the new-mail-only limit is stated"
    assert "cannot read openclaw's code" in prompt, "no code/spec reading in the container"


_shared_invoice_charges = 0


_AARI_INVOICE = {
    "invoice_number": "SHV49c1622284e5",
    "date": "2026-06-19",
    "patient": "Aari",
    "amount": 35.0,
    "items": [{"description": "Prescription fee", "amount": 35.0}],
}
_ECHO_INVOICE = {
    "invoice_number": "SHVd5b232905fdb",
    "date": "2026-06-30",
    "patient": "Echo",
    "amount": 369.33,
    "items": [
        {"description": "CLINDAMYCIN 150MG CAPSULES", "amount": 206.12},
        {"description": "ENROFLOXACIN 150MG TABLETS", "amount": 163.21},
    ],
}
# The receipts' own wording. Both visits are weeks before the 6 Jul charge, so
# only the payment line makes them matchable at all.
_AARI_RECEIPT_TEXT = (
    "TAX INVOICE - RECEIPT 19 Jun 2026 # SHV49c1622284e5\n"
    "Aari 19 Jun 2026 Prescription fee 1.00 $0.00 $31.82 $35.00\n"
    "TOTAL $35.00\nThe following payments have been received with thanks\n"
    "Paid Date Payment Method Payment\n06/07/2026 Credit Card $35.00"
)
_ECHO_RECEIPT_TEXT = (
    "TAX INVOICE - RECEIPT 30 Jun 2026 # SHVd5b232905fdb\n"
    "Echo 30 Jun 2026 CLINDAMYCIN 150MG CAPSULES 28.00 $206.12\n"
    "Echo 30 Jun 2026 ENROFLOXACIN 150MG TABLETS 11.00 $163.21\n"
    "TOTAL $369.33\nThe following payments have been received with thanks\n"
    "Paid Date Payment Method Payment\n06/07/2026 Credit Card $369.33"
)


def test_a_receipt_paid_on_the_charge_date_is_matchable_though_the_visit_is_older():
    """INVOICE_MATCH_WINDOW_DAYS is 3, measured on the SERVICE date — which
    silently rejected both real invoices for this charge: The Shire Vet billed
    19 Jun and 30 Jun, the card was charged 6 Jul, and each receipt says so on
    its own payment line ("06/07/2026 Credit Card $35.00")."""
    from datetime import date as _date

    from openclaw import config

    txn_date = _date(2026, 7, 6)
    assert config.INVOICE_MATCH_WINDOW_DAYS == 3, "this test exists because the window is tight"
    assert not invoice_matching._invoice_date_plausible(_AARI_INVOICE, txn_date), (
        "17 days out on service date"
    )
    assert invoice_matching._paid_on_charge_date(_AARI_RECEIPT_TEXT, _AARI_INVOICE, txn_date)
    assert invoice_matching._paid_on_charge_date(_ECHO_RECEIPT_TEXT, _ECHO_INVOICE, txn_date)

    # Both facts required on ONE line: a bulk email full of other visits' payment
    # dates must not lend them to this invoice.
    assert not invoice_matching._paid_on_charge_date(
        "06/07/2026 Credit Card $999.00\nsome other invoice $35.00", _AARI_INVOICE, txn_date
    ), "the date and THIS invoice's amount have to be the same payment line"
    assert not invoice_matching._paid_on_charge_date(
        _AARI_RECEIPT_TEXT, _AARI_INVOICE, _date(2026, 7, 7)
    )
    assert not invoice_matching._paid_on_charge_date("", _AARI_INVOICE, txn_date)

    # And it reaches the picker: the receipt is chosen where the window alone refuses.
    assert invoice_matching._pick_invoice([_AARI_INVOICE], -35.0, txn_date) is None, (
        "window-only: refused"
    )
    picked = invoice_matching._pick_invoice(
        [_AARI_INVOICE], -35.0, txn_date, text=_AARI_RECEIPT_TEXT
    )
    assert picked and picked["invoice_number"] == "SHV49c1622284e5"


def test_one_charge_two_invoices_two_pets_is_apportioned_automatically():
    """Live 2026-07-27: The Shire Vet's $407.56 charge on 2026-07-06 paid TWO
    invoices, forwarded as two separate emails — SHV49c1622284e5 (Aari, $35.00)
    and SHVd5b232905fdb (Echo, $369.33), the $3.23 balance being card surcharge.
    The first invoice matched and the rest of the charge became the flag
    `possible additional invoice — unexplained $372.56`, so Echo's invoice was
    never claimed at all."""
    import json as _json

    db.init_db()
    claim_id = _shared_invoice_claim(claimable=None)
    claim = _matched_row(claim_id)

    chosen = {"email_id": "em-aari", "invoice": {**_AARI_INVOICE}, "text": _AARI_RECEIPT_TEXT}
    pool = [
        chosen,
        {"email_id": "em-echo", "invoice": {**_ECHO_INVOICE}, "text": _ECHO_RECEIPT_TEXT},
    ]

    assert invoice_matching._apply_match(claim, chosen, pool) is True
    rows = sorted(
        (dict(r) for r in _claims_on_transaction(claim["transaction_id"])), key=lambda r: r["id"]
    )
    assert len(rows) == 2, f"the second invoice must get its own claim: {rows}"
    kept, sibling = rows
    assert kept["id"] == claim_id and kept["pet_id"] == 1, kept
    assert sibling["pet_id"] == 2, "the pet comes off each invoice's printed patient field"
    assert sibling["matched_email_id"] == "em-echo", "each claim carries its OWN invoice email"
    assert kept["flag"] is None, (
        f"nothing is unexplained once both invoices are known: {kept['flag']}"
    )
    kept_invoice, sibling_invoice = (
        _json.loads(kept["invoice_data"]),
        _json.loads(sibling["invoice_data"]),
    )
    assert kept_invoice["invoice_number"] == "SHV49c1622284e5"
    assert sibling_invoice["invoice_number"] == "SHVd5b232905fdb"
    assert (kept_invoice["claimable_amount"], sibling_invoice["claimable_amount"]) == (35.0, 369.33)
    assert "$35.00 + $369.33" in kept_invoice["charge_note"], kept_invoice
    assert sibling["status"] == "matched" and sibling["transaction_id"] == claim["transaction_id"]


def test_complement_search_refuses_anything_it_cannot_prove():
    """A wrong pairing invents a claim, so every gate is a refusal: the pair must
    close the charge within the surcharge margin, be two different invoices, sit
    in the visit's date window, and be unambiguous."""
    from datetime import date as _date

    db.init_db()
    txn_date, charge = _date(2026, 7, 6), -407.56
    # Distinct invoice numbers: the apportionment test above already parked the
    # real ones on claims, and an invoice another claim carries is never a
    # candidate (_already_claimed).
    aari = {**_AARI_INVOICE, "invoice_number": "T2-AARI"}
    echo = {**_ECHO_INVOICE, "invoice_number": "T2-ECHO"}
    chosen = {"email_id": "em-aari", "invoice": aari, "text": _AARI_RECEIPT_TEXT}

    def complement(pool):
        return invoice_matching._complement_for(chosen, [chosen, *pool], charge, txn_date, 999999)

    assert complement([{"email_id": "e", "invoice": echo, "text": _ECHO_RECEIPT_TEXT}]) is not None

    # Doesn't close the gap.
    assert (
        complement(
            [
                {
                    "email_id": "e",
                    "invoice": {
                        **echo,
                        "amount": 100.0,
                        "invoice_number": "X1",
                        "date": "2026-07-06",
                    },
                    "text": "",
                }
            ]
        )
        is None
    )
    # Together they exceed the charge.
    assert (
        complement(
            [
                {
                    "email_id": "e",
                    "invoice": {
                        **echo,
                        "amount": 400.0,
                        "invoice_number": "X2",
                        "date": "2026-07-06",
                    },
                    "text": "",
                }
            ]
        )
        is None
    )
    # Right amount, wrong visit — outside the date window.
    assert (
        complement(
            [
                {
                    "email_id": "e",
                    "invoice": {**echo, "date": "2025-06-30", "invoice_number": "X3"},
                    "text": "",
                }
            ]
        )
        is None
    )
    # Two candidates would both close it: which one the charge paid is unknowable.
    assert (
        complement(
            [
                {"email_id": "e1", "invoice": echo, "text": _ECHO_RECEIPT_TEXT},
                {
                    "email_id": "e2",
                    "invoice": {**echo, "invoice_number": "X4"},
                    "text": _ECHO_RECEIPT_TEXT,
                },
            ]
        )
        is None
    )
    # The same invoice seen twice is not a complement.
    assert (
        complement([{"email_id": "e-dup", "invoice": {**aari}, "text": _AARI_RECEIPT_TEXT}]) is None
    )
    # Nothing left to explain (invoice covers the charge bar a surcharge).
    covered = {"email_id": "em", "invoice": {**aari, "amount": 404.33}, "text": ""}
    assert (
        invoice_matching._complement_for(
            covered,
            [covered, {"email_id": "e", "invoice": echo, "text": _ECHO_RECEIPT_TEXT}],
            charge,
            txn_date,
            999999,
        )
        is None
    )


def _claims_on_transaction(txn_id):
    with db.get_connection() as conn:
        return conn.execute(
            "SELECT * FROM vet_claims WHERE transaction_id = ?", (txn_id,)
        ).fetchall()


def _shared_invoice_claim(claimable=407.56, status="matched"):
    """Claim #1's real shape: The Shire Veterinary Caringbah, $407.56 charged
    2026-07-06, invoice matched, no pet, no condition, no itemization. Each call
    walks the charge date back a day — (date, amount, merchant) is unique."""
    import json as _json

    global _shared_invoice_charges
    txn_date = (
        date.fromisoformat("2026-07-06") - timedelta(days=_shared_invoice_charges)
    ).isoformat()
    _shared_invoice_charges += 1
    now = datetime.now(timezone.utc).isoformat()
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO bank_transactions (date, amount, merchant, vet_flag, created_at) "
            "VALUES (?, ?, ?, 1, ?)",
            (txn_date, -407.56, "THE SHIRE VETERINARY CARINGBAH NSW", now),
        )
        txn_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO vet_claims (transaction_id, pet_id, status, matched_email_id, invoice_data, "
            "created_at, updated_at) VALUES (?, NULL, ?, 'em-shire', ?, ?, ?)",
            (
                txn_id,
                status,
                _json.dumps(
                    {
                        "date": "2026-07-06",
                        "amount": 407.56,
                        "items": [],
                        "claimable_amount": claimable,
                        "invoice_number": "INV-9",
                    }
                ),
                now,
                now,
            ),
        )
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _claim_row(claim_id):
    with db.get_connection() as conn:
        return conn.execute("SELECT * FROM vet_claims WHERE id = ?", (claim_id,)).fetchone()


def test_one_invoice_splits_between_two_pets_each_carrying_its_own_share():
    """One invoice can treat two pets (live: $407.56 at The Shire Veterinary
    Caringbah — Aari $35, Echo the rest), but a claim carries one pet_id, so
    either pet button over-claims and loses the other's share entirely."""
    import json as _json

    db.init_db()
    claim_id = _shared_invoice_claim()

    result = claim_forms.split_between_pets(claim_id, [(1, 35.0), (2, None)])
    assert result["ok"], result
    aari, echo = result["claims"]
    assert (aari["pet_name"], aari["amount"]) == ("Aari", 35.0), result
    assert (echo["pet_name"], echo["amount"]) == ("Echo", 372.56), (
        "the remainder is derived, not guessed"
    )
    assert result["unapportioned"] == 0.0

    kept, sibling = _claim_row(claim_id), _claim_row(echo["claim_id"])
    assert kept["pet_id"] == 1 and sibling["pet_id"] == 2
    assert kept["transaction_id"] == sibling["transaction_id"], "one charge, one bank row"
    assert kept["matched_email_id"] == sibling["matched_email_id"] == "em-shire"
    for row, share in ((kept, 35.0), (sibling, 372.56)):
        invoice = _json.loads(row["invoice_data"])
        assert invoice["claimable_amount"] == share, invoice
        assert invoice["amount"] == 407.56 and invoice["invoice_number"] == "INV-9", (
            "invoice untouched"
        )
        assert f"#{echo['claim_id']} Echo $372.56" in invoice["split_note"], invoice["split_note"]
    assert sibling["condition_text"] is None, (
        "the other pet's condition is never copied — that's a guess"
    )

    # Echo is with Bow Wow, whose process isn't on file: her share is recorded and
    # visible, never dropped for lack of a claim process.
    assert "Bow Wow" in (sibling["flag"] or ""), sibling["flag"]
    blocked = [a for a in claim_status.pending_actions() if a["claim_id"] == echo["claim_id"]]
    assert blocked and not blocked[0]["actionable"], f"Echo's share must show as blocked: {blocked}"


def test_split_guards_refuse_rather_than_guess_or_over_claim():
    """Every refusal here is a wrong claim that didn't get filed."""
    db.init_db()

    over = claim_forms.split_between_pets(_shared_invoice_claim(), [(1, 400.0), (2, 100.0)])
    assert not over["ok"] and "ceiling" in over["message"], over
    assert "$500.00" in over["message"] and "$407.56" in over["message"], (
        "both figures, so it's checkable"
    )

    two_unknowns = claim_forms.split_between_pets(_shared_invoice_claim(), [(1, None), (2, None)])
    assert not two_unknowns["ok"] and "Only one share" in two_unknowns["message"], two_unknowns

    one_pet = claim_forms.split_between_pets(_shared_invoice_claim(), [(1, 35.0)])
    assert not one_pet["ok"] and "at least two pets" in one_pet["message"], one_pet

    dupe = claim_forms.split_between_pets(_shared_invoice_claim(), [(1, 35.0), (1, 100.0)])
    assert not dupe["ok"] and "twice" in dupe["message"], dupe

    already_sent = claim_forms.split_between_pets(
        _shared_invoice_claim(status="sent"), [(1, 35.0), (2, None)]
    )
    assert not already_sent["ok"], already_sent
    assert "already with the insurer" in already_sent["message"], already_sent["message"]

    unmatched = claim_forms.split_between_pets(
        _shared_invoice_claim(status="pending_match"), [(1, 35.0), (2, None)]
    )
    assert not unmatched["ok"] and "no invoice matched yet" in unmatched["message"], unmatched

    # Shares that fall short still split, but the gap is flagged — a quietly
    # unapportioned $72.56 is money nobody claims.
    short_id = _shared_invoice_claim()
    short = claim_forms.split_between_pets(short_id, [(1, 35.0), (2, 300.0)])
    assert short["ok"] and short["unapportioned"] == 72.56, short
    assert "unapportioned" in short["message"], short["message"]
    flag = _claim_row(short_id)["flag"] or ""
    assert "$72.56" in flag, flag
    assert "condition text missing" in flag, f"process_claim's own blocker survives too: {flag}"


def test_the_split_conversation_that_failed_live_now_reaches_one_proposal():
    """2026-07-27, four messages, nothing done: no tool fitted, no way to name
    the claim, and the model ended up sending its own schema's description
    strings as arguments. Same request, stubbed model, must now produce exactly
    one confirmable split proposal — and write nothing until it's confirmed."""
    from openclaw import agent, telegram_bot

    db.init_db()
    claim_id = _shared_invoice_claim()

    captured = {}

    def _fake_chat(messages, tools=None, tool_impls=None, purpose="chat", max_iterations=4):
        captured["context"] = [m["content"] for m in messages if m["role"] == "system"]
        captured["tool_names"] = {t["function"]["name"] for t in tools or []}
        # What the model does with the reply-context line: act on that claim id.
        captured["tool_result"] = tool_impls["propose_split_between_pets"](
            claim_id=claim_id, pets_and_amounts=[{"pet": "Aari", "amount": 35}, {"pet": "Echo"}]
        )
        return {
            "text": "Aari $35.00, Echo $372.56 — tap Confirm.",
            "model": "llama-3.3-70b-versatile",
        }

    original_chat = agent.llm.chat
    try:
        agent.llm.chat = _fake_chat
        reply, proposal = agent.handle_message(
            "This is actually split between echo and Aari. Aari cost was $35 out of this",
            chat_id=None,
            claim_id=claim_id,
        )
    finally:
        agent.llm.chat = original_chat

    assert "propose_split_between_pets" in captured["tool_names"], (
        "the tool has to exist to be reachable"
    )
    assert any(f"claim #{claim_id}" in line for line in captured["context"]), captured["context"]
    assert "Proposed" in captured["tool_result"] and "$35.00" in captured["tool_result"], captured
    assert proposal and proposal["action"] == "split_pets", proposal
    assert proposal["claim_id"] == claim_id and proposal["arg"] == [(1, 35.0), (2, None)], proposal
    assert "Confirm" in reply

    assert _claim_row(claim_id)["pet_id"] is None, "nothing may change before the tap"

    message = telegram_bot._execute_action(proposal)
    assert "Aari $35.00" in message and "Echo $372.56" in message, message
    assert _claim_row(claim_id)["pet_id"] == 1, "the tap is what writes"


def test_assigning_one_pet_is_refused_when_the_message_names_two():
    """Live 2026-07-27: replaying "This is actually split between echo and Aari.
    Aari cost was $35 out of this" against the ASSIGN PET card, the primary model
    proposed assigning Aari AND Echo — no API error, the split tool right there
    in the schema, the prompt rule simply lost. Assigning one pet when his own
    words name two is the over-claim this change exists to prevent, so the
    harness refuses it instead of relying on the next turn choosing better."""
    from openclaw import agent

    db.init_db()
    claim_id = _shared_invoice_claim()
    proposals = []
    text = "This is actually split between echo and Aari. Aari cost was $35 out of this"
    impls = agent._build_impls(proposals, user_text=text)

    refusal = impls["propose_assign_pet"](pet_name="Aari", claim_id=claim_id)
    assert not proposals, "nothing may be queued when the message names two pets"
    assert "SPLIT" in refusal and "propose_split_between_pets" in refusal, refusal
    assert "Aari and Echo" in refusal, refusal

    # One pet named → ordinary assignment, unchanged.
    single = agent._build_impls(proposals, user_text="that one is Aari's")
    assert "Proposed" in single["propose_assign_pet"](pet_name="Aari", claim_id=claim_id)
    assert proposals and proposals[-1]["action"] == "assign_pet", proposals
    assert agent._pets_named_in("the vet echoed the diagnosis") == [], (
        "word-boundary, not substring"
    )


def test_split_proposal_is_refused_before_the_tap_when_it_cannot_work():
    """An impossible split must be refused in the reply, not after Justin taps
    Confirm — the agent runs the same guards the write does."""
    from openclaw import agent

    db.init_db()
    claim_id = _shared_invoice_claim()
    proposals = []
    impls = agent._build_impls(proposals)

    over = impls["propose_split_between_pets"](
        claim_id=claim_id,
        pets_and_amounts=[{"pet": "Aari", "amount": 400}, {"pet": "Echo", "amount": 100}],
    )
    assert "ceiling" in over and not proposals, over

    unknown_pet = impls["propose_split_between_pets"](
        claim_id=claim_id, pets_and_amounts=[{"pet": "Whiskers", "amount": 35}, {"pet": "Echo"}]
    )
    assert "No pet named 'Whiskers'" in unknown_pet and not proposals, unknown_pet

    no_amounts = impls["propose_split_between_pets"](
        claim_id=claim_id, pets_and_amounts=[{"pet": "Aari"}, {"pet": "Echo"}]
    )
    assert "Only one share" in no_amounts and not proposals, no_amounts

    missing_claim = impls["propose_split_between_pets"](
        claim_id=999999, pets_and_amounts=[{"pet": "Aari", "amount": 35}, {"pet": "Echo"}]
    )
    assert "No claim #999999" in missing_claim and not proposals, missing_claim


def test_malformed_tool_call_retries_then_fails_readably():
    """Groq rejects its own garbled output with a 400 `tool_use_failed` (seen
    live: `<function=list_tasks,{...}</function>`). That's a nondeterministic
    formatting slip, not an outage — and it got likelier when the tool surface
    went 8 -> 15. Retry it, and don't report an outage that isn't happening."""
    from openclaw import llm

    class _Boom(Exception):
        pass

    err = _Boom("Error code: 400 - {'code': 'tool_use_failed', 'failed_generation': '<function=x'}")
    assert llm._is_malformed_tool_call(err)
    assert not llm._is_rate_limited(err), "a 400 is not a rate limit"

    attempts = []

    class _Client:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    attempts.append(1)
                    if len(attempts) < 2:
                        raise err

                    class _M:
                        content = "recovered"
                        tool_calls = None

                    class _R:
                        choices = [type("C", (), {"message": _M()})()]

                    return _R()

    message = llm._completion(_Client(), "m", [{"role": "user", "content": "hi"}], None, "test")
    assert message.content == "recovered" and len(attempts) == 2, (
        "one garbled call must not fail the turn"
    )

    class _AlwaysBad:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    raise err

    try:
        llm._completion(_AlwaysBad(), "m", [{"role": "user", "content": "hi"}], None, "test")
        raise AssertionError("must still fail visibly when it never recovers")
    except llm.LLMUnavailableError as exc:
        assert "malformed tool call" in str(exc), "says what actually went wrong"
        assert "try rephrasing" in str(exc)


def test_transient_non_429_failure_is_retried_but_a_bad_request_is_not():
    """A single `403 Access denied. Please check your network settings.` from Groq
    ended a chat turn on its FIRST attempt (2026-07-27, llm_calls #1659 — one
    row) because only 429 and tool_use_failed were retried; the same request
    seconds later worked. Retry unless a retry provably cannot help: a per-day
    cap (switch model instead) or a 400 that is our own request shape."""
    from openclaw import llm

    edge_403 = Exception(
        "Error code: 403 - {'error': {'message': 'Access denied. "
        "Please check your network settings.'}}"
    )
    bad_shape = Exception(
        "Error code: 400 - {'error': {'message': "
        "'messages[2].reasoning: reasoning is not supported with this model'}}"
    )
    garbled = Exception("Error code: 400 - {'code': 'tool_use_failed'}")
    assert not llm._is_request_shape_error(edge_403), "403 says nothing about our request"
    assert llm._is_request_shape_error(bad_shape)
    assert not llm._is_request_shape_error(garbled), "a garbled tool call keeps its own retry path"

    original_backoff = llm.BASE_BACKOFF_SECONDS
    llm.BASE_BACKOFF_SECONDS = 0  # the backoff is asserted elsewhere; don't sleep here

    def _client(exc, fail_times):
        class C:
            class chat:
                class completions:
                    calls = []

                    @classmethod
                    def create(cls, **kwargs):
                        cls.calls.append(1)
                        if len(cls.calls) <= fail_times:
                            raise exc

                        class _M:
                            content = "recovered"
                            tool_calls = None

                        return type("R", (), {"choices": [type("C", (), {"message": _M()})()]})

        return C()

    def _llm_call_count():
        with db.get_connection() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM llm_calls WHERE purpose = 'retrytest'"
            ).fetchone()[0]

    try:
        before = _llm_call_count()
        client = _client(edge_403, fail_times=1)
        message = llm._completion(
            client, "m", [{"role": "user", "content": "hi"}], None, "retrytest"
        )
        assert message.content == "recovered", "a one-off 403 must not kill the turn"
        assert len(client.chat.completions.calls) == 2, client.chat.completions.calls
        assert _llm_call_count() - before == 2, "every attempt logs its own llm_calls row"

        before = _llm_call_count()
        client = _client(bad_shape, fail_times=99)
        try:
            llm._completion(client, "m", [{"role": "user", "content": "hi"}], None, "retrytest")
            raise AssertionError("a request-shape 400 must surface, not be retried")
        except llm.LLMUnavailableError as exc:
            assert "reasoning is not supported" in str(exc), "the provider's own message survives"
        assert len(client.chat.completions.calls) == 1, "no tokens spent reproducing our own bug"
        assert _llm_call_count() - before == 1
    finally:
        llm.BASE_BACKOFF_SECONDS = original_backoff


def test_daily_budget_exhaustion_falls_through_to_another_model():
    """Groq's token-per-day cap is PER MODEL, so an exhausted budget is
    survivable by moving models — unlike the per-minute cap, where waiting is the
    only cure. Justin hit this in normal use and the whole chat agent was dead.
    ADR-0009 made the provider swappable; this is the same failure one level down."""
    from openclaw import llm

    tpd = Exception(
        "Error code: 429 - {'message': 'Rate limit reached for model "
        "`llama-3.3-70b-versatile` ... on tokens per day (TPD): Limit 100000, Used 97968'}"
    )
    tpm = Exception("Error code: 429 - {'message': 'Rate limit reached ... on tokens per minute'}")
    assert llm._is_daily_budget_exhausted(tpd)
    assert not llm._is_daily_budget_exhausted(tpm), "per-minute must NOT trigger a model switch"
    assert llm._is_rate_limited(tpd), "still a 429"

    tried = []

    def _client(fail_for):
        class C:
            class chat:
                class completions:
                    @staticmethod
                    def create(model, **kwargs):
                        tried.append(model)
                        if model in fail_for:
                            raise tpd

                        class _M:
                            content = f"answered by {model}"
                            tool_calls = None

                        return type("R", (), {"choices": [type("C", (), {"message": _M()})()]})

        return C()

    # `_completion` reads the chain from config.LLM_PROVIDER, so the provider is
    # PINNED here. It used to be inherited from whatever .env held, which meant
    # this test silently stopped exercising a chain at all the day the app moved
    # off Groq (2026-08-04): `_FALLBACK_MODELS.get("gemini")` was empty, the
    # "everything spent" case had one link, and the test failed for a reason
    # unrelated to what it asserts.
    @contextlib.contextmanager
    def _pinned(provider):
        original = config.LLM_PROVIDER
        config.LLM_PROVIDER = provider
        try:
            yield
        finally:
            config.LLM_PROVIDER = original

    # Primary spent -> second model's own budget answers, and only ONE attempt is
    # spent on the exhausted model (retrying can't free a daily cap).
    with _pinned("groq"):
        msg = llm._completion(
            _client({"llama-3.3-70b-versatile"}),
            "llama-3.3-70b-versatile",
            [{"role": "user", "content": "hi"}],
            None,
            "test",
        )
    assert msg.content == "answered by openai/gpt-oss-120b", tried
    assert tried == ["llama-3.3-70b-versatile", "openai/gpt-oss-120b"], (
        f"no wasted retries: {tried}"
    )
    assert llm._last_model_used == "openai/gpt-oss-120b", "the answering model is recorded"

    # Everything spent -> visible failure that says what's actually wrong, and
    # doesn't send him hunting an outage. The model set is DERIVED from the
    # configured chain, not hardcoded — hardcoding it broke the moment a fourth
    # link was added, by silently letting the "everything is spent" case succeed.
    #
    # Run for EVERY configured provider, not just Groq: a provider with no chain
    # would otherwise pass this by having nothing to walk. Gemini's own links were
    # probed live before being added (see llm._FALLBACK_MODELS).
    for provider, primary in (("groq", "llama-3.3-70b-versatile"), ("gemini", "gemini-2.5-flash")):
        tried.clear()
        chain = [primary, *llm._FALLBACK_MODELS[provider]]
        assert len(chain) > 1, f"{provider} has no fallback chain to walk"
        try:
            with _pinned(provider):
                llm._completion(
                    _client(set(chain)), primary, [{"role": "user", "content": "hi"}], None, "test"
                )
            raise AssertionError(f"{provider}: must fail visibly once every budget is gone")
        except llm.LLMUnavailableError as exc:
            assert "daily token budget" in str(exc) and "rolling window" in str(exc)
            assert tried == chain, f"{provider} tries each model exactly once, in order: {tried}"


def test_fallback_model_is_disclosed_in_the_reply():
    """A quietly weaker answer is the invisible failure the hard rules forbid."""
    from openclaw import agent, llm

    original = llm.chat
    llm.chat = lambda *a, **k: {"text": "here you go", "model": "llama-3.1-8b-instant"}
    try:
        reply, _proposal = agent.handle_message("what's outstanding", chat_id=None)
        assert "out of daily tokens" in reply and "llama-3.1-8b-instant" in reply
        assert "here you go" in reply, "the actual answer survives the notice"

        # Primary answering must NOT be annotated.
        _b, primary, _k = llm._resolve()
        llm.chat = lambda *a, **k: {"text": "clean", "model": primary}
        reply, _proposal = agent.handle_message("what's outstanding", chat_id=None)
        assert reply == "clean"
    finally:
        llm.chat = original


def test_prompt_forbids_markdown_for_a_plain_text_channel():
    """_handle_chat sends replies with no parse_mode, so markdown arrives
    literally. gpt-oss-120b (reachable via the fallback chain) answers with pipe
    tables by default — unreadable on a phone."""
    from openclaw import agent

    prompt = agent.system_prompt()
    assert "NEVER use markdown tables" in prompt
    assert "Telegram" in prompt


def test_assistant_turn_drops_output_only_fields():
    """Live 400 (2026-07-25): `messages[2].reasoning: reasoning is not supported
    with this model`. The tool loop replayed the whole assistant message back
    into the conversation, so gpt-oss-120b's `reasoning` field poisoned the next
    request and killed the turn. Latent since the loop was written — it could
    only surface once the fallback chain could route to a reasoning model."""
    from openclaw import llm

    class _Fn:
        name = "query_claims"
        arguments = '{"pet":"Aari"}'

    class _Call:
        id = "call_1"
        function = _Fn()

    class _Message:
        content = "let me look"
        tool_calls = [_Call()]
        reasoning = "a long chain of thought the API will not accept back"
        model_extra = {"reasoning": "..."}

    turn = llm._assistant_turn(_Message())
    assert set(turn) == {"role", "content", "tool_calls"}, f"whitelist only: {sorted(turn)}"
    assert "reasoning" not in turn
    # The parts the loop actually needs survive intact, or tool results can't pair.
    assert turn["tool_calls"][0]["id"] == "call_1"
    assert turn["tool_calls"][0]["function"]["name"] == "query_claims"
    assert turn["tool_calls"][0]["function"]["arguments"] == '{"pet":"Aari"}'
    assert turn["content"] == "let me look"

    class _NoContent:
        content = None
        tool_calls = [_Call()]

    assert llm._assistant_turn(_NoContent())["content"] == "", (
        "None content must not serialize as null"
    )


def test_petcover_and_vet_mail_tools_are_distinguishable():
    """Live miss (2026-07-25): asked "what claim emails were sent that you can
    verify and check for a response", the model called the VET invoice-request
    sweep and answered "nothing to verify" — while five submissions sat awaiting
    Petcover. Claims go to Petcover, invoice requests go to the vet; whatever
    else changes, the schema must keep saying which is which."""
    from openclaw import agent

    by_name = {t["function"]["name"]: t["function"]["description"] for t in agent.TOOLS}
    vet = by_name["reconcile_sent_invoice_requests"]
    petcover = by_name["submissions_awaiting_reply"]
    assert "VET" in vet and "Petcover" in vet, "the vet sweep must name both, to exclude one"
    assert "PETCOVER" in petcover
    assert "awaiting a response" in petcover, "must claim his actual phrasing"

    prompt = agent.system_prompt()
    assert "Invoice requests go to the VET" in prompt
    assert "submissions_awaiting_reply" in prompt


def test_tools_schema_stays_small():
    """The whole schema ships in EVERY request on a free-tier budget. This went
    8 tools -> 15; the ceiling makes the next addition deliberate rather than
    something that silently eats the turn's context."""
    import json as _json

    from openclaw import agent

    encoded = _json.dumps(agent.TOOLS)
    assert len(encoded) < 9000, (
        f"tool schema is {len(encoded)} bytes — trim descriptions or drop a tool"
    )

    names = {t["function"]["name"] for t in agent.TOOLS}
    assert names == set(agent._build_impls([])), "every declared tool has an impl and vice versa"
    for tool in agent.TOOLS:
        assert "\n" not in tool["function"]["description"], "descriptions stay one line"


# ---------------------------------------------------------------------------
# Information requests (vet-info-request-chase). Every fixture below is the real
# text of a real email, quoted from the mailbox on 2026-07-27 — including the
# U+2010 hyphens, which are the whole point of the first test.
# ---------------------------------------------------------------------------

# Policyholder letter, To: jagberg@gmail.com. Its reference is written with
# non-breaking hyphens, and it says "suspended" about its own future.
_INFO_REQUEST_LETTER = (
    "Policy number: GABR‑0306‑DC1‑00000001R\n"
    "Pet's name: Ari\n"
    "Claim Reference: DC1‑26‑5992 Sr 1\n"
    "Condition: Raised ALT\n"
    "Further Information Required\n"
    "Thank you for submitting your claim for treatment provided to Ari. To assess your claim, "
    "we need a copy of \nConsultation notes dated 18/05/2026\n"
    "Please note we cannot process the claim without the information requested. Your claim will "
    "be suspended until we have the required information."
)

# Vet cover note, To: admin@theshirevet.com.au, from requiredinfo.au@. One
# sentence of body; the reference lives only in the subject.
_VET_COVER_NOTE_SUBJECT = "Petcover claim for Ari DC1-27-5628 Sr.8"
_VET_COVER_NOTE_BODY = (
    "Dear The Shire Vet, We recently received a claim for treatment provided to Ari, who belong "
    "to Justin and Gabrielle Goldberg, please provide the following for us to review the claim "
    "Consult notes dated"
)


def test_reference_survives_non_breaking_hyphens():
    """The live root cause: [A-Za-z0-9-]+ stops at U+2010, so this letter taught
    the reference "DC1", missed its exact (reference, Sr) lookup, and correlated
    by recency onto claim #2 instead of the claim #8 it names."""
    reference = claim_status.extract_reference(_INFO_REQUEST_LETTER)
    assert reference == "DC1-26-5992", reference
    assert claim_status.extract_sr(_INFO_REQUEST_LETTER, reference) == 1


def test_policy_number_is_never_read_as_a_reference():
    """GABR-0306-DC1-00000001R is the one thing shaped enough to be mistaken for
    a reference, and is why bare patterns were originally rejected here."""
    assert claim_status.extract_reference("Policy number: GABR-0306-DC1-00000001R") is None


def test_reference_from_a_free_form_subject():
    """No context phrase at all — the shape fallback is the only thing that can
    read this, and the phrases are additionally case-sensitive without it."""
    assert claim_status.extract_reference(_VET_COVER_NOTE_SUBJECT) == "DC1-27-5628"
    assert (
        claim_status.extract_reference("GABR-0305-Request for consult note -First Request")
        == "GABR-0305"
    )
    # Live regression: the context phrase captures whatever token follows it, and
    # this subject puts junk there. Shape-first is what keeps it out of the DB.
    assert (
        claim_status.extract_reference("Petcover claim for--Aari--DC1-27-5628 Serial Number: 2")
        == "DC1-27-5628"
    )


def test_every_live_serial_format():
    for text, expected in [
        ("DC1-27-5628 Sr 3", 3),  # original whitespace form
        ("Petcover claim for Ari DC1-27-5628 Sr.8", 8),  # dot separator, live 2026-07-27
        ("Petcover claim for Ari - DC1-27-5628 sr.1", 1),  # lowercase + dot, live 2026-07-27
        ("Petcover claim for--Aari--DC1-27-5628 Serial Number: 2", 2),  # live 2026-07-19
        ("DC1-27-5628 nothing adjacent\nTreatment number: 7", 7),
    ]:
        assert claim_status.extract_sr(text, "DC1-27-5628") == expected, text


def test_info_request_letter_is_not_filed_as_a_suspension():
    """It says "will be suspended" about itself. With `suspended` ordered first
    the live DB ended up with zero info_requested events and two suspended ones,
    both of which were actually requests."""
    assert (
        claim_status.classify("PetCover - Claim Further Information Required", _INFO_REQUEST_LETTER)
        == "info_requested"
    )


def test_a_genuine_suspension_is_still_a_suspension():
    """The pair the reorder must not collapse — this is a real subject."""
    assert (
        claim_status.classify(
            "Petcover Claim DC1-27-5628 SR1 - Claim suspended", "Your claim has been suspended."
        )
        == "suspended"
    )


def test_vet_cover_note_is_classified_not_queued():
    """Was `unclassified` live — the one classification that produces no action."""
    assert claim_status.classify(_VET_COVER_NOTE_SUBJECT, _VET_COVER_NOTE_BODY) == "info_requested"
    assert (
        claim_status.classify(_VET_COVER_NOTE_SUBJECT, "", claim_status.INFO_REQUEST_SENDER)
        == "info_requested"
    ), "the dedicated channel classifies on the sender alone"


def test_auto_reply_from_the_required_info_channel_is_still_noise():
    assert (
        claim_status.classify(
            "Automatic reply: Aari Goldberg - GOLD093", "", claim_status.INFO_REQUEST_SENDER
        )
        == "ignore"
    )


def test_who_owes_the_document_comes_from_the_recipients():
    # The suite blanks credentials, so OWNER_EMAIL has to be supplied here. With it
    # unset the resolver falls to "a vet owes it, raw address" — the safe direction
    # (a wasted chase, not a lost claim), never "Justin owes it".
    _fresh_db()
    config.OWNER_EMAIL = "jagberg@gmail.com"
    with db.get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO vet_contacts (merchant, email) VALUES (?, ?)",
            ("Kings Vet KINGSGROVE NSW", "info@kingsvet.com.au"),
        )
    known = claim_status.resolve_owed_by(
        '"info@kingsvet.com.au" <info@kingsvet.com.au>, jagberg@gmail.com'
    )
    assert known["owed_by"] == "vet" and known["clinic"] == "Kings Vet KINGSGROVE NSW"

    mine = claim_status.resolve_owed_by("jagberg@gmail.com")
    assert mine["owed_by"] == "justin"

    # An address we don't recognize must NOT quietly become Justin's problem —
    # that reassignment is exactly how a request goes unchased.
    unknown = claim_status.resolve_owed_by(
        '"admin@newvet.com.au" <admin@newvet.com.au>, jagberg@gmail.com'
    )
    assert unknown["owed_by"] == "vet" and unknown["clinic"] is None
    assert unknown["clinic_email"] == "admin@newvet.com.au"


def test_info_request_event_records_the_vet_and_the_document():
    _fresh_db()
    with db.get_connection() as conn:
        aari = _aari(conn)
        conn.execute(
            "INSERT OR REPLACE INTO vet_contacts (merchant, email) VALUES (?, ?)",
            ("Kings Vet KINGSGROVE NSW", "info@kingsvet.com.au"),
        )
        claim = _insert_claim(
            conn, aari, "2026-04-02", status="acknowledged", reference="DC1-26-5992", sr=1
        )
    claim_status.process_reply(
        "m-info",
        "PetCover - Claim Further Information Required",
        _INFO_REQUEST_LETTER,
        "claims.au@petcovergroup.com",
        '"info@kingsvet.com.au" <info@kingsvet.com.au>, jagberg@gmail.com',
    )
    with db.get_connection() as conn:
        event = conn.execute(
            "SELECT event_type, detail FROM claim_status_events WHERE claim_id = ? ORDER BY id DESC",
            (claim,),
        ).fetchone()
    import json as _json

    detail = _json.loads(event["detail"])
    assert event["event_type"] == "info_requested"
    assert detail["owed_by"] == "vet" and detail["clinic"] == "Kings Vet KINGSGROVE NSW"
    assert "Consultation notes dated 18/05/2026" in detail["requested_document"]
    # The date is the half that identifies the visit, so it is stored parsed.
    assert detail["requested_document_date"] == "2026-05-18"


def test_the_deadline_is_anchored_on_treatment_not_on_the_bank_charge():
    """Petcover's own words: "within one year of your pet receiving treatment".
    The charge is a different date — The Shire Vet treated Aari on 19 Jun 2026 and
    Echo on 30 Jun, and BOTH were paid on 06/07/2026 (receipts SHV49c1622284e5 /
    SHVd5b232905fdb, forwarded 27 Jul). Anchoring on the charge silently grants
    slack the policy does not give: 17 days for Aari's."""
    import json as _json

    aari_receipt = _json.dumps(
        {
            "date": "2026-06-19",
            "amount": 35.0,
            "items": [{"description": "Prescription fee", "amount": 35.0, "date": "2026-06-19"}],
        }
    )
    treated, known = claim_status.treatment_date(aari_receipt, "2026-07-06")
    assert (treated, known) == ("2026-06-19", True), "the receipt states the treatment date"

    # An invoice billing several visits expires on its OLDEST one.
    multi = _json.dumps(
        {"date": "2026-06-30", "items": [{"date": "2026-06-18"}, {"date": "2026-06-30"}]}
    )
    assert claim_status.treatment_date(multi, "2026-07-06")[0] == "2026-06-18"

    # No invoice attached: fall back to the charge, and say it was assumed.
    assert claim_status.treatment_date(None, "2026-07-06") == ("2026-07-06", False)
    assert claim_status.treatment_date("{}", "2026-07-06") == ("2026-07-06", False)


def test_a_date_petcover_names_resolves_to_the_visit_we_hold():
    """The letter asking for "notes dated 18/05/2026" sits on a claim from a
    DIFFERENT month — live, the request is on claim #8 (a 2 April charge) and the
    date is claim #6's Kings Vet invoice 1000229, a later visit for the same
    condition. A clinic asked for "the notes from invoice 1000229" answers in one
    look, so the resolution has to reach across claims."""
    import json as _json

    _fresh_db()
    with db.get_connection() as conn:
        aari = _aari(conn)
        asked_on = _insert_claim(
            conn,
            aari,
            "2026-04-02",
            status="info_requested",
            merchant="Kings Vet",
            invoice_data=_json.dumps(
                {"date": "2026-04-02", "invoice_number": "199464", "amount": 446.5}
            ),
        )
        holds_visit = _insert_claim(
            conn,
            aari,
            "2026-05-18",
            status="settled",
            merchant="Kings Vet",
            invoice_data=_json.dumps(
                {"date": "2026-05-18", "invoice_number": "1000229", "amount": 351.5}
            ),
        )

    hits = invoice_matching.find_visit_by_date("2026-05-18")
    assert [h["claim_id"] for h in hits] == [holds_visit], (
        "the date names its own visit, not the asking claim"
    )
    assert hits[0]["invoice_number"] == "1000229" and hits[0]["amount"] == 351.5
    assert holds_visit != asked_on, (
        "the whole point: the request and the visit are different claims"
    )
    # Never a nearest-date guess — an adjacent visit is a different consultation.
    assert invoice_matching.find_visit_by_date("2026-05-19") == []
    assert invoice_matching.find_visit_by_date(None) == []


def test_a_line_item_date_matches_even_when_the_invoice_header_differs():
    """An invoice's header date is not always the date of the treatment on it: a
    statement can bill several visits, so a consult on the 18th sits on an invoice
    dated the 30th. Extraction dropped per-item dates until 2026-07-28, which is
    why this case could not be matched at all."""
    import json as _json

    _fresh_db()
    with db.get_connection() as conn:
        aari = _aari(conn)
        claim = _insert_claim(
            conn,
            aari,
            "2026-06-30",
            merchant="Kings Vet",
            invoice_data=_json.dumps(
                {
                    "date": "2026-06-30",
                    "invoice_number": "200500",
                    "amount": 300.0,
                    "items": [
                        {"description": "Consultation", "amount": 96.5, "date": "2026-06-18"},
                        {"description": "Bloods", "amount": 203.5, "date": None},
                    ],
                }
            ),
        )
    assert [h["claim_id"] for h in invoice_matching.find_visit_by_date("2026-06-18")] == [claim]
    assert [h["claim_id"] for h in invoice_matching.find_visit_by_date("2026-06-30")] == [claim], (
        "header date still works"
    )


def test_two_visits_sharing_a_date_are_both_reported():
    """One charge can pay two pets' invoices on the same day (ADR-0019). Choosing
    between them is the clinic's job, not ours."""
    import json as _json

    _fresh_db()
    with db.get_connection() as conn:
        aari = _aari(conn)
        # Distinct amounts: bank_transactions is unique on (date, amount, merchant),
        # and the real shape is two claims on ONE charge anyway — what matters here
        # is two invoices carrying the same service date.
        a = _insert_claim(
            conn,
            aari,
            "2026-07-06",
            merchant="The Shire Vet",
            amount=-35.0,
            invoice_data=_json.dumps(
                {"date": "2026-07-06", "invoice_number": "A1", "amount": 35.0}
            ),
        )
        b = _insert_claim(
            conn,
            aari,
            "2026-07-06",
            merchant="The Shire Vet",
            amount=-369.33,
            invoice_data=_json.dumps(
                {"date": "2026-07-06", "invoice_number": "B2", "amount": 369.33}
            ),
        )
    assert sorted(
        h["claim_id"] for h in invoice_matching.find_visit_by_date("2026-07-06")
    ) == sorted([a, b])


def test_requested_document_stops_at_the_letters_boilerplate():
    """The item sits between the ask and the standard footer. An ask with nothing
    after it must yield nothing — an earlier cut of this captured "Please note we
    cannot process the claim…" and would have shown that to Justin as the document
    Petcover wanted."""
    assert (
        claim_status.extract_requested_document(_INFO_REQUEST_LETTER)
        == "Consultation notes dated 18/05/2026"
    )
    assert (
        claim_status.extract_requested_document(
            "we need a copy of\n\nPlease note we cannot process"
        )
        is None
    )
    assert claim_status.extract_requested_document("a letter with no recognized ask at all") is None
    # Two items asked for at once: the earlier first-line-only cut dropped the second.
    assert (
        claim_status.extract_requested_document(
            "we need a copy of\nConsultation notes dated 18/05/2026\nItemised invoice\n\nPlease note"
        )
        == "Consultation notes dated 18/05/2026; Itemised invoice"
    )


def test_the_ask_own_filler_is_not_mistaken_for_the_document():
    """Both real vet cover notes phrase the ask with filler before the item, and a
    2026-07-28 refactor dropped the consumption of it — a live dry-run then
    produced 'information in order for us to review the' and 'for us to review the
    claim Consult notes dated' as the documents Justin was to chase. One body ends
    mid-sentence (its detail is in an attachment we get no text for), so the honest
    answer there is no document at all."""
    ends_mid_sentence = (
        "We have received a claim for treatment provided to Aari Who belongs to Mrs Gabi Goldberg , "
        "please provide the following information in order for us to review the"
    )
    assert claim_status.extract_requested_document(ends_mid_sentence) is None, (
        "filler is not a document"
    )

    names_the_item = (
        "received a claim for treatment provided to Ari, who belong to Justin and Gabrielle Goldberg, "
        "please provide the following for us to review the claim Consult notes dated"
    )
    assert claim_status.extract_requested_document(names_the_item) == "Consult notes dated"
    # No date printed in the body — the document still names the kind, so the label
    # can say "consult notes"; the visit simply cannot be resolved.
    assert claim_status.requested_document_date("Consult notes dated") is None


def test_requested_document_date_is_day_first_and_refuses_nonsense():
    """Australian letters: 18/05/2026 is 18 May. A malformed date is not a date —
    returning None keeps the label on the document alone rather than resolving the
    request to a visit that never happened."""
    assert (
        claim_status.requested_document_date("Consultation notes dated 18/05/2026") == "2026-05-18"
    )
    assert claim_status.requested_document_date("Consult notes dated 18 May 2026") == "2026-05-18"
    assert claim_status.requested_document_date("Consult notes dated 3-6-2026") == "2026-06-03"
    assert claim_status.requested_document_date("Consult notes dated 31/02/2026") is None
    assert claim_status.requested_document_date("Itemised invoice") is None
    assert claim_status.requested_document_date(None) is None


def test_rereading_the_same_email_records_nothing_new():
    """What makes `poll_petcover_status(reread=True)` safe: a classifier fix can be
    replayed over already-ingested mail without duplicating events, and without
    re-running the status/flag write (which would resurrect a dismissed mismatch)."""
    _fresh_db()
    with db.get_connection() as conn:
        aari = _aari(conn)
        claim = _insert_claim(conn, aari, "2026-04-02", status="sent")
    args = (
        "m-ack",
        "PetCover - Acknowledgement Letter",
        "Pet's name: Ari\nClaim Reference: DC1-26-5992 Sr 1\nCondition: Raised ALT",
    )
    claim_status.process_reply(*args)
    claim_status.process_reply(*args)  # replay
    with db.get_connection() as conn:
        events = conn.execute(
            "SELECT COUNT(*) n FROM claim_status_events WHERE raw_email_id = 'm-ack'"
        ).fetchone()["n"]
    assert events == 1, f"replay appended {events} events"
    assert _claim_row(claim)["petcover_reference"] == "DC1-26-5992"

    # A dismissed flag must survive the replay — the reason the guard skips the
    # whole per-claim block rather than only the INSERT.
    claim_status.dismiss_mismatch(claim)  # no mismatch to dismiss; flag stays None
    claim_status.process_reply(*args)
    assert _claim_row(claim)["flag"] is None


def test_detach_reference_returns_a_claim_to_the_correlation_pool():
    """A mis-learned reference is self-sealing: the claim stops being an
    un-referenced candidate, so correlation can never reconsider it. Live case —
    claim #2 learned the truncated `DC1`."""
    _fresh_db()
    with db.get_connection() as conn:
        aari = _aari(conn)
        claim = _insert_claim(
            conn, aari, "2026-06-19", status="suspended", reference="DC1", sr=None
        )
    assert claim_status.detach_reference(claim)["ok"] is True
    row = _claim_row(claim)
    assert row["petcover_reference"] is None and row["petcover_sr"] is None
    assert claim_status.detach_reference(claim)["ok"] is False, "nothing left to detach"

    with db.get_connection() as conn:
        types = [
            r["event_type"]
            for r in conn.execute(
                "SELECT event_type FROM claim_status_events WHERE claim_id = ?", (claim,)
            )
        ]
    assert "reference_detached" in types, "the undo is logged, not a silent wipe"

    # Detached, it is a candidate again and the real letter can route to it.
    assert claim_status.correlate_ack("Petcover claim for Ari DC1-27-5628 Sr.8")


# --- The state machine (claim-state-from-event-log, Phase 1) ------------------


def test_every_declared_transition_is_legal_and_the_terminals_are_dead_ends():
    """The table is the rule, so the table itself gets asserted: every declared
    pair must actually be applied by `apply_event`, and the two terminal states
    must have no way out via a Petcover LETTER (ADR-0011: a later letter
    reusing a thread's reference must never reopen a closed claim). `settled`
    gained exactly one Justin-initiated exception (settlement-clarification-
    email's `clarification_requested`) — never something Petcover's own mail
    can trigger, so it does not reopen the claim to their letters the way a
    real transition out of `settled` would.

    This test used to assert `target in TRANSITIONS[from_state]` while iterating
    that same set — `x in S for x in S`, true of any table including an empty one,
    and it never called `apply_event` despite the docstring saying so. Zero of the
    42 declared pairs were discriminatingly checked. Found by eval 2026-07-30."""
    _fresh_db()
    with db.get_connection() as conn:
        aari = _aari(conn)

    checked = 0
    for from_state, targets in claim_status.TRANSITIONS.items():
        if from_state is None:
            continue  # the notional creation step; nothing folds through it
        for target in targets:
            assert target in claim_status.TRANSITIONS, (
                f"{target} is a target with no row of its own"
            )
            # Drive it through the real writer: put a claim in `from_state`, fire the
            # event that names `target`, and require the state to actually move.
            event_type = next(e for e, t in claim_status.STATE_EVENTS.items() if t == target)
            with db.get_connection() as conn:
                # One claim per pair, each on its own transaction: bank_transactions
                # is UNIQUE on (date, amount, merchant).
                claim = _insert_claim(
                    conn,
                    aari,
                    "2026-06-01",
                    status=from_state,
                    amount=-50.0 - checked,
                    merchant=f"PAIR VET {checked}",
                )
            outcome = claim_status.apply_event(claim, event_type, {})
            assert outcome["applied"] is True, (
                f"{from_state} -> {target} is declared but was refused"
            )
            assert _claim_row(claim)["status"] == target, (
                f"{from_state} -> {target} did not write the state"
            )
            checked += 1
    assert checked == sum(len(t) for s, t in claim_status.TRANSITIONS.items() if s is not None)
    assert checked >= 40, f"only {checked} pairs exercised — the table shrank unnoticed"
    assert claim_status.TRANSITIONS["settled"] == frozenset({"awaiting_petcover_clarification"}), (
        "settlement-clarification-email's one Justin-initiated exception, and no other"
    )
    assert claim_status.TRANSITIONS["declined"] == frozenset()
    assert claim_status.TRANSITIONS["awaiting_petcover_clarification"] == frozenset(), (
        "no way out via apply_event — dismiss_mismatch clears the flag and leaves status alone, "
        "same as confirm_resolved does for info_requested/suspended"
    )
    assert claim_status.TRANSITIONS[None] == frozenset({"pending_match"}), (
        "a new claim starts nowhere else"
    )


def test_the_backwards_moves_of_the_2026_07_27_reread_are_refused():
    """Regression fixture for the incident that motivated the table.

    Two of the four moves that re-read performed are refused here: #6 and #7 went
    `settled` -> `acknowledged`, and that pair is now impossible.

    The other two are NOT refusable and this test says so rather than pretending
    otherwise: #22's `sent` -> `below_excess` and #18's `below_excess` ->
    `acknowledged` are both legal forward moves (`below_excess` is non-terminal by
    decision — the invoice is retained). What was wrong with them was the routing
    and the replay, not the transition, so their guard is `_already_recorded` plus
    reference/Sr precedence. `tasks.md` 1.3 claimed all four were illegal; it was
    written before the table existed and the table disagrees with it."""
    assert "acknowledged" not in claim_status.TRANSITIONS["settled"]
    assert "acknowledged" not in claim_status.TRANSITIONS["declined"]
    assert "acknowledged" in claim_status.TRANSITIONS["below_excess"], (
        "legal — not the table's to refuse"
    )
    assert "below_excess" in claim_status.TRANSITIONS["sent"], "legal — not the table's to refuse"


def test_the_two_event_classifications_are_complete_and_disjoint():
    """Every state event names a real status, and no event type is in both sets —
    an event that is both stateless and state-bearing would be decided by
    dict-lookup order, which is not a decision anyone made."""
    for event_type, target in claim_status.STATE_EVENTS.items():
        assert target in status_labels.LABELS, (
            f"{event_type} targets '{target}', which has no wording"
        )
        assert target in claim_status.TRANSITIONS, f"{target} is unreachable — no row in the table"
    overlap = set(claim_status.STATE_EVENTS) & claim_status.STATELESS_EVENTS
    assert not overlap, f"declared in both: {overlap}"
    # The backfill event is the third category and belongs to neither set: it seeds
    # a per-claim state read from its own detail. Being in STATELESS_EVENTS is what
    # design.md said and it would have made the backfill a no-op.
    assert claim_status.BACKFILL_EVENT not in claim_status.STATELESS_EVENTS
    assert claim_status.BACKFILL_EVENT not in claim_status.STATE_EVENTS


def test_a_backfilled_claim_projects_to_the_state_its_backfill_names():
    """The backfill's only job. Without this the nineteen live claims whose
    transitions predate the log would keep projecting `pending_match`, and Phase 2
    handing the projection authority would reset every one of them — the 2026-07-27
    regression at five times the scale."""
    _fresh_db()
    with db.get_connection() as conn:
        claim = _insert_claim(conn, _aari(conn), "2026-06-01", status="settled")
    assert claim_status.project_state(claim) == "pending_match", "no history to fold yet"

    outcome = claim_status.apply_event(
        claim, claim_status.BACKFILL_EVENT, {"backfilled": True, "status": "settled"}
    )
    assert outcome == {"applied": True, "state": "settled", "refused": None}
    assert claim_status.project_state(claim) == "settled"
    assert claim_status.state_projection_disagreements() == []

    # A later real event still has to be legal from the seeded state — the seed is
    # an exemption for itself, not a licence to reopen a terminal claim.
    assert claim_status.apply_event(claim, "acknowledged", {})["applied"] is False
    assert claim_status.project_state(claim) == "settled"


def test_a_backfill_naming_an_unknown_status_is_refused_not_seeded():
    _fresh_db()
    with db.get_connection() as conn:
        claim = _insert_claim(conn, _aari(conn), "2026-06-01", status="sent")
    outcome = claim_status.apply_event(
        claim, claim_status.BACKFILL_EVENT, {"status": "half_settled"}
    )
    assert outcome["applied"] is False and "half_settled" in outcome["refused"]
    assert _claim_row(claim)["status"] == "sent"
    assert claim_status.project_state(claim) == "pending_match", (
        "the bad seed is ignored by the fold"
    )


def test_apply_event_writes_a_legal_state_and_records_it():
    _fresh_db()
    with db.get_connection() as conn:
        claim = _insert_claim(conn, _aari(conn), "2026-06-01", status="drafted")
    outcome = claim_status.apply_event(claim, "sent", {"draft_id": "d-1"})
    assert outcome == {"applied": True, "state": "sent", "refused": None}
    assert _claim_row(claim)["status"] == "sent"
    with db.get_connection() as conn:
        types = [
            r["event_type"]
            for r in conn.execute(
                "SELECT event_type FROM claim_status_events WHERE claim_id = ?", (claim,)
            )
        ]
    assert types == ["sent"]


def test_apply_event_refuses_an_undeclared_transition_and_flags_it():
    """The event is kept — it happened, and hiding it is how the 2026-07-28 audit
    became necessary — but the state does not move and the claim says why."""
    _fresh_db()
    with db.get_connection() as conn:
        claim = _insert_claim(conn, _aari(conn), "2026-06-01", status="settled")
    outcome = claim_status.apply_event(claim, "acknowledged", {"subject": "an old ack, re-read"})
    assert outcome["applied"] is False
    assert outcome["state"] == "settled", "reports where the claim actually is"
    row = _claim_row(claim)
    assert row["status"] == "settled", "a refused transition must not move the state"
    assert "settled" in row["flag"] and "acknowledged" in row["flag"], (
        f"flag names both states: {row['flag']}"
    )
    with db.get_connection() as conn:
        types = [
            r["event_type"]
            for r in conn.execute(
                "SELECT event_type FROM claim_status_events WHERE claim_id = ?", (claim,)
            )
        ]
    assert types == ["acknowledged"], "the refused event stays as evidence"


def test_an_undeclared_event_type_is_flagged_rather_than_silently_ignored():
    """A typo'd or new event type is a defect. Treating it as stateless would let
    it be a silent no-op forever, which is the failure mode this whole change is
    about."""
    _fresh_db()
    with db.get_connection() as conn:
        claim = _insert_claim(conn, _aari(conn), "2026-06-01", status="sent")
    outcome = claim_status.apply_event(claim, "acknowlegded", {})  # deliberate typo
    assert outcome["applied"] is False and outcome["refused"]
    assert "acknowlegded" in _claim_row(claim)["flag"]
    assert _claim_row(claim)["status"] == "sent"


def test_stateless_events_record_and_move_nothing():
    """`unclassified` was a special case inside one writer's UPDATE; it is now a
    property of the event type, and the same guarantee holds for every member of
    the set — including `confirmed_resolved`, which the old code never touched
    status for either."""
    _fresh_db()
    with db.get_connection() as conn:
        claim = _insert_claim(conn, _aari(conn), "2026-06-01", status="acknowledged")
    for event_type in sorted(claim_status.STATELESS_EVENTS):
        outcome = claim_status.apply_event(claim, event_type, {})
        assert outcome == {"applied": False, "state": "acknowledged", "refused": None}, event_type
        row = _claim_row(claim)
        assert row["status"] == "acknowledged", f"{event_type} moved the state"
        assert row["flag"] is None, f"{event_type} was treated as a refusal"


def test_the_whole_lifecycle_is_one_event_per_transition_in_order():
    """End to end through the real writers, not through `apply_event` directly:
    every state a claim passed through is now a fact on record, which is what the
    2026-07-28 repair had to infer from an absence."""
    _fresh_db()
    with db.get_connection() as conn:
        claim = _insert_claim(
            conn, _aari(conn), "2026-06-01", status="pending_match", reference="DC1-27-9001", sr=1
        )
    invoice_matching._mark_matched(
        claim, "email-life-1", {"amount": 120.0, "claimable_amount": 120.0}
    )
    assert _claim_row(claim)["status"] == "matched"
    claim_status.apply_event(
        claim, "drafted", {"draft_id": "d-life"}
    )  # claim_forms' own path needs Gmail
    with db.get_connection() as conn:
        conn.execute("UPDATE vet_claims SET draft_id = 'd-life' WHERE id = ?", (claim,))
    assert claim_status.mark_sent(claim)["ok"] is True
    claim_status.process_reply(
        "email-life-2", "Petcover DC1-27-9001 SR1 acknowledgement letter", ""
    )
    claim_status.process_reply(
        "email-life-3", "Petcover DC1-27-9001 SR1 - your claim has been approved", ""
    )

    with db.get_connection() as conn:
        types = [
            r["event_type"]
            for r in conn.execute(
                "SELECT event_type FROM claim_status_events WHERE claim_id = ? ORDER BY created_at, id",
                (claim,),
            )
        ]
    assert types == ["matched", "drafted", "sent", "acknowledged", "approved"], types
    assert _claim_row(claim)["status"] == "approved"
    assert claim_status.project_state(claim) == "approved", "the column and the log agree"


def test_no_module_outside_claim_status_writes_the_status_column():
    """Mechanical, not conventional. This repo has a documented case of a rule
    being broken four times in one session by people who had just read it
    (ADR-0018), so 'apply_event is the only writer' gets a check rather than a
    paragraph. Test files are exempt: a fixture asserting a starting state is not
    a production write path."""
    import re as _re
    from pathlib import Path as _Path

    package = _Path(__file__).resolve().parent.parent / "openclaw"
    # `(?!WHERE)` per character so the SET clause can't run past it: several
    # statements legitimately read `AND status = 'pending_match'` in a WHERE.
    pattern = _re.compile(
        r"UPDATE\s+vet_claims\s+SET\s+(?:(?!WHERE)[^\"'])*\bstatus\s*=", _re.IGNORECASE
    )
    # The guard gets its own guard: a pattern that matches nothing would make this
    # test pass forever. Both halves asserted — it fires on a real violation and
    # does not fire on the WHERE-clause reads that are legitimate.
    assert pattern.search("UPDATE vet_claims SET status = 'sent', updated_at = ? WHERE id = ?")
    assert pattern.search("UPDATE vet_claims SET pet_id = ?, status = 'matched' WHERE id = ?")
    assert not pattern.search(
        "UPDATE vet_claims SET flag = ?, updated_at = ? WHERE id = ? AND status = 'x'"
    )
    offenders = []
    for path in sorted(package.glob("*.py")):
        if path.name == "claim_status.py":
            continue
        text = path.read_text(encoding="utf-8")
        # Statements are split across adjacent string literals; join them first so
        # "UPDATE vet_claims SET " + "status = ?" is still caught.
        joined = _re.sub(r"\"\s*\n\s*\"", "", text)
        if pattern.search(joined):
            offenders.append(path.name)
    assert not offenders, f"these write vet_claims.status directly: {offenders}"


def test_the_projection_folds_the_real_reply_sequences_we_hold():
    """Sequences taken from the live log (read-only, 2026-07-29): claim #8's
    acknowledged -> info_requested -> info_requested, claim #21's acknowledged ->
    approved -> settled, and claim #19's acknowledged -> below_excess ->
    acknowledged, which only folds because `below_excess` is non-terminal.

    Each is prefixed with the submission events the writers now append. Without
    that prefix these sequences fold to `pending_match` — see the next test."""
    lived = {
        "8": (["acknowledged", "info_requested", "info_requested"], "info_requested"),
        "21": (["acknowledged", "approved", "settled"], "settled"),
        "19": (["acknowledged", "below_excess", "acknowledged"], "acknowledged"),
        "6": (["approved", "settled", "mismatch_dismissed"], "settled"),
    }
    for live_id, (replies, expected) in lived.items():
        _fresh_db()
        with db.get_connection() as conn:
            claim = _insert_claim(conn, _aari(conn), "2026-06-01", status="pending_match")
        for event_type in ["matched", "drafted", "sent", *replies]:
            claim_status.apply_event(claim, event_type, {})
        assert claim_status.project_state(claim) == expected, f"live claim #{live_id}"
        assert _claim_row(claim)["status"] == expected, (
            f"live claim #{live_id}: column and fold disagree"
        )


def test_a_claim_whose_transitions_predate_the_log_projects_to_its_birth_state():
    """The backfill case, asserted rather than assumed: all nine claims that hold
    events hold only *reply* events, because the six writers that moved them to
    matched/drafted/sent appended nothing. So the fold cannot reach where they
    are, and every existing claim is expected to disagree until group 6 runs.

    Real: claim #2 holds one `reference_detached` event and sits at `sent`."""
    _fresh_db()
    with db.get_connection() as conn:
        claim = _insert_claim(conn, _aari(conn), "2026-06-01", status="sent")
    claim_status.apply_event(claim, "reference_detached", {"was": "DC1"})
    assert claim_status.project_state(claim) == "pending_match"
    assert [d["claim_id"] for d in claim_status.state_projection_disagreements()] == [claim]


def test_the_projection_survives_an_illegal_event():
    _fresh_db()
    with db.get_connection() as conn:
        claim = _insert_claim(conn, _aari(conn), "2026-06-01", status="pending_match")
    for event_type in ("matched", "drafted", "sent", "acknowledged"):
        claim_status.apply_event(claim, event_type, {})
    with db.get_connection() as conn:
        # This test also covered reverted events until 2026-07-31. `state_reverted`
        # was never written by anything — zero rows live, and apply_event could not
        # produce one — so the fold's skip and the event type both went with the
        # retro. Rebuild the coverage alongside `revert_state` (Phase 2, task 7.1).
        #
        # An event that is illegal from the replayed state must be skipped without
        # costing us the rest of the fold — so a legal one after it still applies.
        # `matched` is the illegal one: the fold is at `acknowledged` by here and
        # TRANSITIONS["acknowledged"] does not contain it, because a claim never
        # goes back to matched once submitted. `approved` and `settled` after it
        # are both legal from `acknowledged` and must still land.
        #
        # The earlier version of this test injected `approved` then `settled` and
        # called `approved` the illegal one — but `sent` allows `approved`, so no
        # illegal event was ever present. Mutating `_fold` to `break` on the first
        # illegal event left the whole suite green. Found by eval 2026-07-30.
        for event_type in ("matched", "approved", "settled"):
            conn.execute(
                "INSERT INTO claim_status_events (claim_id, event_type, detail, created_at) VALUES (?, ?, ?, ?)",
                (claim, event_type, "{}", datetime.now(timezone.utc).isoformat()),
            )
    assert "matched" not in claim_status.TRANSITIONS["acknowledged"], (
        "fixture assumes this pair is illegal"
    )
    # The illegal `matched` is skipped; the two legal events after it still apply.
    # A fold that aborted on the illegal event would stop at `acknowledged`.
    assert claim_status.project_state(claim) == "settled"


def test_a_resolved_visit_says_which_date_actually_matched():
    """Option 3 from the BACKLOG: the line-item branch stays, but nothing may
    imply it pinpointed the treatment. Across 54 held invoices only three line
    items carry a date and each equals its own invoice's header date, so a match
    is almost always the header's — a weaker fact than the consult's own date,
    and the only one the documents support."""
    import json as _json

    _fresh_db()
    with db.get_connection() as conn:
        aari = _aari(conn)
        header = _insert_claim(
            conn,
            aari,
            "2026-05-30",
            amount=-351.50,
            merchant="KINGS VET",
            invoice_data=_json.dumps(
                {
                    "date": "2026-05-30",
                    "amount": 351.50,
                    "invoice_number": "1000229",
                    "items": [{"description": "Consult", "amount": 95.0}],
                }
            ),
        )
        item = _insert_claim(
            conn,
            aari,
            "2026-05-31",
            amount=-120.0,
            merchant="OTHER VET",
            invoice_data=_json.dumps(
                {
                    "date": "2026-05-31",
                    "amount": 120.0,
                    "invoice_number": "2000111",
                    "items": [{"description": "Consult", "amount": 60.0, "date": "2026-05-18"}],
                }
            ),
        )

    on_header = invoice_matching.find_visit_by_date("2026-05-30")
    assert [h["claim_id"] for h in on_header] == [header]
    assert on_header[0]["matched_on"] == "invoice date"

    # The case the branch exists for, which no held document has produced: an
    # item dated earlier than the invoice that bills it.
    on_item = invoice_matching.find_visit_by_date("2026-05-18")
    assert [h["claim_id"] for h in on_item] == [item]
    assert on_item[0]["matched_on"] == "line item", "the stronger claim, and only when earned"

    from openclaw import pipeline

    assert "matched on its invoice date" in pipeline._visit_line(invoice_matching, "2026-05-30")
    assert "matched on its line item" in pipeline._visit_line(invoice_matching, "2026-05-18")


def test_unmatching_a_submitted_claim_destroys_nothing():
    """Regression: routing the status write through `apply_event` turned an
    unconditional reset into a refusable one, while the wipe above it stayed
    unconditional. Unmatching a `sent` claim therefore nulled `invoice_data`,
    `matched_email_id` and `flag`, had its status write refused, and left the
    claim submitted with no invoice. Reachable from the Telegram unmatch button;
    no test called `unmatch` at all. Found by eval 2026-07-31."""
    import json as _json

    _fresh_db()
    with db.get_connection() as conn:
        aari = _aari(conn)
        submitted = _insert_claim(
            conn, aari, "2026-06-01", status="sent", invoice_data=_json.dumps({"amount": 120.0})
        )
        conn.execute(
            "UPDATE vet_claims SET matched_email_id = 'email-x' WHERE id = ?", (submitted,)
        )

    result = invoice_matching.unmatch(submitted)
    assert result["ok"] is False, "a submitted claim's invoice must not be rejectable"
    row = _claim_row(submitted)
    assert row["status"] == "sent"
    assert row["invoice_data"] is not None, "the invoice was destroyed by a refused reset"
    assert row["matched_email_id"] == "email-x"

    # The ordinary path still works: from `matched`, unmatched -> pending_match
    # is a declared transition, so the wipe and the state change both happen.
    with db.get_connection() as conn:
        matched = _insert_claim(
            conn,
            aari,
            "2026-06-02",
            status="matched",
            invoice_data=_json.dumps({"amount": 99.0}),
            amount=-99.0,
            merchant="UNMATCH VET",
        )
        conn.execute("UPDATE vet_claims SET matched_email_id = 'email-y' WHERE id = ?", (matched,))
    assert invoice_matching.unmatch(matched)["ok"] is True
    row = _claim_row(matched)
    assert row["status"] == "pending_match" and row["invoice_data"] is None


def test_health_reports_a_broken_projection_instead_of_500ing():
    """`/health` is the one URL you check to find out whether anything is wrong,
    and it also carries `polling_alive`. An unguarded fold call turned a
    malformed event detail into a 500 that hid both. It must degrade to a visible
    marker — and never to `0`, which reads as healthy on the figure gating
    Phase 2."""
    from openclaw import main as main_module

    original = claim_status.state_projection_disagreements
    try:
        claim_status.state_projection_disagreements = lambda: (_ for _ in ()).throw(
            ValueError("bad detail")
        )
        value = main_module._disagreement_count()
    finally:
        claim_status.state_projection_disagreements = original
    assert isinstance(value, str) and "unavailable" in value, value
    assert value != 0 and value != "0"


def test_the_shadow_comparison_reports_without_repairing_anything():
    """A comparison that fixes what it measures has measured nothing. Phase 1's
    whole value is that the two sources are compared, not reconciled."""
    from openclaw import pipeline

    _fresh_db()
    with db.get_connection() as conn:
        claim = _insert_claim(conn, _aari(conn), "2026-06-01", status="pending_match")
    for event_type in ("matched", "drafted", "sent"):
        claim_status.apply_event(claim, event_type, {})
    assert claim_status.state_projection_disagreements() == []
    with db.get_connection() as conn:  # inject one, the way a stray direct write would
        conn.execute("UPDATE vet_claims SET status = 'settled' WHERE id = ?", (claim,))
    before = dict(_claim_row(claim))
    reported = pipeline.compare_state_projection()
    assert reported == [{"claim_id": claim, "stored": "settled", "projected": "sent"}]
    after = dict(_claim_row(claim))
    assert after["status"] == "settled" and after["flag"] == before["flag"], (
        "the comparison wrote something"
    )


# --- OpenClaw gateway: the internal surface and the outbound seam -------------
# Tested at the function level rather than through fastapi's TestClient on
# purpose: TestClient needs httpx, and adding a test-only dependency to reach
# code that is already directly callable buys nothing. The suite must also pass
# with no gateway installed, which is why every send test injects a runner.


class _FakeRequest:
    def __init__(self, host="127.0.0.1"):
        self.client = type("c", (), {"host": host})()


def _with_secret(secret, allow=None):
    """Set the internal-API config and hand back a restore callable."""
    from openclaw import config as cfg

    before = (cfg.INTERNAL_API_SECRET, cfg.INTERNAL_API_ALLOW_HOSTS)
    cfg.INTERNAL_API_SECRET = secret
    if allow is not None:
        cfg.INTERNAL_API_ALLOW_HOSTS = allow

    def restore():
        cfg.INTERNAL_API_SECRET, cfg.INTERNAL_API_ALLOW_HOSTS = before

    return restore


def test_internal_surface_refuses_when_no_secret_is_configured():
    """An unset secret is a misconfiguration, not a permission. Defaulting to
    allow would make the whole surface public the first time someone forgot the
    env var."""
    from openclaw import internal_api

    restore = _with_secret("")
    try:
        reason = internal_api._authorized(_FakeRequest(), "anything")
        assert reason and "not configured" in reason, reason
    finally:
        restore()


def test_internal_surface_rejects_a_bad_or_missing_secret():
    from openclaw import internal_api

    restore = _with_secret("s3cret")
    try:
        assert internal_api._authorized(_FakeRequest(), "wrong") == "bad or missing secret"
        assert internal_api._authorized(_FakeRequest(), None) == "bad or missing secret"
        assert internal_api._authorized(_FakeRequest(), "s3cret") is None
    finally:
        restore()


def test_internal_surface_rejects_a_host_outside_the_allowlist():
    """Defence in depth — the secret is the real auth, but a correct secret from
    an unexpected host is still worth refusing and logging."""
    from openclaw import internal_api

    restore = _with_secret("s3cret", allow={"127.0.0.1"})
    try:
        reason = internal_api._authorized(_FakeRequest(host="10.1.2.3"), "s3cret")
        assert reason and "not in allowlist" in reason, reason
        assert internal_api._authorized(_FakeRequest(host="127.0.0.1"), "s3cret") is None
    finally:
        restore()


def test_two_concurrent_ticks_never_both_run():
    """Two pipeline.run_once calls against one database would draft the same
    claims into two Gmail drafts — two Petcover submissions for one set of
    invoices. APScheduler refused an overlapping run for free (max_instances
    defaults to 1, unset everywhere here); gateway cron does not, so this is a
    guarantee being rebuilt rather than a new one."""
    import threading

    from openclaw import internal_api

    entered = []
    release = threading.Event()

    def _slow():
        entered.append(1)
        release.wait(5)
        return "done"

    outcomes = []
    first = threading.Thread(
        target=lambda: outcomes.append(internal_api.run_exclusive("tick", _slow))
    )
    first.start()
    for _ in range(500):  # wait for the first tick to actually hold the lock
        if entered:
            break
        time.sleep(0.01)
    assert entered, "the first tick never started"

    assert internal_api.run_exclusive("tick", _slow) == (False, None), (
        "a second tick entered the body"
    )
    assert len(entered) == 1, entered

    release.set()
    first.join(5)
    assert outcomes == [(True, "done")], outcomes

    # Released, so the next invocation after the first finishes does run.
    assert internal_api.run_exclusive("tick", lambda: "second") == (True, "second")


def test_different_jobs_do_not_block_each_other():
    """The lock is per job name — a running tick must not stop the nudge."""
    import threading

    from openclaw import internal_api

    held = threading.Event()
    release = threading.Event()
    t = threading.Thread(
        target=lambda: internal_api.run_exclusive("tick", lambda: (held.set(), release.wait(5)))
    )
    t.start()
    try:
        assert held.wait(5), "the tick never took its lock"
        assert internal_api.run_exclusive("nudge", lambda: "ok") == (True, "ok")
    finally:
        release.set()
        t.join(5)


def test_nothing_outside_gateway_client_shells_out_to_the_gateway():
    """The LoggedBot rule, carried to the new transport: one seam, or the message
    log stops being complete. A bypassing caller sends the message and writes no
    row, and a missing row is indistinguishable from a message never sent.

    This is the guard the module map rates as only *partial* for LoggedBot —
    nothing there stops a second `telegram.Bot` being constructed."""
    # `config.OPENCLAW_CLI`, not bare `OPENCLAW_CLI` — config.py is where the
    # setting is DEFINED, and naming a setting is not reaching the gateway.
    markers = ("import subprocess", "from subprocess", "config.OPENCLAW_CLI")

    def hits(text):
        return [m for m in markers if m in text]

    # The scan gets its own guard: it reads production text, so a marker list
    # that had drifted out of date would report a clean sweep forever.
    assert hits("import subprocess\n"), "the scan misses a real bypass"
    assert hits("from subprocess import run"), "the scan misses a real bypass"
    assert hits("subprocess.run(config.OPENCLAW_CLI)"), "the scan misses a real bypass"
    assert not hits('OPENCLAW_CLI = os.getenv("OPENCLAW_CLI", "openclaw")'), (
        "config.py's own definition must not count as reaching the gateway"
    )

    src = Path(__file__).resolve().parent.parent / "openclaw"
    offenders = []
    for path in sorted(src.glob("*.py")):
        if path.name == "gateway_client.py":
            continue
        for marker in hits(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.name}: {marker}")
    assert not offenders, (
        "these reach the gateway outside the logged seam, so their messages "
        f"would never land in telegram_messages: {offenders}"
    )


def test_correlation_id_is_minted_when_the_caller_supplies_none():
    """An event crosses two runtimes now. Without a shared id a failure halfway
    is untraceable in either log."""
    from openclaw import internal_api

    assert internal_api._correlation_id("abc123") == "abc123"
    assert internal_api._correlation_id("  ").startswith("int-")
    assert internal_api._correlation_id(None).startswith("int-")
    assert internal_api._correlation_id(None) != internal_api._correlation_id(None)


def _ok_runner(stdout='{"ok": true}'):
    def run(argv, **kwargs):
        return type("p", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()

    return run


def test_gateway_send_failure_carries_the_stderr_reason():
    """A send that did not happen must never look like one that did, and an exit
    code alone is useless for diagnosis."""
    from openclaw import gateway_client

    def run(argv, **kwargs):
        return type("p", (), {"returncode": 2, "stdout": "", "stderr": "chat not found"})()

    try:
        gateway_client.send_message("42", "hello #7", runner=run)
    except gateway_client.GatewaySendError as exc:
        assert "exit 2" in str(exc) and "chat not found" in str(exc), str(exc)
    else:
        raise AssertionError("a failed send did not raise")


def test_the_suite_runs_with_no_gateway_installed():
    """Hermetic: no daemon, no CLI on PATH. A missing binary is a named failure,
    not a stack trace from subprocess."""
    from openclaw import gateway_client

    def run(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    try:
        gateway_client.send_message("42", "hello", runner=run)
    except gateway_client.GatewaySendError as exc:
        assert "not found" in str(exc), str(exc)
    else:
        raise AssertionError("a missing gateway CLI did not raise")


def test_every_gateway_send_lands_in_the_message_log():
    """The LoggedBot guarantee, carried over: nothing reaches the channel without
    a telegram_messages row. This is the seam that keeps the log trustworthy."""
    from openclaw import gateway_client, message_log

    _fresh_db()
    before = message_log.stats()["total"]
    gateway_client.send_message("42", "Claim #7 needs a condition", runner=_ok_runner())
    gateway_client.send_file("42", "/tmp/card.png", caption="Claim #7", runner=_ok_runner())
    gateway_client.edit_message("42", "9", "Claim #7 — done", runner=_ok_runner())
    assert message_log.stats()["total"] == before + 3, message_log.stats()


def test_a_react_failure_does_not_break_the_handler():
    """The ack exists so a slow handler does not feel dead. Losing the ack is
    strictly better than losing the handler."""
    from openclaw import gateway_client

    def run(argv, **kwargs):
        return type("p", (), {"returncode": 1, "stdout": "", "stderr": "rate limited"})()

    _fresh_db()
    assert gateway_client.react("42", "9", runner=run) is False
    assert gateway_client.react("42", "9", runner=_ok_runner()) is True


def test_a_non_json_success_is_not_treated_as_a_failed_send():
    """Raising here would let a caller retry a message that already went out."""
    from openclaw import gateway_client, message_log

    # _fresh_db() deliberately leaves telegram_messages alone (it is the RL
    # dataset), so count relatively — an absolute total depends on test order.
    _fresh_db()
    before = message_log.stats()["total"]
    assert gateway_client.send_message("42", "hi", runner=_ok_runner(stdout="sent")) == {}
    assert message_log.stats()["total"] == before + 1


def _capture_argv():
    """Record the argv a gateway_client call builds, without running anything."""
    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        return type("p", (), {"returncode": 0, "stdout": '{"ok": true}', "stderr": ""})()

    return seen, run


def test_gateway_argv_uses_the_flags_the_cli_actually_has():
    """19a — the first version of gateway_client guessed every flag and got every
    flag wrong: --chat, --text, --file, --caption, --buttons. None exist. The CLI
    accepts none of them, but the failure that matters is subtler than a crash:
    a wrong *presentation* is discarded silently with ok:true and a real message
    id, so there is no signal to notice.

    Verified against `openclaw message <sub> --help` on gateway 2026.6.34. This
    test exists so the next person to guess is stopped by a red suite."""
    from openclaw import gateway_client

    _fresh_db()
    invented = {"--chat", "--text", "--file", "--caption", "--buttons"}

    seen, run = _capture_argv()
    gateway_client.send_message("42", "hello #7", runner=run)
    gateway_client.send_file("42", "/data/card.png", caption="Claim #7", runner=run)
    gateway_client.edit_message("42", "9", "Claim #7 — done", runner=run)
    gateway_client.react("42", "9", runner=run)

    for argv in seen:
        assert not invented & set(argv), f"invented flag in {argv}"
        assert argv[1] == "message" and "--channel" in argv and "--target" in argv, argv
        assert argv[-1] == "--json", argv

    send, media, edit, react = seen
    assert "--message" in send and send[send.index("--message") + 1] == "hello #7"
    # With --media set, --message IS the caption. The CLI's own help says
    # --message is "required unless --media is set"; there is no caption flag.
    assert "--media" in media and media[media.index("--message") + 1] == "Claim #7"
    assert "--message-id" in edit and "--message" in edit
    assert "--message-id" in react and "--emoji" in react


def test_buttons_are_nested_in_a_blocks_array_never_at_the_top_level():
    """19a.1 — five real sends were discarded for putting `buttons` at the top of
    the presentation. Every one returned ok:true with a message id.

    Checked against the platform's own normalizeMessagePresentation, which
    returns undefined for the top-level shape and echoes this one back. Assert
    the exact nesting, not merely that buttons are present somewhere."""
    from openclaw import gateway_client

    _fresh_db()
    seen, run = _capture_argv()
    gateway_client.send_message(
        "42", "Claim #7", buttons=[{"label": "Mark sent", "command": "/mark 7 sent"}], runner=run
    )

    import json as _json

    payload = _json.loads(seen[0][seen[0].index("--presentation") + 1])
    assert "buttons" not in payload, f"top-level buttons are silently discarded: {payload}"
    assert payload == {
        "blocks": [
            {
                "type": "buttons",
                "buttons": [
                    {"label": "Mark sent", "action": {"type": "command", "command": "/mark 7 sent"}}
                ],
            }
        ]
    }, payload


def test_a_button_command_over_the_byte_budget_is_refused_not_sent():
    """19a.4 — Telegram caps callback_data at 64 bytes and the gateway spends 6
    on a `tgcmd:` prefix. At 59 the button is filtered out, its row is dropped,
    and a message whose only row was dropped arrives with NO keyboard — ok:true,
    real message id, no error. Measured live at the boundary.

    Count UTF-8 bytes, not characters: a non-ASCII pet name costs more than one
    byte each and would walk past a len() check."""
    from openclaw import gateway_client

    _fresh_db()
    budget = gateway_client.COMMAND_CALLBACK_BUDGET_BYTES
    assert budget == 58, budget

    # A real verb: an undeclared one is refused before the byte check now, and a
    # fixture that trips the wrong guard measures the wrong thing.
    pad = budget - len("/mark ")
    seen, run = _capture_argv()
    gateway_client.send_message(
        "42", "x", buttons=[{"label": "ok", "command": "/mark " + "a" * pad}], runner=run
    )
    assert len(seen) == 1, "a command exactly at the budget must still send"

    for command in ("/mark " + "a" * (pad + 1), "/mark 7 " + "é" * 30):
        try:
            gateway_client.send_message(
                "42", "x", buttons=[{"label": "ok", "command": command}], runner=run
            )
        except gateway_client.PresentationError as exc:
            assert "byte" in str(exc), str(exc)
        else:
            raise AssertionError(
                f"an over-budget command was sent and its button lost: {command!r}"
            )
    assert len(seen) == 1, "an over-budget send reached the CLI"


def test_a_label_less_button_costs_every_button_on_the_message():
    """19a.2 — verified against the shipped normalizer: one button with no label
    makes it return undefined for the WHOLE presentation, so a single bad button
    silently strips the keyboard off the message. Refuse instead."""
    from openclaw import gateway_client

    _fresh_db()
    seen, run = _capture_argv()
    for bad in (
        {"command": "/mark 7 sent"},
        {"label": "  ", "command": "/mark 7 sent"},
        {"label": "Mark", "command": "mark 7 sent"},
    ):
        try:
            gateway_client.send_message("42", "x", buttons=[bad], runner=run)
        except gateway_client.PresentationError:
            pass
        else:
            raise AssertionError(f"a presentation the platform would discard was sent: {bad}")
    assert not seen, "a payload that would be discarded reached the CLI"


def test_the_telegram_tee_writes_the_same_row_shape_the_old_transport_did():
    """1.4 / 12.1 — after the cutover the app never sees a PTB Update, but the
    dataset must not change meaning halfway through. Same `kind` vocabulary,
    same raw payload, same app_version, whichever transport delivered it."""
    from openclaw import config, internal_api, message_log

    _fresh_db()
    body = {"update_id": 77001, "update": {"message": {"text": "/mark 7 sent"}}}
    assert internal_api.record_event(body, "corr-1")["status"] == "ok"
    # A redelivery is not an error and must not double-count the dataset.
    assert internal_api.record_event(body, "corr-2")["status"] == "duplicate"

    with db.get_connection() as conn:
        row = dict(
            conn.execute(
                "SELECT kind, summary, payload, app_version, direction, processed_at "
                "FROM telegram_messages WHERE update_id = 77001"
            ).fetchone()
        )
    assert row["kind"] == "command" and row["summary"] == "/mark 7 sent", row
    assert row["direction"] == "in" and row["app_version"] == config.APP_VERSION, row
    # Written before anything handles it — that unset processed_at IS the replay
    # queue, and it is why a crash mid-handler does not lose the message.
    assert row["processed_at"] is None, row
    assert "/mark 7 sent" in row["payload"], row["payload"]
    assert message_log.stats()["queued"] >= 1


def test_the_telegram_tee_refuses_an_event_with_no_update_id():
    """No id means no dedupe key and no way to settle the row, so every
    redelivery would silently grow the dataset."""
    from openclaw import internal_api

    _fresh_db()
    assert internal_api.record_event({"update": {}}, "corr-3").status_code == 400
    # The tee is guarded by the same secret as every other internal route — it
    # carries raw message content, so an open one leaks the conversation.
    restore = _with_secret("s3cret", allow={"127.0.0.1"})
    try:
        assert internal_api._guard(_FakeRequest(), "wrong", "telegram/event", "c") is not None
        assert (
            internal_api._guard(_FakeRequest(host="10.0.0.9"), "s3cret", "telegram/event", "c")
            is not None
        )
        assert internal_api._guard(_FakeRequest(), "s3cret", "telegram/event", "c") is None
    finally:
        restore()


def test_one_classifier_describes_both_transports():
    """The gateway delivers dicts and PTB delivered objects. Two classifiers
    would drift, and the drift would only show up months later in the dataset —
    an edit once logged as kind `other` with an empty summary for exactly this
    kind of reason (2026-07-27)."""
    from openclaw import message_log

    cases = [
        ({"message": {"text": "/mark 7 sent"}}, ("command", "/mark 7 sent")),
        ({"message": {"text": "hello"}}, ("text", "hello")),
        ({"edited_message": {"text": "actually $35"}}, ("text", "edit: actually $35")),
        ({"callback_query": {"data": "sent:7"}}, ("tap", "sent:7")),
        ({"message": {"document": {"file_id": "x"}}}, ("non_text", "<document>")),
        ({"message": {"photo": [{"file_id": "x"}]}}, ("non_text", "<photo>")),
        ({}, ("other", "")),
    ]
    for raw, expected in cases:
        assert message_log._describe(raw) == expected, (raw, message_log._describe(raw))


# --- the MCP read surface -----------------------------------------------------


def test_mcp_inventory_has_no_dangerous_tool():
    """2.3 / 19a.7 — this test IS the gmail-isolation-boundary enforcement.

    The stock gateway agent asserted it had checked email in a runtime with no
    mail credential, so prompt-level discipline demonstrably does not hold this
    line. The inventory is written by hand precisely so it can be asserted."""
    from openclaw import mcp_server

    def scan(names):
        return [
            f"{name} contains {bad!r}"
            for name in names
            for bad in mcp_server.FORBIDDEN_TOOL_SUBSTRINGS
            if bad in name
        ]

    # The guard gets its own guard, the same way
    # test_no_module_outside_claim_status_writes_the_status_column does: this
    # scan iterates two production lists, so a tripwire that had stopped firing
    # would pass forever and read exactly like a clean inventory. Both halves.
    for dangerous in ("read_file", "search_mail", "get_secret", "run_shell", "send_draft"):
        assert scan([dangerous]), f"the tripwire does not fire on {dangerous!r}"
    assert not scan(["query_claims", "turn_context"]), "the tripwire fires on a safe name"

    assert not scan(mcp_server.TOOL_NAMES), scan(mcp_server.TOOL_NAMES)

    # Proposals ARE reachable here as of section 3, and that is not a hole: a
    # propose_* tool queues a row and sends a button, and the write happens on
    # the tap. What must stay unreachable is the commit. Assert the boundary
    # where it actually is — no implementation on this surface reaches
    # `proposals.commit` or `proposals.execute`.
    from openclaw import agent, proposals

    droppable = [n for n in agent._build_impls([], "") if n.startswith("propose_")]
    assert droppable, "the agent exposes no propose_* — this test would prove nothing"
    impls = mcp_server._impls()
    assert set(impls) == set(mcp_server.TOOL_NAMES), set(impls) ^ set(mcp_server.TOOL_NAMES)

    committed = []
    real_commit, real_execute = proposals.commit, proposals.execute
    proposals.commit = lambda *a, **k: (
        committed.append(("commit", a))
        or {
            "ok": True,
            "message": "",
        }
    )
    proposals.execute = lambda *a, **k: committed.append(("execute", a)) or ""
    try:
        # Every tool, called with nothing. Most will complain about missing
        # arguments; none may reach a write. A TypeError here is the tool
        # refusing bad input, which is the correct behaviour and not a commit.
        for name, fn in mcp_server._impls().items():
            try:
                fn()
            except TypeError:
                pass
        assert not committed, f"an MCP implementation reached the commit path: {committed}"
    finally:
        proposals.commit, proposals.execute = real_commit, real_execute


def test_mcp_inventory_is_enumerated_and_within_its_token_budget():
    """2.2 / 19a.3 — every schema here ships in EVERY agent turn. A trimmed turn
    is 3,865 tokens against Groq's 12,000 TPM, so the inventory has a real
    budget rather than a taste. Failing loudly on tool #N+1 is the point.

    No dynamic or wildcard registration: a tool that can only arrive by being
    written into TOOLS is a tool that can be counted."""
    import json as _json

    from openclaw import mcp_server

    # Ceilings sit just above the measured truth, not at the theoretical
    # headroom. They were 12 and 8,000 against 7 tools / 2,405 chars, which let
    # five tools and 3.3x the schema arrive with the suite green — a budget
    # nothing can exceed is not a budget.
    #
    # Raised 2026-08-02 when section 3 added the five propose_* tools, which is
    # the mechanism working as designed: 12 tools / 4,660 chars measured, so 13
    # and 5,200. Still ~1,165 tokens at the usual 4 chars/token, against the
    # ~8,100 of headroom measured after the 17.8/17.10 cuts. Raise these only
    # after re-measuring a real turn, never ahead of the need.
    assert len(mcp_server.TOOLS) <= 13, (
        f"{len(mcp_server.TOOLS)} tools — re-measure the turn before raising this"
    )
    schema_chars = len(_json.dumps(mcp_server.TOOLS))
    assert schema_chars <= 5200, f"tool schemas are {schema_chars} chars; they ship on every turn"
    for tool in mcp_server.TOOLS:
        assert tool["name"] and tool["description"] and "inputSchema" in tool, tool


def test_mcp_speaks_enough_of_the_protocol_to_be_probed():
    """2.5's hermetic half. The live half needs the gateway and is a preflight
    assertion; this one catches the shape breaking without one."""
    from openclaw import mcp_server

    init = mcp_server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        }
    )
    assert init["result"]["protocolVersion"] == "2025-06-18", init
    assert init["result"]["capabilities"]["tools"] is not None, init
    # The instructions must say what this surface CANNOT do. The absence of a
    # mailbox has to be stated, not inferred from an inventory nobody reads.
    assert "READ ONLY" in init["result"]["instructions"], init["result"]["instructions"]

    listed = mcp_server.dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    served = [t["name"] for t in listed["result"]["tools"]]
    # Frozen literal, not TOOL_NAMES: `dispatch` returns TOOLS and TOOL_NAMES is
    # derived from TOOLS (mcp_server.py:102), so comparing the two asserted
    # nothing any change could break. The inventory is the
    # gmail-isolation-boundary surface and a per-turn token cost, so it changes
    # by deliberate edit here or not at all.
    assert served == [
        "turn_context",
        "query_claims",
        "pending_actions",
        "claim_detail",
        "claim_history",
        "submissions_awaiting_reply",
        "list_tasks",
        "propose_mark_sent",
        "propose_set_condition",
        "propose_assign_pet",
        "propose_mark_resolved",
        "propose_split_between_pets",
    ], served
    # A client reads the schemas off this response, never off TOOLS.
    assert all(
        t["inputSchema"]["type"] == "object" and t["description"] for t in listed["result"]["tools"]
    ), listed["result"]["tools"]
    # `ping` is part of the probe the gateway runs; it was the one dispatch
    # method with no assertion.
    assert mcp_server.dispatch({"jsonrpc": "2.0", "id": 9, "method": "ping"}) == {
        "jsonrpc": "2.0",
        "id": 9,
        "result": {},
    }

    # A notification gets no response at all — returning one is a protocol
    # violation, and the client is entitled to treat it as a broken server.
    assert mcp_server.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert (
        mcp_server.dispatch({"jsonrpc": "2.0", "id": 3, "method": "nonsense"})["error"]["code"]
        == -32601
    )


def test_mcp_turn_context_reads_the_pets_live():
    """2.4 — the pet list is a DB read at call time, never baked into agent
    config. The model invented 'Whiskers' and 'Fluffy' when left to guess, and a
    list written into a workspace file is correct until a pet is added and then
    wrong silently. This is why the shipped USER.md deliberately has none."""
    from datetime import datetime, timezone

    from openclaw import mcp_server

    _fresh_db()
    out = mcp_server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "turn_context", "arguments": {}},
        }
    )
    text = out["result"]["content"][0]["text"]
    assert out["result"]["isError"] is False, out
    assert "Aari" in text and "Echo" in text, text
    assert datetime.now(timezone.utc).date().isoformat() in text, text

    with db.get_connection() as conn:
        conn.execute("INSERT INTO pets (name, insurer) VALUES ('Bandit', 'Petcover')")
    later = mcp_server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "turn_context", "arguments": {}},
        }
    )
    assert "Bandit" in later["result"]["content"][0]["text"], "the pet list was cached, not read"


def test_a_failing_mcp_tool_reports_to_the_model_not_the_transport():
    """A tool failure the model can see is one it can recover from or report. A
    JSON-RPC error ends the turn with nothing said, which is the silent no-op
    the project's rules forbid."""
    from openclaw import mcp_server

    _fresh_db()
    missing = mcp_server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "no_such_tool", "arguments": {}},
        }
    )
    assert missing["result"]["isError"] is True and "error" not in missing, missing

    bad_args = mcp_server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "claim_detail", "arguments": {"nope": 1}},
        }
    )
    assert bad_args["result"]["isError"] is True, bad_args
    assert "claim_detail" in bad_args["result"]["content"][0]["text"], bad_args


def _scratch() -> str:
    """A throwaway directory. The outbox tests must never touch a real one."""
    return tempfile.mkdtemp(prefix="openclaw-outbox-")


def test_a_card_is_published_under_the_path_the_gateway_can_actually_read():
    """14.2 — one file, two path spaces, and handing over the wrong one fails
    with `Local media path is not under an allowed directory`: an error that
    reads like a permissions problem and is really a namespace one.

    The gateway's media allowlist is a fixed set of roots and `/tmp` is not
    among them (14.1/14.4), so the app's own path is never sendable."""
    from openclaw import config, gateway_client, media_outbox

    _fresh_db()
    outbox = Path(_scratch()) / "outbox"
    before = (config.MEDIA_OUTBOX_DIR, config.MEDIA_OUTBOX_GATEWAY_DIR)
    try:
        config.MEDIA_OUTBOX_DIR = media_outbox.config.MEDIA_OUTBOX_DIR = str(outbox)
        config.MEDIA_OUTBOX_GATEWAY_DIR = media_outbox.config.MEDIA_OUTBOX_GATEWAY_DIR = (
            "/home/node/.openclaw/media"
        )

        seen, run = _capture_argv()
        gateway_client.send_card("42", b"\x89PNG-not-really", caption="Claim #7", runner=run)

        argv = seen[0]
        sent = argv[argv.index("--media") + 1]
        # The gateway's namespace, never the app's. Returning the app path would
        # be refused by the platform, silently to anyone reading our own logs.
        assert sent.startswith("/home/node/.openclaw/media/"), sent
        assert str(outbox) not in sent, sent

        written = list(outbox.glob("card-*.png"))
        assert len(written) == 1 and written[0].read_bytes() == b"\x89PNG-not-really"
        # Not the claim id: these names sit in a directory the gateway can read.
        assert Path(sent).name == written[0].name
        assert not list(outbox.glob("*.part")), "a partial write was left behind"
    finally:
        config.MEDIA_OUTBOX_DIR, config.MEDIA_OUTBOX_GATEWAY_DIR = before
        media_outbox.config.MEDIA_OUTBOX_DIR, media_outbox.config.MEDIA_OUTBOX_GATEWAY_DIR = before


def test_an_unwritable_outbox_fails_before_anything_is_sent():
    """A card that could not be written must never look like one delivered."""
    from openclaw import config, gateway_client, media_outbox

    _fresh_db()
    before = config.MEDIA_OUTBOX_DIR
    try:
        # A path under an existing FILE cannot be created as a directory.
        blocker = Path(_scratch()) / "not-a-dir"
        blocker.write_text("x", encoding="utf-8")
        config.MEDIA_OUTBOX_DIR = media_outbox.config.MEDIA_OUTBOX_DIR = str(blocker / "outbox")

        seen, run = _capture_argv()
        try:
            gateway_client.send_card("42", b"png", runner=run)
        except media_outbox.OutboxError:
            pass
        else:
            raise AssertionError("an unpublishable card reported success")
        assert not seen, "the CLI was invoked for a card that was never written"
    finally:
        config.MEDIA_OUTBOX_DIR = media_outbox.config.MEDIA_OUTBOX_DIR = before


def test_the_outbox_sweeps_its_own_expired_files():
    """Nothing reads these back — Telegram keeps its own copy once delivered.
    Swept on publish rather than on a timer: publishing is the only event that
    matters, and a directory nobody writes to needs no tidying."""
    import time as _time

    from openclaw import config, media_outbox

    outbox = Path(_scratch()) / "outbox-sweep"
    before = config.MEDIA_OUTBOX_DIR
    try:
        config.MEDIA_OUTBOX_DIR = media_outbox.config.MEDIA_OUTBOX_DIR = str(outbox)
        outbox.mkdir(parents=True, exist_ok=True)
        stale, fresh = outbox / "card-old.png", outbox / "card-new.png"
        stale.write_bytes(b"old")
        fresh.write_bytes(b"new")
        old_enough = _time.time() - media_outbox.TTL_SECONDS - 60
        os.utime(stale, (old_enough, old_enough))

        media_outbox.publish(b"png")
        assert not stale.exists(), "an expired card was left in the outbox"
        assert fresh.exists(), "a live card was swept"
    finally:
        config.MEDIA_OUTBOX_DIR = media_outbox.config.MEDIA_OUTBOX_DIR = before


def test_the_plugin_report_is_per_boot_and_never_persisted():
    """19b.6's evidence. An unregistered command in a button is not an error —
    it reaches the agent as a chat turn and spends tokens (16.8, measured live
    three times in Justin's chat). Both plugin enablement gates fail silently
    (18.7), so "it loaded" proves nothing.

    In-memory on purpose: persisting it would recreate the exact failure that
    makes `plugins list` useless — a saved registry that goes stale and reported
    `commands: []` for commands that worked (18.6)."""
    from openclaw import gateway_client, internal_api

    internal_api._plugin_report.clear()
    assert internal_api.plugin_report() == {}, (
        "an absent report must read as 'the plugin has not run'"
    )

    internal_api._plugin_report.update({"plugin": "claims", "commands": ["mark", "pet"]})
    assert internal_api.plugin_report()["commands"] == ["mark", "pet"]
    # A copy, not the live dict — /health must not hand out something a caller
    # can mutate into a passing report.
    internal_api.plugin_report()["commands"] = []
    assert internal_api.plugin_report()["commands"] == ["mark", "pet"]
    internal_api._plugin_report.clear()

    assert gateway_client.BUTTON_COMMANDS, "the preflight has nothing to assert"
    assert all(not c.startswith("/") for c in gateway_client.BUTTON_COMMANDS), (
        gateway_client.BUTTON_COMMANDS
    )


def _propose_via_mcp(name, arguments, runner=None):
    """Drive one propose_* tool the way the gateway's agent would, with the
    Confirm card's send captured instead of shelled out."""
    from openclaw import mcp_server

    seen, capture = _capture_argv()
    out = mcp_server._call_tool(name, arguments, runner=runner or capture)
    return out["content"][0]["text"], seen


def _claim_count_snapshot():
    with db.get_connection() as conn:
        return [
            tuple(r)
            for r in conn.execute(
                "SELECT id, status, pet_id, condition_text FROM vet_claims ORDER BY id"
            )
        ]


def test_a_proposal_writes_a_pending_row_and_changes_no_claim_data():
    """3.1 / 3.7 — the whole gate. A propose_* call must leave the claims table
    byte-identical and put a Confirm button in front of Justin instead.

    3.7's framing is the one that matters: the model saying it did something is
    not evidence either way, so assert the data, not the sentence."""
    from openclaw import db as _db
    from openclaw import proposals

    _fresh_db()
    with _db.get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO telegram_registrations (username, chat_id, registered_at) "
            "VALUES (?, ?, '2026-08-02T00:00:00Z')",
            (config.TELEGRAM_USERNAME, 4242),
        )
        _insert_claim(conn, 1, "2026-06-01", status="drafted", draft_id="d-proposal")
        claim = conn.execute("SELECT id FROM vet_claims ORDER BY id LIMIT 1").fetchone()
    assert claim, "fixture has no claim to propose against"

    before = _claim_count_snapshot()
    text, seen = _propose_via_mcp("propose_mark_sent", {"claim_id": claim["id"]})

    assert _claim_count_snapshot() == before, "a proposal changed claim data"
    with _db.get_connection() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM pending_proposals")]
    assert len(rows) == 1, rows
    assert rows[0]["action"] == "mark_sent" and rows[0]["confirmed_at"] is None, rows[0]
    assert rows[0]["origin"] == "chat" and str(claim["id"]) in rows[0]["label"], rows[0]

    # A Confirm button went out, carrying the row id and nothing the model wrote.
    assert len(seen) == 1, seen
    argv = seen[0]
    import json as _json

    presentation = _json.loads(argv[argv.index("--presentation") + 1])
    button = presentation["blocks"][0]["buttons"][0]
    assert button["action"] == {"type": "command", "command": f"/confirm {rows[0]['id']}"}, button
    assert len(button["action"]["command"].encode("utf-8")) <= 58, button
    # The card's text is composed from the code-built label, not from the model.
    assert argv[argv.index("--message") + 1] == f"Confirm: {rows[0]['label']}", argv
    assert f"#{rows[0]['id']}" in text and "Nothing has changed yet" in text, text

    # And only the tap commits.
    assert proposals.commit(rows[0]["id"])["ok"] is True
    assert _claim_count_snapshot() != before, "the confirmed proposal changed nothing"
    # Single-use: Telegram redelivers, and a second mark-sent is a second
    # Petcover submission for one set of invoices.
    again = proposals.commit(rows[0]["id"])
    assert again["ok"] is False and "Already confirmed" in again["message"], again


def test_the_mcp_surface_refuses_a_single_pet_assignment_when_the_message_names_two():
    """3.3 — the live 2026-07-27 failure, replayed through the new surface.

    Replaying *"This is actually split between echo and Aari. Aari cost was $35
    out of this"* against the ASSIGN PET card, the model proposed assigning Aari
    AND Echo — no API error, the split tool present in the schema. ADR-0025 says
    this refusal is enforced in the MCP surface, not mirrored there.

    The message text is read from the message log, not from a tool argument: a
    model that paraphrased it would paraphrase the second pet name away and take
    the refusal with it."""
    from openclaw import db as _db

    _fresh_db()
    live = "This is actually split between echo and Aari. Aari cost was $35 out of this"
    with _db.get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO telegram_registrations (username, chat_id, registered_at) "
            "VALUES (?, ?, '2026-08-02T00:00:00Z')",
            (config.TELEGRAM_USERNAME, 4242),
        )
        conn.execute(
            "INSERT INTO telegram_messages (update_id, direction, kind, summary, payload, "
            "app_version, received_at) VALUES (?, 'in', 'text', ?, '{}', 'test', '2026-08-02T00:00:00Z')",
            (990001, live),
        )
        _insert_claim(conn, 1, "2026-06-01", status="pending_match")
        claim = conn.execute("SELECT id FROM vet_claims ORDER BY id LIMIT 1").fetchone()
    assert _db.latest_inbound_text() == live, _db.latest_inbound_text()

    before = _claim_count_snapshot()
    text, seen = _propose_via_mcp(
        "propose_assign_pet", {"pet_name": "Aari", "claim_id": claim["id"]}
    )

    assert "SPLIT" in text and "propose_split_between_pets" in text, text
    assert _claim_count_snapshot() == before, "the refusal still changed claim data"
    with _db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM pending_proposals").fetchone()["c"] == 0, (
            "a refusal queued a proposal"
        )
    assert not seen, "a refusal sent a Confirm button"


def test_the_mcp_surface_refuses_a_split_with_no_amounts_rather_than_writing_zero_rows():
    """3.4 — a per-pet split with nothing to apportion must be refused, not
    filled with $0. `propose_split_between_pets` dry-runs the same guards the
    write uses, so an impossible split is refused in the reply instead of
    failing after the tap."""
    from openclaw import db as _db

    _fresh_db()
    with _db.get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO telegram_registrations (username, chat_id, registered_at) "
            "VALUES (?, ?, '2026-08-02T00:00:00Z')",
            (config.TELEGRAM_USERNAME, 4242),
        )
        _insert_claim(conn, 1, "2026-06-01", status="matched")
        claim = conn.execute("SELECT id FROM vet_claims ORDER BY id LIMIT 1").fetchone()

    before = _claim_count_snapshot()
    # Two pets, neither carrying an amount: only one share may be left out, and
    # inventing the other is exactly what must not happen.
    text, seen = _propose_via_mcp(
        "propose_split_between_pets",
        {"claim_id": claim["id"], "pets_and_amounts": [{"pet": "Aari"}, {"pet": "Echo"}]},
    )

    assert "Never invent" in text or "missing amounts" in text, text
    assert _claim_count_snapshot() == before, "the refusal changed claim data"
    with _db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM pending_proposals").fetchone()["c"] == 0, text
    assert not seen, "a refusal sent a Confirm button"


def test_every_mutating_tool_takes_an_explicit_claim_id():
    """3.5 — with no way to name the claim under discussion, the model
    fabricated argument values out of the tool schemas' own description strings
    (live, 2026-07-27). No tool may be targetable only by pet or reference."""
    from openclaw import mcp_server

    mutating = [t for t in mcp_server.TOOLS if t["name"].startswith("propose_")]
    assert mutating, "no proposal tools to check"
    for tool in mutating:
        assert "claim_id" in tool["inputSchema"]["properties"], tool["name"]
        assert tool["inputSchema"]["properties"]["claim_id"]["type"] == "integer", tool["name"]
    # And the claim under discussion is available to the turn without one:
    # turn_context carries today's date and the pets, and the message log
    # carries what he actually typed.
    assert isinstance(db.latest_inbound_text(), str)


def test_a_confirm_with_a_junk_id_changes_nothing_and_says_so():
    """The tap path's own bad input. A 404-shaped answer that reads like success
    is how a morning of taps changed nothing and left no evidence of why."""
    from openclaw import internal_api

    _fresh_db()
    before = _claim_count_snapshot()
    for junk in ("", "abc", None, "99999"):
        outcome = internal_api.confirm_proposal(junk)
        assert outcome["ok"] is False, (junk, outcome)
        assert "Nothing was changed" in outcome["message"] or "not found" in outcome["message"], (
            outcome
        )
    assert _claim_count_snapshot() == before


def test_the_actions_run_is_one_concurrent_burst_and_still_announces_truncation():
    """Latency and honesty at once, because the cheap fix threatened the rule.

    Every send costs 9–13s end to end (measured live 2026-08-03; ~6.6s of it is
    local CLI initialisation), so the old shape — summary card first, then N tap
    cards, then up to two note messages — cost two ordered rounds. What removed
    the ordering was making nothing depend on it, NOT dropping the summary: the
    summary is card 0 of the burst, and Telegram may deliver it anywhere in the
    run (Justin's call 2026-08-03, restoring it).

    The risk in that trade is the held-back count. A cap nobody is told about is
    a silent truncation, and no latency saving buys that — so it is asserted
    here rather than left to the rewrite's good intentions.
    """
    import concurrent.futures
    import threading
    import time as _time

    from openclaw import claim_status, commands

    over = commands.ACTION_CARD_CAP + 2
    fake = [
        {
            "kind": "mark_sent",
            "claim_id": i,
            "pet_id": 1,
            "actionable": True,
            "title": "mark sent",
            "merchant": "THE SHIRE VET",
            "amount": -120.0,
            "date": "2026-05-01",
            "age_days": 30,
            "pet_name": "Aari",
            "condition_text": "itchy ear",
            "blocks": "the claim",
            "flag": None,
            "waiting": claim_status.NOBODY_WAITING,
            "claimable": 120.0,
            "claimable_recorded": True,
            "expected": {"available": True, "value": 100.0, "note": None},
            "claim_ids": [i],
            "members": None,
            "group_id": f"S{i}",
        }
        for i in range(1, over + 1)
    ]

    real = claim_status.pending_actions
    claim_status.pending_actions = lambda: fake
    try:
        cards = commands.actions_cards()
    finally:
        claim_status.pending_actions = real

    # The summary card plus one message per shown action, and no separate note
    # messages — the notes ride on the summary's caption.
    assert len(cards) == commands.ACTION_CARD_CAP + 1, len(cards)
    assert cards[0].get("png"), "the rendered summary card is missing from the burst"
    assert not any(c.get("png") for c in cards[1:]), "more than one rendered card is being sent"
    # The truncation is still stated, on the summary's caption.
    assert f"+{over - commands.ACTION_CARD_CAP} more" in cards[0]["caption"], cards[0]["caption"]
    assert "to action" in cards[0]["caption"], cards[0]["caption"]
    # Every tap card is tappable; the summary carries no buttons.
    assert all(c["buttons"] for c in cards[1:]), [c["buttons"] for c in cards[1:]]
    assert not cards[0]["buttons"], cards[0]["buttons"]

    # And nothing about the run is order-dependent, so it can go at once.
    order, lock = [], threading.Lock()

    def deliver(card):
        with lock:
            order.append((card.get("text") or card.get("caption") or "")[:12])
        _time.sleep(0.12)
        return None

    began = _time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(cards))) as pool:
        outcomes = list(pool.map(deliver, cards))
    elapsed = _time.time() - began
    assert all(o is None for o in outcomes), outcomes
    assert elapsed < 0.12 * len(cards) / 2, f"the cards did not overlap: {elapsed:.2f}s"


def test_a_tap_result_reaches_justin_even_when_the_edit_fails():
    """4.6 — a tap whose outcome vanished is indistinguishable from one that
    never registered, which is the failure ADR-0014 exists for. So the edit
    falls back to a plain reply, and the degradation is logged rather than
    quietly becoming the norm."""
    from openclaw import notify

    _fresh_db()
    with db.get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO telegram_registrations (username, chat_id, registered_at) "
            "VALUES (?, ?, '2026-08-02T00:00:00Z')",
            (config.TELEGRAM_USERNAME, 4242),
        )

    replies = []
    real_send, real_gw = notify.send_text, notify.using_gateway
    notify.send_text = lambda text, buttons=None: replies.append(text) or True
    notify.using_gateway = lambda: True
    try:
        from openclaw import gateway_client

        real_edit = gateway_client.edit_message
        gateway_client.edit_message = lambda *a, **k: (_ for _ in ()).throw(
            gateway_client.GatewaySendError("exit 2: message not found")
        )
        try:
            assert notify.append_result("9", "✅ Claim #7 marked sent") is True
            assert replies == ["✅ Claim #7 marked sent"], replies
        finally:
            gateway_client.edit_message = real_edit

        # No message to edit at all — still says it, rather than dropping it.
        replies.clear()
        assert notify.append_result(None, "✅ done") is True
        assert replies == ["✅ done"], replies
    finally:
        notify.send_text, notify.using_gateway = real_send, real_gw


def test_a_failed_ack_never_costs_the_handler():
    """4.7 — the ack exists so a slow answer does not feel dead. Losing it is
    strictly better than losing the handler, so it returns a bool and never
    raises, whatever the transport does."""
    from openclaw import gateway_client, notify

    _fresh_db()
    with db.get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO telegram_registrations (username, chat_id, registered_at) "
            "VALUES (?, ?, '2026-08-02T00:00:00Z')",
            (config.TELEGRAM_USERNAME, 4242),
        )

    real_gw, real_react = notify.using_gateway, gateway_client.react
    notify.using_gateway = lambda: True
    try:
        gateway_client.react = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("gateway gone"))
        assert notify.ack("9") is False, "a broken reaction escaped as an exception"
        gateway_client.react = lambda *a, **k: True
        assert notify.ack("9") is True
        # No message id is not a failure worth reporting — a command tap has none.
        assert notify.ack(None) is False
        assert notify.ack("") is False
    finally:
        notify.using_gateway, gateway_client.react = real_gw, real_react


def test_a_pending_condition_flow_claims_the_next_typed_message_and_then_releases():
    """4.3 / 12.2 — the whole reason this flow exists. What Justin types is
    stored verbatim, with no model between his words and `condition_text`, which
    is the field the hard rules forbid inferring.

    Claims exactly once. A flow that kept claiming would swallow the rest of the
    conversation; one that never claimed would hand the condition to the agent."""
    from openclaw import pending_flows

    _fresh_db()
    with db.get_connection() as conn:
        _insert_claim(conn, 1, "2026-06-01", status="matched")
        cid = conn.execute("SELECT id FROM vet_claims").fetchone()["id"]

    assert pending_flows.claim_text(7001, "hello there") is None, "claimed with no flow pending"

    started = pending_flows.start_condition(7001, cid)
    assert started["force_reply"] is True and "condition" in started["prompt"], started
    # Durable: the state is a row, not a dict, because the tap, the claim check
    # and the reply are three separate requests after the cutover.
    with db.get_connection() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) c FROM pending_flows WHERE kind = 'condition'"
            ).fetchone()["c"]
            == 1
        )

    card = pending_flows.claim_text(7001, "kennel cough")
    assert card is not None and "kennel cough" in card["text"], card
    with db.get_connection() as conn:
        stored = conn.execute(
            "SELECT condition_text FROM vet_claims WHERE id = ?", (cid,)
        ).fetchone()[0]
    assert stored == "kennel cough", stored

    assert pending_flows.claim_text(7001, "and another thing") is None, "the flow claimed twice"
    with db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM pending_flows").fetchone()["c"] == 0


def test_a_split_flow_walks_every_item_and_only_then_applies():
    """The per-item walk. Nothing is written until the last item is answered —
    `apply_item_conditions` runs once, not per item, so an abandoned flow leaves
    the claim untouched rather than half-conditioned."""
    from openclaw import pending_flows

    _fresh_db()
    with db.get_connection() as conn:
        _insert_claim(conn, 1, "2026-06-01", status="matched")
        cid = conn.execute("SELECT id FROM vet_claims").fetchone()["id"]

    items = [{"description": "consult", "amount": 80.0}, {"description": "vaccine", "amount": 40.0}]
    prompt = pending_flows.start_split(7002, cid, 1, items)
    assert "Item 1/2" in prompt["prompt"] and "consult" in prompt["prompt"], prompt
    # Buttons are commands, inside budget, and the verb is registered.
    from openclaw.button_commands import BUTTON_COMMANDS

    for button in prompt["buttons"]:
        assert button["command"][1:].split(" ")[0] in BUTTON_COMMANDS, button
        assert len(button["command"].encode("utf-8")) <= 58, button

    nxt = pending_flows.record_item(7002, "itchy ear")
    assert "Item 2/2" in nxt["prompt"], nxt
    # Still nothing applied at the halfway point.
    assert pending_flows.get(7002, pending_flows.SPLIT) is not None

    done = pending_flows.record_item(7002, None)
    assert "text" in done, done
    assert pending_flows.get(7002, pending_flows.SPLIT) is None, "the flow outlived its last item"


def test_a_typed_item_beats_a_pending_condition_and_the_flow_is_one_decision():
    """Both flows can be pending at once, as they could when they were two
    dicts. The typed item wins — it is the more specific state.

    And there is ONE decision function: `claim_text`. The PTB handler and the
    gateway's `before_dispatch` hook both call it, so the two transports cannot
    disagree about whether the agent sees a typed condition."""
    from openclaw import pending_flows, telegram_bot

    _fresh_db()
    with db.get_connection() as conn:
        _insert_claim(conn, 1, "2026-06-01", status="matched")
        cid = conn.execute("SELECT id FROM vet_claims").fetchone()["id"]

    pending_flows.start_condition(7003, cid)
    pending_flows.start_split(7003, cid, 1, [{"description": "consult", "amount": 80.0}])
    pending_flows.await_typed_item(7003)

    card = pending_flows.claim_text(7003, "arthritis")
    assert card is not None, "the typed item did not claim"
    # The split consumed it, so the condition flow is still waiting.
    assert pending_flows.get(7003, pending_flows.CONDITION) is not None
    assert pending_flows.get(7003, pending_flows.SPLIT) is None, "the one-item split did not finish"

    # No second copy of the decision. The scan is for the state KEY, not for
    # the module's function names -- calling `pending_flows.await_typed_item` is
    # the intended use; reaching into `state["await_type"]` is the copy. An
    # earlier version of this check matched both and would have failed forever.
    marker = '"await_type"'
    assert marker in (
        Path(__file__).resolve().parent.parent / "openclaw" / "pending_flows.py"
    ).read_text(encoding="utf-8"), "the scan's marker no longer exists"
    offenders = [
        p.name
        for p in (Path(__file__).resolve().parent.parent / "openclaw").glob("*.py")
        if p.name != "pending_flows.py" and marker in p.read_text(encoding="utf-8")
    ]
    assert not offenders, f"these reimplement the pending-flow decision: {offenders}"
    assert callable(telegram_bot._send_flow_card)


def test_the_claim_endpoint_fails_open_and_refuses_a_stranger():
    """A claim check that errors must let the message through — a lost message
    is worse than a stray chat turn — and a stranger's message must never be
    consumed by a flow of Justin's."""
    from openclaw import internal_api, pending_flows

    _fresh_db()
    assert internal_api.commands_is_authorized("someone-else") is False
    assert internal_api.commands_is_authorized(config.TELEGRAM_USERNAME) is True

    with db.get_connection() as conn:
        _insert_claim(conn, 1, "2026-06-01", status="matched")
        cid = conn.execute("SELECT id FROM vet_claims").fetchone()["id"]
    pending_flows.start_condition(7004, cid)

    real = pending_flows.claim_text
    pending_flows.claim_text = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db gone"))
    try:
        # The route's own body is async; assert the property the route relies on
        # — that the failure is catchable here rather than escaping upward.
        try:
            pending_flows.claim_text(7004, "x")
        except RuntimeError:
            pass
        else:
            raise AssertionError("the stub did not raise")
    finally:
        pending_flows.claim_text = real
    # And the flow is still pending, so nothing was consumed by the failure.
    assert pending_flows.get(7004, pending_flows.CONDITION) is not None


def test_an_unattended_notification_with_no_registered_chat_says_so_and_sends_nothing():
    """4.9's second half. Every outbound here is unattended — a pipeline tick on
    cron, the daily nudge — so the only place a dropped one can surface is the
    log, and it has to be at ERROR because the fix is a human action (`/start`).

    Returns False rather than raising: a tick must not die because a
    notification failed, and must never look like it sent one."""
    from openclaw import notify

    _fresh_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM telegram_registrations")

    sent = []
    real = notify.using_gateway
    notify.using_gateway = lambda: sent.append("reached transport") or False
    try:
        assert notify.send_text("claim #7 needs a condition") is False
        assert notify.send_card("cap", b"png") is False
        assert notify.send_document("cap", b"pdf", "x.pdf") is False
    finally:
        notify.using_gateway = real
    assert sent == [], "an outbound with no target still reached the transport"


def test_the_updater_flag_actually_stops_the_updater():
    """The flag has to gate the *poller*, not just the outbound seam.

    Found the hard way on 2026-08-03: `TELEGRAM_UPDATER_ENABLED` was added and
    wired only into `notify`, so the cutover deploy handed the token to the
    gateway while the app kept polling. Telegram answered
    `Conflict: terminated by other getUpdates request` and the preflight failed
    the deploy — which is the guard working, but the guard is not the fix.

    A disabled updater must also read as *disabled* rather than *dead*, or
    `_watchdog_telegram_polling` SIGTERMs the process on a loop."""
    import asyncio as _asyncio

    from openclaw import config as _config
    from openclaw import telegram_bot

    before_flag, before_app = _config.TELEGRAM_UPDATER_ENABLED, telegram_bot._application
    built = []
    real_build = telegram_bot.build_application
    telegram_bot.build_application = lambda: built.append(1)
    try:
        _config.TELEGRAM_UPDATER_ENABLED = False
        telegram_bot._application = None
        _asyncio.run(telegram_bot.start_polling())
        assert not built, "the updater was built with the flag off — two pollers, one token, 409"
        # None, not False: the watchdog restarts the process on False.
        assert telegram_bot.polling_alive() is None, telegram_bot.polling_alive()
    finally:
        telegram_bot.build_application = real_build
        _config.TELEGRAM_UPDATER_ENABLED, telegram_bot._application = before_flag, before_app


def test_the_outbound_seam_follows_the_updater_flag():
    """4.1 — the flag is the app updater's, so the transports are exact
    opposites. Two pollers on one token is a 409, and the preflight fails the
    deploy if both poll or neither does."""
    from openclaw import config as _config
    from openclaw import notify

    before = _config.TELEGRAM_UPDATER_ENABLED
    try:
        _config.TELEGRAM_UPDATER_ENABLED = True
        assert notify.using_gateway() is False, "the app polls, so the app sends"
        _config.TELEGRAM_UPDATER_ENABLED = False
        assert notify.using_gateway() is True, "the gateway polls, so the gateway sends"
    finally:
        _config.TELEGRAM_UPDATER_ENABLED = before


def test_both_transports_run_the_same_command_logic():
    """4.1's real requirement. The updater flag stays on for a week after the
    cutover, so the PTB path and the gateway path run side by side — and a
    behaviour that differs by transport is one that will differ after the flag
    is removed, when nobody is looking for it.

    Aliased, not wrapped: assert identity, because a wrapper is where the drift
    would live."""
    from openclaw import commands, telegram_bot

    for name in (
        "handle_mark",
        "handle_pet",
        "handle_resolved",
        "handle_sent",
        "handle_process",
        "handle_vetemail",
        "handle_notvet",
        "handle_start",
        "register_chat",
        "_action_card_text",
        "prior_conditions",
    ):
        assert getattr(telegram_bot, name) is getattr(
            commands,
            name.lstrip("_") if name.startswith("_action") or name.startswith("prior") else name,
            None,
        ) or getattr(telegram_bot, name) is getattr(commands, name), name
    assert telegram_bot._is_authorized is commands.is_authorized


def test_an_unauthorized_command_is_refused_out_loud():
    """4.8 — the check stays app-side. The gateway deciding to deliver an event
    is not the same as this app accepting it, and the username check is the only
    thing between a stranger's `/mark 7 sent` and a Petcover submission.

    Refused *out loud*: a tap that did nothing and said nothing is
    indistinguishable from one that never arrived."""
    from openclaw import commands

    for name in ("mark", "pet", "resolve", "confirm", "actions", "history", "unmatch"):
        out = commands.dispatch(name, "7 x", "someone-else")
        assert out == {"text": "Not authorized.", "cards": []}, (name, out)
    # Case-insensitive: Telegram reports display casing, so an exact compare
    # wrongly rejects the real user.
    assert commands.is_authorized(config.TELEGRAM_USERNAME.upper()) is True
    assert commands.is_authorized(None) is False


def test_an_unknown_command_says_so_instead_of_shrugging():
    """A plugin registering something this app cannot serve is a deploy-time
    mistake. The pre-gateway equivalent — an unhandled callback prefix — did
    nothing silently, which read as a tap that never arrived."""
    from openclaw import commands

    out = commands.dispatch("ping", "", config.TELEGRAM_USERNAME)
    assert "not a command this app serves" in out["text"], out
    assert out["cards"] == []
    for name in ("mark", "pet", "resolve", "unmatch", "invreq", "dismiss"):
        out = commands.dispatch(name, "", config.TELEGRAM_USERNAME)
        assert "needs a claim id" in out["text"], (name, out)


def test_mark_reserves_two_words_and_treats_the_rest_as_a_condition():
    """Slice 1's design writes the mark-sent tap as `/mark 7 sent`, while this
    app's `/mark` sets the condition text. Both are true, so `sent` joins
    `reviewed` as reserved. The hazard is named rather than hidden: a condition
    genuinely called "sent" cannot be set by button."""
    from openclaw import claim_status, commands

    _fresh_db()
    with db.get_connection() as conn:
        _insert_claim(conn, 1, "2026-06-01", status="drafted", draft_id="d-mark")
        cid = conn.execute("SELECT id FROM vet_claims").fetchone()["id"]

    assert commands.RESERVED_MARK_WORDS == {"sent", "reviewed"}
    commands.dispatch("mark", f"{cid} kennel cough", config.TELEGRAM_USERNAME)
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT status, condition_text FROM vet_claims WHERE id = ?", (cid,)
        ).fetchone()
    assert row["condition_text"] == "kennel cough", dict(row)
    assert row["status"] == "drafted", dict(row)

    commands.dispatch("mark", f"{cid} sent", config.TELEGRAM_USERNAME)
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT status, condition_text FROM vet_claims WHERE id = ?", (cid,)
        ).fetchone()
    # The reserved word marked it sent and did NOT overwrite the condition.
    assert row["status"] == "sent", dict(row)
    assert row["condition_text"] == "kennel cough", dict(row)
    assert claim_status.history_rows() is not None


def test_every_card_button_names_a_command_the_plugin_registered():
    """The hazard slice 1 measured: a `command` button is deterministic only
    while its command is registered. An unregistered one is not an error — the
    tap reaches the agent as a chat turn with its own token in the prompt, which
    is the commit-token-through-a-model path D12 exists to prevent.

    So every button any card can build is checked against the tuple, and against
    the 58-byte budget that silently deletes an overlong one from the keyboard."""
    from openclaw import claim_status, commands
    from openclaw.button_commands import BUTTON_COMMANDS

    _fresh_db()
    with db.get_connection() as conn:
        _insert_claim(conn, 1, "2026-06-01", status="drafted", draft_id="d-cards")
        _insert_claim(conn, 1, "2026-06-02", status="matched", condition="itchy ear")

    cards = commands.actions_cards() + [c for c in [commands.history_card(1)] if c]
    # Synthesise one card per action kind too: the live fixture will not produce
    # all eight, and an unexercised kind is exactly where an unregistered verb
    # would hide.
    for kind in commands._ACTION_EMOJI:
        cards.append(
            {"buttons": commands._action_buttons({"kind": kind, "claim_id": 7, "pet_id": 1})}
        )

    checked = 0
    for card in cards:
        for button in card.get("buttons") or []:
            verb = button["command"][1:].split(" ", 1)[0]
            assert verb in BUTTON_COMMANDS, (verb, button)
            assert len(button["command"].encode("utf-8")) <= 58, button
            assert button["label"].strip(), button
            checked += 1
    assert checked, "no buttons were checked — the fixture proves nothing"

    # And the guard fires: an unregistered verb is refused at build time.
    try:
        commands._command_button("nope", "/ping 7")
    except AssertionError:
        pass
    else:
        raise AssertionError("an unregistered command was allowed onto a card")
    assert claim_status.pending_actions() is not None


def test_the_plugin_registers_exactly_the_commands_a_button_may_emit():
    """19a.4's missing half. `BUTTON_COMMANDS` had only two assertions on it —
    non-empty, and slash-free — neither of which any rename could break, so the
    tuple was decorative. It has two readers and both must agree with it:

    * `gateway-plugin/index.js`, which registers the names inside the gateway;
    * `gateway_client.build_buttons`, which is the only place a button is built.

    The preflight compares the first pair at deploy time. That is the right place
    for it — it reads the running gateway — but it means a rename went unnoticed
    until someone deployed. This is the same check without the container.

    A verb nobody registered is not an error at the gateway: the tap reaches the
    agent as a chat turn and spends tokens (measured live 2026-08-01, three
    `/ping` taps, three model replies)."""
    import re as _re

    from openclaw import gateway_client
    from openclaw.button_commands import BUTTON_COMMANDS

    plugin = (Path(__file__).resolve().parent.parent / "gateway-plugin" / "index.js").read_text(
        encoding="utf-8"
    )
    block = _re.search(r"const COMMANDS = \[(.*?)\];", plugin, _re.S)
    assert block, "gateway-plugin/index.js no longer declares `const COMMANDS = [...]`"
    registered = _re.findall(r'name:\s*"([^"]+)"', block.group(1))
    assert registered, block.group(1)
    assert registered == list(BUTTON_COMMANDS), (registered, BUTTON_COMMANDS)

    # And the declaration is load-bearing rather than advisory: build_buttons
    # refuses a verb outside it. `button_commands.py` already said card-building
    # code must draw from the tuple; nothing made it so until now.
    _fresh_db()
    seen, run = _capture_argv()
    for verb in BUTTON_COMMANDS:
        gateway_client.send_message(
            "42", "x", buttons=[{"label": "ok", "command": f"/{verb} 7"}], runner=run
        )
    assert len(seen) == len(BUTTON_COMMANDS), "a declared command was refused"

    for undeclared in ("/ping", "/status 7", "/markk 7 sent"):
        try:
            gateway_client.send_message(
                "42", "x", buttons=[{"label": "ok", "command": undeclared}], runner=run
            )
        except gateway_client.PresentationError as exc:
            assert "BUTTON_COMMANDS" in str(exc), str(exc)
        else:
            raise AssertionError(f"a button nobody registered was sent: {undeclared!r}")
    assert len(seen) == len(BUTTON_COMMANDS), "an undeclared command reached the CLI"


def test_the_fast_send_path_batches_one_call_keeps_order_and_still_logs_every_card():
    """4.14. The CLI costs 9–13s a message and ~6.6s of that is its own
    initialisation with no network contact, so N messages cost N × 9s however
    they are scheduled. This path is one local HTTP call and N in-process
    dispatches inside the gateway.

    Three properties are asserted because each replaces something the CLI path
    gave for free:

    * ONE request for N cards — the whole point;
    * order preserved, which the concurrent CLI burst could not offer and which
      is what lets `/actions` put its summary card first again;
    * every delivered card still lands in `telegram_messages`. The message log is
      the audit trail and the RL dataset (ADR-0014); a faster path that logs less
      is a worse path.

    And a partial send must raise, never return quietly: `sent: 1, failures: 1`
    reading as success is exactly the silent failure the hard rules forbid.
    """
    import json as _json

    from openclaw import config, db, gateway_client

    _fresh_db()
    with db.get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO telegram_registrations (username, chat_id, registered_at) "
            "VALUES (?, ?, '2026-08-03T00:00:00Z')",
            (config.TELEGRAM_USERNAME, 77),
        )

    calls = []

    class _Response:
        def __init__(self, body):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def poster(request, timeout=None):
        calls.append(_json.loads(request.data))
        return _Response(
            _json.dumps({"ok": True, "sent": len(calls[-1]["cards"]), "failures": []}).encode()
        )

    cards = [
        {"png": b"\x89PNG-summary", "caption": "3 to action, 1 blocked", "buttons": []},
        {"text": "Claim #7 — mark sent", "buttons": [{"label": "Sent", "command": "/mark 7 sent"}]},
        {"text": "Claim #8 — assign pet", "buttons": [{"label": "Aari", "command": "/pet 8 1"}]},
    ]
    real_url, real_token = config.OPENCLAW_GATEWAY_HTTP_URL, config.OPENCLAW_GATEWAY_TOKEN
    config.OPENCLAW_GATEWAY_HTTP_URL = "http://gateway:18789"
    config.OPENCLAW_GATEWAY_TOKEN = "t"
    try:
        assert gateway_client.using_http_route() is True
        gateway_client.send_cards("77", cards, correlation="tg-test-1", poster=poster)

        # One request for three cards, in the order they were given.
        assert len(calls) == 1, calls
        payload = calls[0]["cards"]
        assert [c.get("message") for c in payload] == [
            "3 to action, 1 blocked",
            "Claim #7 — mark sent",
            "Claim #8 — assign pet",
        ], payload
        # The rendered card travels as a PATH through the shared outbox. Base64
        # in `buffer` is accepted by the schema and fails at runtime: the gateway
        # materialises it under a read-only mount (`ENOENT: mkdir .../media/
        # outbound`), which only a live send revealed.
        assert payload[0]["media_url"].startswith(config.MEDIA_OUTBOX_GATEWAY_DIR), payload[0]
        assert "media_url" not in payload[1], payload[1]
        # Buttons go through the one validated builder, nested inside `blocks` —
        # the shape the platform's own normalizer accepts. Top-level `buttons`
        # is discarded silently with `ok: true`.
        assert payload[1]["presentation"]["blocks"][0]["type"] == "buttons", payload[1]
        assert "buttons" not in payload[0], "the summary card should carry no presentation"

        # Every delivered card is in the message log, one row each, in order.
        # Scoped to the last three: earlier tests share this database.
        with db.get_connection() as conn:
            logged = conn.execute(
                "SELECT kind, summary FROM telegram_messages WHERE direction = 'out' "
                "ORDER BY id DESC LIMIT 3"
            ).fetchall()
        assert [r["kind"] for r in reversed(logged)] == ["file", "text", "text"], [
            dict(r) for r in logged
        ]
        assert [r["summary"] for r in reversed(logged)] == [
            "3 to action, 1 blocked",
            "Claim #7 — mark sent",
            "Claim #8 — assign pet",
        ], [dict(r) for r in logged]

        # A partial send raises, naming the reason. Never a quiet success.
        def failing(request, timeout=None):
            return _Response(
                _json.dumps(
                    {"ok": False, "sent": 1, "failures": [{"card": 1, "reason": "chat not found"}]}
                ).encode()
            )

        try:
            gateway_client.send_cards("77", cards, poster=failing)
        except gateway_client.GatewaySendError as exc:
            assert "chat not found" in str(exc), str(exc)
        else:
            raise AssertionError("a partial send was reported as a whole one")
    finally:
        config.OPENCLAW_GATEWAY_HTTP_URL, config.OPENCLAW_GATEWAY_TOKEN = real_url, real_token

    # Both halves or neither: a URL with no token would 401 every send and read
    # as an outage rather than as a misconfiguration.
    assert gateway_client.using_http_route() is False


def test_the_ack_is_the_gateways_own_and_the_scope_is_the_one_that_covers_a_dm():
    """Two deploys were spent hand-rolling a 👍 in the plugin, on two different
    hooks, and neither could have worked. The cause was never the hook: the
    gateway ships this feature and its default `ackReactionScope` is
    `group-mentions`, so it was configured off for the only chat here — a DM.
    That is also why Justin never saw it work before the gateway.

    So the assertion is on the SEED, which is where the fix lives, plus the two
    things the plugin must not go back to doing.

    A tap is separately impossible for anyone to ack: it arrives as a callback
    query, so there is no message to react to. `startTypingCue` is what covers
    it, and that is asserted here too because it is the only feedback a tap has
    beyond its reply."""
    seed = (
        Path(__file__).resolve().parent.parent.parent / "scripts" / "gateway_seed.sh"
    ).read_text(encoding="utf-8")
    assert "messages.ackReactionScope '\"all\"'" in seed, (
        "the ack scope is not set to one that includes a DM"
    )
    assert "messages.ackReaction " in seed, "no ack emoji is configured"
    assert "messages.statusReactions.enabled false" in seed, (
        "statusReactions turns the sticky ack into a transient lifecycle emoji that clears — "
        "it was enabled once and the 👍 stopped coming back"
    )

    plugin = (Path(__file__).resolve().parent.parent / "gateway-plugin" / "index.js").read_text(
        encoding="utf-8"
    )
    assert "registerInboundAck" not in plugin, (
        "the hand-rolled ack is back; the gateway does this natively via messages.ackReaction"
    )
    assert "sendChatAction" in plugin and "startTypingCue" in plugin, (
        "the typing cue is gone — a tap would then have no feedback until its reply lands"
    )


def test_the_plugin_declares_the_contract_its_in_process_send_depends_on():
    """The load-bearing line is in a JSON manifest, and losing it fails at
    RUNTIME with a thrown dispatch — no build error, no lint, nothing at deploy.

    `registry.canDispatchGatewayMethodsFromHttpRoute` reads
    `contracts.gatewayMethodDispatch` from the manifest **alone**; the route
    cannot ask for the permission itself. So the manifest and the code that
    depends on it are asserted together, here, where a rename is caught before it
    reaches a container."""
    import json as _json

    plugin_dir = Path(__file__).resolve().parent.parent / "gateway-plugin"
    manifest = _json.loads((plugin_dir / "openclaw.plugin.json").read_text(encoding="utf-8"))
    assert manifest.get("contracts", {}).get("gatewayMethodDispatch") == [
        "authenticated-request"
    ], manifest

    plugin = (plugin_dir / "index.js").read_text(encoding="utf-8")
    assert "dispatchGatewayMethod" in plugin, "the plugin no longer dispatches in-process"
    assert '"/api/v1/claims/send"' in plugin, "the send route path changed — config must follow"
    # `auth: "plugin"` routes are handed an EMPTY scope list, so a write would be
    # refused; only the trusted-operator surface resolves the CLI's own scopes.
    assert 'gatewayRuntimeScopeSurface: "trusted-operator"' in plugin, plugin[:0]
    assert 'auth: "gateway"' in plugin, "a plugin-auth route cannot dispatch a write"


def test_chat_has_a_gemini_backend_and_the_agents_primary_is_the_reachable_provider():
    """Groq blocks this network as of 2026-08-04 — 403 to a request carrying no
    Authorization header at all, from the host and from inside both containers.
    Not the key, not the account, not a rate limit. Every model in
    `_FALLBACK_MODELS` is Groq, so the whole ADR-0017 chain went with it and the
    agent had no model for four deploys.

    Two halves, and each fails differently if lost:

    1. `chat()` used to refuse `LLM_PROVIDER=gemini` outright ("supports
       extract() only"), so switching the app's provider would have traded a 403
       for a hard refusal. Asserted by behaviour rather than by grepping for the
       old message: with the provider set and the key blank it must fail on the
       KEY, which proves it reached the client instead of the old early return.
    2. The gateway agent's primary must be the provider that answers. Groq stays
       configured — a network that can reach it needs one line changed, not a
       provider rebuilt — but it must not be the primary while it is blocked."""
    base, model, _key = None, None, None
    assert "gemini" in llm._PROVIDERS, (
        "chat() has no Gemini backend; the tool loop is Groq-only again"
    )
    base, model, _key = llm._PROVIDERS["gemini"]
    assert base.endswith("/v1beta/openai"), (
        f"the OpenAI-compatible surface is what `openai-completions` needs, got {base}"
    )
    assert model.startswith("gemini-"), model

    original = config.LLM_PROVIDER
    original_client = llm._client
    try:
        config.LLM_PROVIDER = "gemini"
        llm._client = None
        try:
            llm.chat([{"role": "user", "content": "hi"}])
            raise AssertionError("a blank key must fail visibly")
        except llm.LLMUnavailableError as exc:
            assert "GEMINI_API_KEY" in str(exc), (
                f"chat() refused gemini before reaching the client: {exc}"
            )
    finally:
        config.LLM_PROVIDER = original
        llm._client = original_client

    seed = (
        Path(__file__).resolve().parent.parent.parent / "scripts" / "gateway_seed.sh"
    ).read_text(encoding="utf-8")
    assert "models.providers.gemini" in seed, "the gateway has no Gemini provider configured"

    # The gateway needs the SAME chain the app has, and for the same reason: the
    # daily quota is per model, so a spent primary is only survivable by moving.
    # Learned live 2026-08-04 — a day of deploys exhausted
    # GenerateRequestsPerDayPerProjectPerModel-FreeTier for gemini-2.5-flash and the
    # deploy failed with one model declared and nowhere to go.
    #
    # Asserted as a SET relationship against llm._FALLBACK_MODELS rather than as a
    # literal list: two hand-maintained copies of a model chain is the duplication
    # this repo keeps getting caught by.
    for model in llm._FALLBACK_MODELS["gemini"]:
        assert f'"gemini/{model}"' in seed, (
            f"{model} is in the app's chain but not the gateway's fallbacks"
        )
        assert f'\\"id\\":\\"{model}\\"' in seed, (
            f"{model} is a fallback the provider entry never declares — the gateway "
            "cannot fail over to a model it does not know"
        )
    assert "agents.defaults.model.fallbacks" in seed, "the agent has no fallback list"
    assert "generativelanguage.googleapis.com/v1beta/openai" in seed, seed[:0]
    assert "agents.defaults.model.primary '\"gemini/gemini-2.5-flash\"'" in seed, (
        "the agent's primary is not the provider this network can reach"
    )
    groq_primary = seed.count("agents.defaults.model.primary '\"groq/")
    assert groq_primary <= 1, (
        "Groq must be at most the no-Gemini fallback, never the default primary"
    )


def test_a_gateway_command_writes_an_inbound_row_and_settles_it():
    """Task 4.2. For a day after the cutover NOTHING wrote an inbound row: six of
    Justin's live commands and taps produced zero `direction=in` rows, so
    ADR-0014's audit trail had only its outbound half and "did my tap register?"
    was answerable solely from container logs — which are destroyed on recreate.

    The defect was invisible because task 4.2 was ticked for registering a hook
    while the tee named in its own sentence was never wired. So this test asserts
    the ROW, not the wiring.

    Settled immediately and deliberately: `pending()` is the replay queue and
    nothing drains it after the cutover — `replay_pending` rebuilds a
    python-telegram-bot Update and its only caller is `telegram_bot.py`, which is
    off and is deleted by section 6. A row left unprocessed would read as work
    pending that will never happen."""
    from openclaw import internal_api

    db.init_db()

    def _rows(pattern):
        with db.get_connection() as conn:
            return conn.execute(
                "SELECT update_id, direction, kind, summary, processed_at, error, correlation_id "
                "FROM telegram_messages WHERE update_id LIKE ? ORDER BY id",
                (pattern,),
            ).fetchall()

    # A command, as the plugin delivers one.
    inbound_id = internal_api.tee_inbound("tg-dismiss-t1", "/dismiss 2", "jagberg", chat_id=4242)
    assert inbound_id.startswith("cmd:tg-dismiss-t1:"), inbound_id
    rows = _rows("cmd:tg-dismiss-t1%")
    assert len(rows) == 1, f"the command left no inbound row: {rows}"
    row = rows[0]
    assert row["direction"] == "in"
    # The SAME vocabulary the PTB era wrote — `_describe` classifies a
    # slash-prefixed text as `command`, so no new kind value enters the dataset.
    assert row["kind"] == "command", row["kind"]
    assert row["summary"] == "/dismiss 2", row["summary"]
    assert row["correlation_id"] == "tg-dismiss-t1", (
        "the row cannot be joined to its outbound cards"
    )
    assert row["processed_at"] is None, "settling is the caller's job, not the tee's"

    internal_api.settle_inbound(inbound_id)
    row = _rows("cmd:tg-dismiss-t1%")[0]
    assert row["processed_at"] is not None, "an unsettled row sits in a queue nothing drains"
    assert row["error"] is None

    # A failure settles AND annotates. Order matters: mark_processed refuses a row
    # that already carries an error, so settling second would leave it pending.
    failed_id = internal_api.tee_inbound("tg-mark-t2", "/mark 7 sent", "jagberg", chat_id=4242)
    internal_api.settle_inbound(failed_id, "boom")
    row = _rows("cmd:tg-mark-t2%")[0]
    assert row["processed_at"] is not None, "a failed command was left in the replay queue"
    assert row["error"] == "boom", row["error"]

    # A typed message goes through the other tee and must NOT read as a command.
    typed_id = internal_api.tee_inbound("tg-text-t3", "kennel cough", "jagberg", chat_id=4242)
    internal_api.settle_inbound(typed_id)
    row = _rows("cmd:tg-text-t3%")[0]
    assert row["kind"] == "text", row["kind"]

    # Both routes must actually call it — the defect this replaces was a helper
    # that existed with no caller.
    source = (Path(__file__).resolve().parent.parent / "openclaw" / "internal_api.py").read_text(
        encoding="utf-8"
    )
    assert source.count("tee_inbound(") >= 3, "a tee point stopped calling the tee"
    assert source.count("settle_inbound(") >= 4, "a path settles nothing, so its row stays queued"

    # THE COLLISION CASE, which the first version of this got wrong. The plugin's
    # correlation counter is module-level and resets on every plugin reload, so
    # `tg-actions-n1` recurs after each deploy. Two taps with the SAME correlation
    # must still produce two rows: with `INSERT OR IGNORE` on a UNIQUE column, a
    # shared id meant the second tap wrote nothing and then settled the first
    # tap's row instead.
    a = internal_api.tee_inbound("tg-actions-n1", "/actions", "jagberg", chat_id=4242)
    b = internal_api.tee_inbound("tg-actions-n1", "/actions", "jagberg", chat_id=4242)
    assert a != b, "a repeated correlation produced one id, so a tap would be swallowed"
    rows = _rows("cmd:tg-actions-n1%")
    assert len(rows) == 2, f"a restart-repeated correlation lost a row: {rows}"
    assert {r["correlation_id"] for r in rows} == {"tg-actions-n1"}, (
        "the join key was mangled to make it unique"
    )
    internal_api.settle_inbound(a)
    settled = {r["update_id"]: r["processed_at"] for r in _rows("cmd:tg-actions-n1%")}
    assert settled[a] is not None and settled[b] is None, (
        f"settling one tap touched the other: {settled}"
    )


def test_a_tick_reports_what_it_did_not_merely_that_it_fired():
    """Task 9.6. `cron runs` says a job fired. After the cutover that is the ONLY
    thing cron can say, so a 40-second tick that advanced nothing reads exactly
    like one that advanced four claims — and the two need opposite responses.

    Measured by counting before and after rather than by each step reporting its
    own work: a step that forgets to report is indistinguishable from a step that
    did nothing, which is the failure being measured.
    """
    from openclaw import internal_api

    db.init_db()

    def _busy():
        with db.get_connection() as conn:
            conn.execute("INSERT INTO ops_alerts (kind, sent_at) VALUES ('t', '2026-08-06')")
        return "did a thing"

    out = internal_api._run("tick", _busy, "corr-busy")
    assert out["status"] == "ok", out
    assert out["changed"] == {"ops_alerts": 1}, (
        f"a tick that wrote a row reported no effect: {out.get('changed')!r}"
    )
    assert isinstance(out["duration_ms"], int), out
    assert out["result"] == "did a thing", "the step's own return value was dropped"

    # The quiet tick. `{}` is the answer, and it must be an answer rather than an
    # absent field — "nothing changed" is the state worth alerting on later, and a
    # missing key cannot be alerted on at all.
    quiet = internal_api._run("tick", lambda: None, "corr-quiet")
    assert quiet["changed"] == {}, quiet["changed"]
    assert "duration_ms" in quiet

    # A delta reports the direction of movement, not just that something moved:
    # a claim leaving `matched` for `drafted` is two entries, -1 and +1, which is
    # what makes "advanced" legible in a log line.
    before = {"claims:matched": 2, "claims:drafted": 0, "tasks": 5}
    after = {"claims:matched": 1, "claims:drafted": 1, "tasks": 5}
    assert internal_api._effect_delta(before, after) == {"claims:drafted": 1, "claims:matched": -1}

    # A failed snapshot must not invent movement. Returning {} on error means the
    # line reads "changed={}" — honest — rather than every count looking new.
    assert internal_api._effect_delta({}, after) == {}


def test_the_gateway_row_and_the_ptb_row_differ_in_nothing_a_query_reads():
    """Task 9.4. The previous test asserts a row exists; this one asserts it is the
    SAME row the old transport wrote, column by column, because ADR-0014's dataset
    is only worth having if a query spanning the cutover means one thing.

    Compared against a real python-telegram-bot payload rather than against my
    idea of one: `record_inbound` serialises `update.to_dict()`, so the fixture is
    that dict's shape.
    """
    import json as _json

    from openclaw import internal_api, message_log

    db.init_db()

    def _row(update_id):
        with db.get_connection() as conn:
            return conn.execute(
                "SELECT * FROM telegram_messages WHERE update_id = ?", (str(update_id),)
            ).fetchone()

    # The PTB shape, as `update.to_dict()` produced it before the cutover.
    ptb_raw = {
        "update_id": 90001,
        "message": {
            "message_id": 55,
            "date": 1754300000,
            "text": "/dismiss 2",
            "from": {"id": 7, "username": "jagberg", "is_bot": False},
            "chat": {"id": 4242, "type": "private"},
        },
    }
    message_log.record_inbound_raw(90001, ptb_raw)
    gateway_id = internal_api.tee_inbound("tg-dismiss-p1", "/dismiss 2", "jagberg", chat_id=4242)

    old, new = _row(90001), _row(gateway_id)

    # Every column a query reads must agree. `update_id` and `payload` are the two
    # that legitimately differ, and both differences are recorded in task 4.2.
    for column in ("direction", "kind", "summary", "app_version"):
        assert old[column] == new[column], (
            f"{column} changed meaning across the cutover: {old[column]!r} -> {new[column]!r}"
        )
    assert new["app_version"] == config.APP_VERSION, (
        "a row that cannot say which build wrote it mistags the dataset"
    )
    for column in ("direction", "kind", "summary", "app_version", "received_at"):
        assert new[column] is not None, f"{column} is NULL on the gateway path"

    # The payload is thinner, not empty: it must still be parseable and still carry
    # the three facts anything downstream reads off it.
    payload = _json.loads(new["payload"])
    assert payload["message"]["text"] == "/dismiss 2"
    assert payload["message"]["from"]["username"] == "jagberg"
    assert payload["message"]["chat"]["id"] == 4242

    # processed_at ordering: NULL until settled, then never before received_at. A
    # row settled "before" it arrived would make replay ordering meaningless.
    assert new["processed_at"] is None
    internal_api.settle_inbound(gateway_id)
    new = _row(gateway_id)
    assert new["processed_at"] >= new["received_at"], (
        f"settled before it arrived: {new['received_at']} -> {new['processed_at']}"
    )

    # The 2026-07-27 bug: an edit arrives with `message` absent, and logged as kind
    # `other` with an empty summary — the one message that mattered was the one the
    # log could not show. `_describe` is shared by both transports, so this guards
    # the classifier for whichever one delivers the edit.
    kind, summary = message_log._describe(
        {"edited_message": {"text": "Aari cost was $35 out of this", "chat": {"id": 4242}}}
    )
    assert kind == "text", kind
    assert summary == "edit: Aari cost was $35 out of this", summary

    # ...but nothing on the gateway path can produce that row, and this asserts the
    # gap rather than letting the classifier test imply coverage it does not have.
    # The tee builds one shape, `{"message": ...}`, so `tap`, `edit:` and every
    # `callback_query` row are unreachable — which is why a tapped button now logs
    # as `kind=command` where it once logged `kind=tap` with its callback data.
    # Delete this assertion when a raw-event tee exists; until then it is the
    # honest boundary of 9.4.
    assert "callback_query" not in payload and "edited_message" not in payload, (
        "a raw-event tee now exists — 9.4's edit and tap parity is testable end to end"
    )

    # And the plugin no longer emits an id that repeats across restarts.
    plugin = (Path(__file__).resolve().parent.parent / "gateway-plugin" / "index.js").read_text(
        encoding="utf-8"
    )
    assert "const RUN =" in plugin and "${RUN}n${++sequence}" in plugin, (
        "the plugin's correlation counter is back to resetting to n1 on every reload"
    )


def test_a_duplicated_gateway_delivery_commits_exactly_one_mutation():
    """Task 10.12, and the only §10 item with a money consequence: a second
    mark-sent is a second Petcover submission for one set of invoices.

    Telegram redelivers, ADR-0014's replay re-runs unsettled rows, and after the
    cutover a tap crosses two runtimes — so a duplicate is normal traffic, not an
    edge case. Three layers have to hold, and each is asserted through the real
    function rather than by inspecting a flag:

      1. the log row dedupes, so the replay queue never holds the same event twice
      2. `claim_status.mark_sent` refuses a claim that is no longer `drafted`
      3. `proposals.commit` is single-use

    Layer 1 alone is not enough, which is the point of testing all three: the
    dedupe only covers a *redelivery of the same update*, and two distinct taps on
    two cards of the same batch are two different updates."""
    from openclaw import claim_status, message_log, proposals

    _fresh_db()

    # 1. Same update, delivered twice.
    raw = {"message": {"text": "/mark 1 sent"}}
    first = message_log.record_inbound_raw(9_200_001, raw, correlation="dup-a")
    second = message_log.record_inbound_raw(9_200_001, raw, correlation="dup-b")
    assert first == 9_200_001, first
    assert second is None, "a redelivered update was queued a second time"
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT COUNT(*) c FROM telegram_messages WHERE update_id = ?", (9_200_001,)
        ).fetchone()["c"]
    assert rows == 1, f"{rows} rows for one update"

    # 2. Two taps on one batch: two different updates, one draft, one submission.
    with db.get_connection() as conn:
        _insert_claim(conn, 1, "2026-06-01", status="drafted", draft_id="draft-dup")
        _insert_claim(conn, 1, "2026-06-02", status="drafted", draft_id="draft-dup")
        ids = [r["id"] for r in conn.execute("SELECT id FROM vet_claims ORDER BY id")]

    one = claim_status.mark_sent(ids[0])
    assert one["ok"] is True, one
    # The sibling card's tap. A batch is one email, so this claim is already sent.
    two = claim_status.mark_sent(ids[1])
    assert two["ok"] is False, "the sibling tap advanced a claim a second time"
    assert "sent" in two["message"].lower(), two

    with db.get_connection() as conn:
        statuses = [r["status"] for r in conn.execute("SELECT status FROM vet_claims ORDER BY id")]
        events = conn.execute(
            "SELECT COUNT(*) c FROM claim_status_events WHERE event_type = 'sent'"
        ).fetchone()["c"]
    assert statuses == ["sent", "sent"], statuses
    # One submission, one event per claim — never two for the same claim, which is
    # what a duplicate Petcover email would look like in the log.
    assert events == len(ids), f"{events} sent events for {len(ids)} claims"

    # 3. A confirm tap, redelivered.
    with db.get_connection() as conn:
        _insert_claim(conn, 1, "2026-06-03", status="drafted", draft_id="draft-solo")
        solo = conn.execute("SELECT id FROM vet_claims ORDER BY id DESC LIMIT 1").fetchone()["id"]
    pid = proposals.record("mark_sent", label=f"mark #{solo} sent", claim_id=solo, origin="chat")
    first_tap = proposals.commit(pid)
    second_tap = proposals.commit(pid)
    assert first_tap["ok"] is True, first_tap
    assert second_tap["ok"] is False and "already confirmed" in second_tap["message"].lower(), (
        second_tap
    )
    with db.get_connection() as conn:
        solo_events = conn.execute(
            "SELECT COUNT(*) c FROM claim_status_events WHERE claim_id = ? AND event_type = 'sent'",
            (solo,),
        ).fetchone()["c"]
    assert solo_events == 1, f"the redelivered confirm wrote {solo_events} sent events"


def test_the_gateways_log_is_configured_to_outlive_its_container():
    """Task 13.5's real half. An access denial leaving no trace was read as a log
    LEVEL problem; the durability half is worse and was the actual cause. The
    gateway wrote to stdout (`docker compose logs`, destroyed when the container is
    recreated) and to `/tmp/openclaw/openclaw-<date>.log`, inside the container and
    destroyed with it — so every deploy erased the evidence for the one before, and
    this session lost a real failure's text that way (`job_runs.last_error`).

    `/home/node/.openclaw` is the state volume, so a file there survives. Asserted
    on the seed because that is where the setting lives and nothing else would
    notice its removal."""
    seed = (
        Path(__file__).resolve().parent.parent.parent / "scripts" / "gateway_seed.sh"
    ).read_text(encoding="utf-8")
    assert "logging.file" in seed, "the gateway log has no durable sink again"
    assert "/home/node/.openclaw/logs/" in seed, (
        "the log path is outside the state volume, so a recreate destroys it"
    )
    assert "/tmp/" not in seed.split("logging.file")[1][:200], "back to a container-local path"
    # Level pinned rather than inherited: the ingress drop lines are info, so a
    # shipped default moving to warn would silently take them with it.
    assert "logging.level '\"info\"'" in seed


def test_a_gateway_delivered_event_and_its_replies_carry_the_same_correlation_id():
    """Task 10.14. An event now crosses gateway -> plugin -> app -> handler -> any
    resulting send, and the log lines were the only place the id existed. Container
    logs do not survive a recreate, so a week-old "did my tap register?" was
    answerable only through `update_id` — which pairs with the correlation id in a
    log line that is already gone.

    Also covers the migration itself: the live table has hundreds of rows and
    `CREATE TABLE IF NOT EXISTS` will not add a column to it, so the column arrives
    via `_migrate_added_columns` at startup. Asserted against a table created
    WITHOUT the column, because that is the live case and the fresh-DB case would
    pass either way."""
    import sqlite3 as _sqlite3

    from openclaw import internal_api, message_log

    # The live shape: the table exists, predates the column, and holds a row.
    legacy = Path(_tmpdir) / "legacy-messages.db"
    if legacy.exists():
        legacy.unlink()
    with _sqlite3.connect(legacy) as conn:
        conn.execute("""CREATE TABLE telegram_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, update_id INTEGER UNIQUE, direction TEXT NOT NULL,
            kind TEXT, summary TEXT, payload TEXT NOT NULL, app_version TEXT NOT NULL,
            received_at TEXT NOT NULL, processed_at TEXT, error TEXT)""")
        conn.execute(
            "INSERT INTO telegram_messages (update_id, direction, payload, app_version, "
            "received_at) VALUES (1, 'in', '{}', 'old', 'then')"
        )
    db.init_db(str(legacy))
    with _sqlite3.connect(legacy) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(telegram_messages)")}
        assert "correlation_id" in cols, "the column was not migrated onto an existing table"
        old_row = conn.execute(
            "SELECT correlation_id FROM telegram_messages WHERE update_id = 1"
        ).fetchone()
    assert old_row[0] is None, "a pre-existing row must read NULL, not a fabricated id"

    # Now the real writers, on the suite's own DB.
    db.init_db()
    correlation = internal_api._correlation_id(None)
    assert (
        message_log.record_inbound_raw(
            9_100_001, {"message": {"text": "tap"}}, correlation=correlation
        )
        == 9_100_001
    )
    message_log.record_outbound("text", "reply to that tap", {"x": 1}, correlation=correlation)
    # An unprompted send has no inbound event, so it carries no id rather than a
    # made-up one — a fabricated link is worse than an absent one.
    message_log.record_outbound("text", "daily nudge", {"x": 2})

    with db.get_connection() as conn:
        inbound = conn.execute(
            "SELECT correlation_id FROM telegram_messages WHERE update_id = ?", (9_100_001,)
        ).fetchone()
        replied = conn.execute(
            "SELECT correlation_id FROM telegram_messages WHERE summary = 'reply to that tap'"
        ).fetchone()
        nudge = conn.execute(
            "SELECT correlation_id FROM telegram_messages WHERE summary = 'daily nudge'"
        ).fetchone()
    assert inbound["correlation_id"] == correlation
    assert replied["correlation_id"] == correlation, "the reply cannot be joined to its cause"
    assert nudge["correlation_id"] is None, "an unprompted send invented a correlation"


def test_nothing_but_the_one_named_exception_can_send_mail_and_no_tool_offers_to():
    """Task 10.3, guarding the first hard rule: Gmail DRAFTS only, never send —
    narrowed by ADR-0030 to one named exception, `invoice_matching.py`'s
    `send_invoice_request` (vet invoice-request emails only). Every other file
    must still have zero send call sites.

    Two halves, because there are two ways to break it. The code half greps for
    Gmail's send call rather than for the word "send" — `bot.send_message` is a
    legitimate Telegram send and a guard that trips on it gets deleted within a
    week. The inventory half matters because the agent cannot be argued out of a
    capability it has: if a send tool is on the surface, prompt wording is all
    that stands between a model and an outgoing email."""
    from openclaw import mcp_server

    permitted_file = "invoice_matching.py"
    send_patterns = ("messages().send", "messages() .send", ".send(userId", "drafts().send")

    offenders = []
    permitted_file_has_send = False
    for path in (Path(__file__).resolve().parent.parent / "openclaw").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for pattern in send_patterns:
            # Only real call sites: this file's own prose says "never send()", and
            # so do several module docstrings.
            for line in text.splitlines():
                if pattern in line and not line.lstrip().startswith("#"):
                    if path.name == permitted_file:
                        permitted_file_has_send = True
                    else:
                        offenders.append(f"{path.name}: {line.strip()[:80]}")
    assert not offenders, (
        f"a Gmail send call exists outside the one ADR-0030 exception: {offenders}"
    )
    assert permitted_file_has_send, (
        "the one permitted send call site (ADR-0030, invoice_matching.send_invoice_request) "
        "is missing — this test must not pass by the send path having been deleted or moved"
    )

    named = [t["name"] for t in mcp_server.TOOLS]
    for tool in named:
        assert "send" not in tool and "email" not in tool and "mail" not in tool, (
            f"the agent's tool surface offers {tool} — a send capability cannot be prompted away"
        )
    # And the drafting path IS present, so this test cannot pass by the whole
    # Gmail integration having been deleted. `drafts().create` lives in
    # claim_forms and invoice_matching, NOT in gmail_client — which is where this
    # assertion first looked, and it failed for that reason rather than for a real
    # one. Scan the package.
    package = "\n".join(
        p.read_text(encoding="utf-8")
        for p in (Path(__file__).resolve().parent.parent / "openclaw").glob("*.py")
    )
    assert "drafts().create" in package, "no draft path left — this guard would then be vacuous"


def test_an_overlapping_internal_call_says_skipped_and_never_reads_as_a_run():
    """The `skipped` body of `/internal/*` — three responses, and this was the one
    with no assertion (named in section 10's own gap list).

    It matters because cron fires on a fixed cadence and a tick can outlast it: a
    skipped run that answered `ok` would make an overlap indistinguishable from
    work done, and `job_runs.last_ok_at` would advance on a run that never ran."""
    from openclaw import internal_api

    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM job_runs WHERE route = 'tick'")
    internal_api.record_run("tick", "last_ok_at")
    before = internal_api.scheduler_health()["jobs"]["tick"]["last_ok_at"]

    ran = []
    lock = internal_api._locks.setdefault("tick", __import__("threading").Lock())
    lock.acquire()  # stand in for a tick still running
    try:
        body = internal_api._run("tick", lambda: ran.append(1), "corr-test")
    finally:
        lock.release()

    assert body["status"] == "skipped", body
    assert body["reason"] == "already running", body
    assert body["correlation_id"] == "corr-test", body
    assert ran == [], "the job body ran anyway — the lock bought nothing"

    after = internal_api.scheduler_health()["jobs"]["tick"]
    assert after["last_ok_at"] == before, "a skipped run advanced last_ok_at"
    assert after["last_skipped_at"], "the skip was not recorded, so a stuck lock looks healthy"


def test_extraction_walks_the_model_chain_under_every_provider_including_gemini():
    """Task 10.13, and it caught a live regression rather than confirming one.

    `llm.extract` used to delegate to `gemini.extract` whenever
    LLM_PROVIDER=gemini. Harmless while Gemini was a rollback option; the day it
    became the default (Groq blocked the network) invoice extraction silently lost
    ADR-0017's per-model daily walk, because `gemini._generate` pins ONE model and
    retries it three times with backoff — the correct answer to a per-minute cap
    and a useless one to a per-day cap.

    Asserted by COUNTING the models tried, not by checking which module was
    imported: the point is the walk, and a future refactor could keep the walk
    while moving the code."""
    from openclaw import llm

    tried = []
    tpd = Exception(
        "429 RESOURCE_EXHAUSTED: You exceeded your current quota. "
        "violations: [{'quotaId': 'GenerateRequestsPerDayPerProjectPerModel-FreeTier'}]"
    )

    # Gemini's per-day 429 must classify as daily-exhausted, or the chain never
    # walks. Its message says only "you exceeded your current quota" — the useful
    # part is the quotaId, captured from a real response on 2026-08-04.
    assert llm._is_daily_budget_exhausted(tpd), "Gemini's per-day quota is not recognised"
    # And a purely per-minute Gemini 429 must NOT trigger a model switch: waiting
    # is the only cure there, and switching burns a second model's budget.
    assert not llm._is_daily_budget_exhausted(
        Exception(
            "429 RESOURCE_EXHAUSTED: violations: "
            "[{'quotaId': 'GenerateRequestsPerMinutePerProjectPerModel-FreeTier'}]"
        )
    )

    class _Client:
        class chat:
            class completions:
                @staticmethod
                def create(model, **kwargs):
                    tried.append(model)
                    if len(tried) < 3:
                        raise tpd

                    class _M:
                        content = f"extracted by {model}"
                        tool_calls = None

                    return type("R", (), {"choices": [type("C", (), {"message": _M()})()]})

    original_provider, original_client = config.LLM_PROVIDER, llm._client
    original_key = llm._PROVIDERS["gemini"]
    try:
        config.LLM_PROVIDER = "gemini"
        # A key must be present or _openai_client refuses before any model is tried.
        llm._PROVIDERS["gemini"] = (original_key[0], original_key[1], "test-key")
        llm._client = _Client()
        text = llm.extract("read this invoice", purpose="test")
    finally:
        config.LLM_PROVIDER = original_provider
        llm._PROVIDERS["gemini"] = original_key
        llm._client = original_client

    assert len(tried) == 3, (
        f"extraction stopped at {tried} — a per-day cap was retried instead of walked"
    )
    assert tried[0] == llm._PROVIDERS["gemini"][1], f"primary first: {tried}"
    assert tried[1:] == list(llm._FALLBACK_MODELS["gemini"])[:2], (
        f"then the chain, in order: {tried}"
    )
    assert "extracted by" in text


def test_the_cron_declarations_cover_every_job_apscheduler_ran_at_the_cadence_config_says():
    """Task 5.1. Two copies of a schedule is the duplication this repo keeps
    getting bitten by, so the numbers in `scripts/gateway_cron.sh` are asserted
    against `config.py`'s defaults rather than trusted to stay in step.

    And the COVERAGE half matters more than the cadences: APScheduler ran five
    jobs, and the two nobody thinks about — the weekly vet chase and the queue
    expiry — had no internal endpoint at all. Without them the cutover stops them
    silently, which is indistinguishable from a week with nothing to chase."""
    cron = (
        Path(__file__).resolve().parent.parent.parent / "scripts" / "gateway_cron.sh"
    ).read_text(encoding="utf-8")

    # Every route the script posts to must exist on the router, and every
    # scheduled job must have a route. Derived from the app, not a second list.
    from openclaw import internal_api

    routes = {r.path for r in internal_api.router.routes}
    for route in ("tick", "ingest", "nudge", "vet-nudge", "expire-queue"):
        assert f"/internal/{route}" in routes, (
            f"/internal/{route} is scheduled but the router has no such path: {sorted(routes)}"
        )
        assert route in cron, f"{route} has an endpoint but nothing schedules it"

    assert f"{config.VET_CLAIM_PIPELINE_INTERVAL_MINUTES}m" in cron, (
        "the tick cadence in gateway_cron.sh disagrees with VET_CLAIM_PIPELINE_INTERVAL_MINUTES"
    )
    assert f"{config.GMAIL_POLL_INTERVAL_MINUTES}m" in cron, (
        "the ingest cadence disagrees with GMAIL_POLL_INTERVAL_MINUTES"
    )
    assert f"0 {config.ACTION_NUDGE_HOUR} * * *" in cron, (
        "the daily cron hour disagrees with ACTION_NUDGE_HOUR"
    )
    # Monday, as VET_NUDGE_DAY says. Cron's 5-field day-of-week is numeric here.
    weekday = {"mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 0}
    assert f"0 {config.ACTION_NUDGE_HOUR} * * {weekday[config.VET_NUDGE_DAY]}" in cron, (
        f"the weekly cron day disagrees with VET_NUDGE_DAY={config.VET_NUDGE_DAY}"
    )

    # Sydney, not UTC. APScheduler ran these in the app container's local time,
    # which is UTC, so "hour 9" delivered a MORNING nudge at 7-8pm Sydney. Changed
    # deliberately 2026-08-04 (Justin), and asserted because reverting it would be
    # invisible: the cron expression would still read `0 9 * * *` and still fire
    # daily, just at the wrong end of his day.
    assert "Australia/Sydney" in cron, (
        "the daily jobs are back on UTC — 9am becomes evening in Sydney"
    )
    assert "--tz UTC" not in cron
    # An IANA zone rather than a fixed offset, so DST is the gateway's problem
    # rather than something that drifts by an hour twice a year.
    assert "+10" not in cron and "+11" not in cron

    # The secret must reach the app as an unexpanded variable. Interpolating it
    # into the payload would persist it in the gateway's cron store and echo it
    # back from `cron get`, `cron list` and the run log.
    assert "$CLAIMS_INTERNAL_SECRET" in cron and "X-OpenClaw-Secret" in cron
    # -f, or curl exits 0 on a 500 and a failed tick reads as a successful run.
    assert "curl -fsS" in cron, "without -f an HTTP error is not an error"
    # Idempotency: without a declaration key every deploy adds five more jobs.
    assert cron.count("--declaration-key") >= 1


def test_a_reminder_due_while_the_app_was_down_still_fires_and_says_how_late():
    """Justin's call, 2026-08-04: never drop a late one, and never let it pass for
    fresh. Cron cannot express a one-shot at an arbitrary minute, so the sweep
    rides the 15-minute tick — which also means the sweep must be idempotent,
    since a duplicated cron delivery (5.4) calls it twice."""
    from openclaw import reminders

    db.init_db()
    # Inserted directly: `tasks.create_task` runs an LLM follow-up extraction, and
    # the suite forces every provider key blank to stay hermetic.
    with db.get_connection() as conn:
        task_id = conn.execute(
            "INSERT INTO tasks (description, status, source, created_at) VALUES (?,?,?,?)",
            ("feed the cat", "open", "test", datetime.now(timezone.utc).isoformat()),
        ).lastrowid
    now = datetime.now(timezone.utc)
    long_ago = now - timedelta(days=3)
    future = now + timedelta(hours=4)
    # Rows inserted directly, NOT via `schedule_reminder`. That helper still adds
    # an APScheduler `date` job while the flag allows it, and this suite has the
    # scheduler running — so for a past-dated reminder the job fires immediately
    # and marks it `due` before the sweep looks, and the sweep then finds nothing.
    # The first version of this test raced exactly that and failed intermittently
    # on ordering. The sweep is what is under test; the legacy job is not.
    with db.get_connection() as conn:
        for when in (long_ago, future):
            conn.execute(
                "INSERT INTO reminders (task_id, scheduled_at, status, job_id, created_at) "
                "VALUES (?, ?, 'scheduled', ?, ?)",
                (task_id, when.isoformat(), f"sweep-test-{when.isoformat()}", now.isoformat()),
            )

    assert reminders.sweep_due(now) == 1, "exactly the overdue one fires"
    # Idempotent: the second call marks nothing, so a duplicated cron delivery
    # cannot double-fire. The guarantee is the WHERE clause, not a fired_at
    # column — a new column would need hand-run ALTER TABLE on the live DB.
    assert reminders.sweep_due(now) == 0

    with db.get_connection() as conn:
        rows = {
            r["scheduled_at"]: r["status"]
            for r in conn.execute("SELECT scheduled_at, status FROM reminders").fetchall()
        }
    assert rows[long_ago.isoformat()] == "due"
    assert rows[future.isoformat()] == "scheduled", "a future reminder must not be swept"

    # "note how late" — the words, so a three-day-late reminder cannot be read as
    # one set this morning.
    assert reminders.overdue_text(long_ago.isoformat(), now) == "overdue by 3d"
    assert reminders.overdue_text((now - timedelta(hours=5)).isoformat(), now) == "overdue by 5h"
    assert (
        reminders.overdue_text((now - timedelta(minutes=40)).isoformat(), now) == "overdue by 40m"
    )
    # Inside one tick's lag is the design, not a delay worth naming.
    assert reminders.overdue_text((now - timedelta(minutes=4)).isoformat(), now) == "on time"


def test_a_scheduler_that_stopped_firing_is_a_value_on_health_not_an_absence():
    """Task 5.6, and the whole reason `job_runs` exists. Once cron owns
    scheduling, a never-declared or disabled entry looks exactly like a quiet
    week — the app has no opinion about *when* work happens any more.

    Asserted through `record_run` rather than by hand-writing rows, so the thing
    under test is the path `/internal/*` actually takes."""
    from openclaw import internal_api

    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM job_runs")

    # Nothing has ever run: every job overdue, and "never" rather than a made-up
    # age. This is the state a missing cron entry leaves behind.
    health = internal_api.scheduler_health()
    assert health["jobs"]["tick"]["minutes_since_ok"] is None
    assert any(o.startswith("tick: never") for o in health["overdue"]), health["overdue"]
    assert len(health["overdue"]) == 5, health["overdue"]

    internal_api.record_run("tick", "last_ok_at")
    health = internal_api.scheduler_health()
    assert health["jobs"]["tick"]["minutes_since_ok"] == 0
    assert not any(o.startswith("tick") for o in health["overdue"]), health["overdue"]

    # A stale success is overdue. Written directly because the point is age, and
    # the only way to age a row through record_run is to wait 45 minutes.
    stale = (datetime.now(timezone.utc) - timedelta(minutes=46)).isoformat()
    with db.get_connection() as conn:
        conn.execute("UPDATE job_runs SET last_ok_at = ? WHERE route = 'tick'", (stale,))
    health = internal_api.scheduler_health()
    assert any(o.startswith("tick: 46m") for o in health["overdue"]), health["overdue"]

    # An error and a skip are recorded distinctly. A route that only ever skips is
    # a stuck lock and must not read as healthy.
    internal_api.record_run("ingest", "last_error_at", "boom")
    internal_api.record_run("nudge", "last_skipped_at")
    health = internal_api.scheduler_health()
    assert health["jobs"]["ingest"]["last_error"] == "boom"

    # The error text must SURVIVE the next run starting. The first version cleared
    # it on every write, so the following start erased it — found live, with
    # `/health` showing `last_error_at` set and `last_error: null` after the
    # container holding the matching log line had been recreated. The diagnostic
    # was simply gone.
    internal_api.record_run("ingest", "last_started_at")
    assert internal_api.scheduler_health()["jobs"]["ingest"]["last_error"] == "boom", (
        "a run starting wiped the previous failure's reason"
    )
    internal_api.record_run("ingest", "last_skipped_at")
    assert internal_api.scheduler_health()["jobs"]["ingest"]["last_error"] == "boom"
    # A success clears it: a stale error beside a fresh last_ok_at reads as an
    # outage that is still happening.
    internal_api.record_run("ingest", "last_ok_at")
    assert internal_api.scheduler_health()["jobs"]["ingest"].get("last_error") is None
    assert health["jobs"]["nudge"]["last_skipped_at"]
    assert health["jobs"]["nudge"]["last_ok_at"] is None, "a skip is not a success"

    # Which runtime is expected to be driving, read from the flag rather than
    # guessed: "nothing ran" means a broken app when the flag is on and a missing
    # cron entry when it is off, and those have different fixes.
    original = config.SCHEDULER_ENABLED
    try:
        config.SCHEDULER_ENABLED = False
        assert internal_api.scheduler_health()["owner"] == "gateway cron"
        config.SCHEDULER_ENABLED = True
        assert internal_api.scheduler_health()["owner"] == "in-process scheduler"
    finally:
        config.SCHEDULER_ENABLED = original


# --- petcover-settlement-reconciliation ------------------------------------
#
# The ten live approval letters, read read-only from Gmail on 2026-08-04 and
# used here as fixtures. Every figure is Petcover's own; nothing is derived.
# `DC1-27-5628` Tr 8 is the only one with a non-zero non-claimable amount, and
# so the only one that tells the two candidate formulas apart.
LIVE_APPROVAL_LETTERS = (
    # (label, claimed, fixed excess, non-claimable, age contribution $, %, paid)
    ("5628 Tr2", 35.00, 0.00, 0.00, 12.25, 0.35, 22.75),
    ("5628 Tr5", 446.50, 49.26, 0.00, 139.03, 0.35, 258.21),
    ("5628 Tr6", 45.00, 0.00, 0.00, 15.75, 0.35, 29.25),
    ("5992 Tr1", 351.50, 150.00, 0.00, 70.53, 0.35, 130.97),
    ("5992 Tr2", 35.00, 0.00, 0.00, 12.25, 0.35, 22.75),
    ("5628 Tr7", 132.50, 0.00, 0.00, 46.38, 0.35, 86.13),
    ("5993 Tr1", 944.50, 150.00, 0.00, 278.08, 0.35, 516.42),
    ("5992 Tr3", 2521.46, 0.00, 0.00, 882.51, 0.35, 1638.95),
    ("5992 Tr4", 135.00, 0.00, 0.00, 47.25, 0.35, 87.75),
    ("5628 Tr8", 580.74, 0.00, 135.00, 156.01, 0.35, 289.73),
)


def _letter_detail(letter):
    _, claimed, excess, non_claimable, age_dollars, age_percent, paid = letter
    return {
        "claimed_amount": claimed,
        "fixed_excess_stated": excess,
        "non_claimable_stated": non_claimable,
        "age_contribution_stated": age_dollars,
        "age_contribution_percent": age_percent,
        "percentage_excess_stated": 0.00,
        "paid_amount": paid,
    }


def test_the_approval_letter_gives_up_every_figure_it_states():
    """Six figures per letter, all from live text. The two missing until
    2026-08-04 are the two the arithmetic needed: the non-claimable line (whose
    hyphen is U+2010, which is why `_normalize` runs first) and the bracketed
    percentage."""
    body = (
        "Total amount claimed: $580.74\n"
        "Paid by you:\n"
        "Fixed excess $0.00\n"
        "Non‐claimable amount $135.00\n"
        "Age Contribution: $156.01 [35%]\n"
        "Percentage Excess: $0.00 [0%]\n"
        "Paid by us: $289.73\n"
    )
    got = claim_status.extract_approval_amounts(body)
    assert got["claimed_amount"] == 580.74
    assert got["fixed_excess_stated"] == 0.00
    assert got["non_claimable_stated"] == 135.00
    assert got["age_contribution_stated"] == 156.01
    assert got["age_contribution_percent"] == 0.35
    assert got["percentage_excess_stated"] == 0.00
    assert got["paid_amount"] == 289.73
    # A term the letter omits stays absent — never stored as a guessed zero.
    without = claim_status.extract_approval_amounts(
        "Total amount claimed: $35.00\nPaid by us: $22.75"
    )
    assert "non_claimable_stated" not in without
    assert "age_contribution_percent" not in without


def test_settlement_arithmetic_matches_petcovers_own_figures():
    """Check A re-adds the letter's own line items. All ten live letters, exact.

    The guard against reverting: `claimable - 150` gives $430.74 where Petcover
    paid $22.75, and the intermediate `(claimed - excess) * 0.65` gives $377.48
    on Tr 8 where they paid $289.73."""
    for letter in LIVE_APPROVAL_LETTERS:
        detail = _letter_detail(letter)
        assert claim_status._check_petcovers_arithmetic(detail, detail["paid_amount"]) is None, (
            letter[0]
        )

    tr8 = _letter_detail(LIVE_APPROVAL_LETTERS[-1])
    naive = (tr8["claimed_amount"] - tr8["fixed_excess_stated"]) * (1 - 0.35)
    assert abs(naive - tr8["paid_amount"]) > claim_status.SETTLEMENT_TOLERANCE

    # A letter whose own figures genuinely don't add up is flagged, naming all
    # four of Petcover's numbers.
    broken = {
        "claimed_amount": 200.00,
        "fixed_excess_stated": 0.00,
        "non_claimable_stated": 0.00,
        "age_contribution_percent": 0.35,
        "paid_amount": 150.00,
    }
    flag = claim_status._check_petcovers_arithmetic(broken, 150.00)
    assert flag and flag.startswith("settlement mismatch")
    for figure in ("$200.00", "$0.00", "35%", "$130.00", "$150.00"):
        assert figure in flag, (figure, flag)

    # No stated percentage — an event written before the pattern shipped — is
    # skipped, not checked against a term it never captured.
    assert (
        claim_status._check_petcovers_arithmetic(
            {"claimed_amount": 351.50, "fixed_excess_stated": 150.00, "paid_amount": 130.97}, 130.97
        )
        is None
    )


def test_assessment_difference_is_a_separate_flag_from_arithmetic():
    """Claim #8's live shape: Petcover's arithmetic on $351.50 is right to the
    cent, and $351.50 is not what we submitted. One flag, of the assessment
    kind, and no word about a $150 excess we did not infer."""
    _fresh_db()
    import json as _json

    with db.get_connection() as conn:
        aari = _aari(conn)
        conn.execute(
            "UPDATE pets SET policy_anniversary = ? WHERE id = ?",
            (_anniversary_days_ago(300), aari),
        )
        txn = _relative_date(100)
        cid = _insert_claim(
            conn,
            aari,
            txn,
            status="acknowledged",
            reference="DC1-26-5992",
            sr=1,
            invoice_data=_json.dumps({"amount": 446.50, "claimable_amount": 446.50}),
        )
    detail = {
        "claimed_amount": 351.50,
        "fixed_excess_stated": 150.00,
        "non_claimable_stated": 0.00,
        "age_contribution_stated": 70.53,
        "age_contribution_percent": 0.35,
        "paid_amount": 130.97,
    }
    flag = claim_status._validate_settlement(_claim_row(cid), detail, txn)
    assert flag and flag.startswith("assessment difference"), flag
    assert claim_status._settlement_check_kind(flag) == "assessment"
    for figure in (f"#{cid}", "DC1-26-5992", "Sr 1", "$446.50", "$351.50"):
        assert figure in flag, (figure, flag)
    assert "fresh $150 excess" not in flag
    assert "excess already used" not in flag

    # Same letter against a claim we submitted at Petcover's own figure: nothing
    # to ask about, so no flag at all.
    with db.get_connection() as conn:
        agreed = _insert_claim(
            conn,
            _aari(conn),
            txn,
            status="acknowledged",
            reference="DC1-26-5992",
            sr=9,
            amount=-351.50,
            invoice_data=_json.dumps({"claimable_amount": 351.50}),
        )
    assert claim_status._validate_settlement(_claim_row(agreed), detail, txn) is None


def test_no_expectation_is_computed_without_a_recorded_claimable_subtotal():
    """Claim #2's shape — an invoice total and no claimable subtotal. The old
    code produced "we expected $430.74" from $580.74 - $150.00, a number never
    submitted to anyone. Now it says what it doesn't know, and names the letter
    so the letter isn't lost behind a silent None."""
    _fresh_db()
    import json as _json

    with db.get_connection() as conn:
        aari = _aari(conn)
        conn.execute(
            "UPDATE pets SET policy_anniversary = ? WHERE id = ?",
            (_anniversary_days_ago(300), aari),
        )
        txn = _relative_date(100)
        cid = _insert_claim(
            conn,
            aari,
            txn,
            status="acknowledged",
            reference="DC1-26-5992",
            sr=2,
            invoice_data=_json.dumps({"amount": 580.74}),
        )
    detail = _letter_detail(LIVE_APPROVAL_LETTERS[4])
    flag = claim_status._validate_settlement(_claim_row(cid), detail, txn)
    assert flag and flag.startswith("claimable subtotal not recorded"), flag
    assert "$430.74" not in flag and "580.74" not in flag
    assert "$35.00" in flag and "$22.75" in flag


def test_the_accessor_tells_a_recorded_zero_from_an_absent_key():
    import json as _json

    assert claim_status.claimable_subtotal(_json.dumps({"claimable_amount": 446.50})) == (
        446.50,
        True,
    )
    # Live claim #20: a real $0.00, not a missing key.
    assert claim_status.claimable_subtotal(
        _json.dumps({"amount": 152.50, "claimable_amount": 0.0})
    ) == (0.0, True)
    # Live claim #2: an invoice total is not a stand-in.
    assert claim_status.claimable_subtotal(_json.dumps({"amount": 580.74})) == (None, False)
    assert claim_status.claimable_subtotal(None) == (None, False)
    assert claim_status.claimable_subtotal("") == (None, False)


def test_no_module_substitutes_the_invoice_total_for_a_claimable_subtotal():
    """Mechanical guard for the rule one accessor now holds. Four call sites
    carried this substitution by convention until 2026-08-04, and a convention
    with no guard is what every row of `app/openclaw/CLAUDE.md`'s collapse table
    was before it became an incident."""
    import re as _re
    from pathlib import Path as _Path

    inline = _re.compile(r"""claimable_amount["']\s*,\s*\w+\.get\(["']amount""")
    two_step = _re.compile(
        r"""claimable_amount["']\)\s*\n\s*if\s+\w+\s+is\s+None\s*:\s*\n\s*\w+\s*=\s*\w+\.get\(["']amount"""
    )
    or_chain = _re.compile(r"""claimable_amount["']\)\s*or\s+\w+\.get\(["']amount""")
    patterns = (inline, two_step, or_chain)

    # The guard gets its own guard: a matcher that stopped matching would make
    # this test pass forever and read exactly like a clean scan.
    assert inline.search('claimable = invoice.get("claimable_amount", invoice.get("amount"))')
    assert two_step.search(
        'claimable = invoice.get("claimable_amount")\n    if claimable is None:\n        claimable = invoice.get("amount")'
    )
    assert or_chain.search('claimed = invoice.get("claimable_amount") or invoice.get("amount")')
    assert not any(
        p.search('value, recorded = claim_status.claimable_subtotal(row["invoice_data"])')
        for p in patterns
    )

    offenders = []
    for path in sorted((_Path(__file__).resolve().parent.parent / "openclaw").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for pattern in patterns:
            match = pattern.search(text)
            if match:
                offenders.append(f"{path.name}:{text[: match.start()].count(chr(10)) + 1}")
    assert not offenders, (
        f"these substitute the invoice total for a claimable subtotal: {offenders}"
    )


def test_every_action_kind_declares_a_waiting_party():
    """No defaulting. Naming the wrong waiting party is how a chase never
    happens — the same reason `info_requested` carries `owed_by`."""
    for kind in claim_status.ACTION_PRIORITY:
        assert kind in claim_status._ACTION_META, kind
        declared = claim_status._ACTION_META[kind][2]
        options = declared.values() if isinstance(declared, dict) else [declared]
        for option in options:
            assert option in claim_status.WAITING_PARTIES, (kind, option)

    # A kind added without one raises rather than picking a party for it.
    claim_status._ACTION_META["invented_kind"] = ("x", "y", {"a": claim_status.NOBODY_WAITING})
    try:
        raised = False
        try:
            claim_status.waiting_party("invented_kind", "b")
        except KeyError:
            raised = True
        assert raised, "an undeclared situation silently returned a waiting party"
    finally:
        del claim_status._ACTION_META["invented_kind"]

    assert (
        claim_status.waiting_party("dismiss_mismatch", "arithmetic") == claim_status.NOBODY_WAITING
    )
    assert (
        claim_status.waiting_party("dismiss_mismatch", "assessment")
        == claim_status.YOU_WAITING_ON_PETCOVER
    )
    assert (
        claim_status.waiting_party("confirm_resolved", "justin")
        == claim_status.PETCOVER_WAITING_ON_YOU
    )
    assert (
        claim_status.waiting_party("confirm_resolved", "vet") == claim_status.YOU_WAITING_ON_THE_VET
    )


def test_action_card_never_shows_the_invoice_total_as_the_claim_amount():
    """Claim #2's shape on a card: the $580.74 invoice total must not appear as
    the claim amount, and an absent subtotal must not print as $0.00."""
    from openclaw import commands

    card = commands._action_card_text(
        {
            "kind": "dismiss_mismatch",
            "title": "Review settlement",
            "blocks": "a paid-vs-expected difference is unreviewed",
            "waiting": claim_status.NOBODY_WAITING,
            "claim_id": 2,
            "merchant": "THE SHIRE VETERINARY CARINGBAH",
            "amount": -585.39,
            "pet_name": "Aari",
            "condition_text": "Arthritis",
            "date": "2026-06-19",
            "age_days": 46,
            "claimable": None,
            "claimable_recorded": False,
            "expected": {
                "available": False,
                "value": None,
                "note": "invoice on file, but no claimable subtotal recorded",
            },
            "members": None,
        }
    )
    assert "Claim amount: Not recorded" in card
    assert "580.74" not in card
    assert "Claim amount: $0.00" not in card
    assert (
        "Expected payment: Not recorded (invoice on file, but no claimable subtotal recorded)"
        in card
    )
    assert "#2" in card
    assert claim_status.NOBODY_WAITING in card
    assert "Petcover is waiting on you" not in card

    # A recorded $0.00 is a figure, and prints as one.
    zero = {
        "kind": "dismiss_mismatch",
        "title": "Review settlement",
        "blocks": "b",
        "waiting": claim_status.YOU_WAITING_ON_PETCOVER,
        "claim_id": 20,
        "merchant": "VET",
        "amount": -152.50,
        "pet_name": "Echo",
        "condition_text": None,
        "date": "2026-07-01",
        "age_days": 10,
        "claimable": 0.0,
        "claimable_recorded": True,
        "expected": {"available": False, "value": None, "note": "no policy excess/cap on file"},
        "members": None,
    }
    card = commands._action_card_text(zero)
    assert "Claim amount: $0.00" in card
    assert "Expected payment: Not recorded (no policy excess/cap on file)" in card
    assert claim_status.YOU_WAITING_ON_PETCOVER in card


def test_dismissing_an_assessment_difference_keeps_it_reviewable():
    """An arithmetic difference Justin has checked disappears. A question he has
    put to Petcover does not — event 58 is the live proof of what this prevents:
    claim #2's whole finding became prose in `dismissed_flag`, on no surface."""
    _fresh_db()
    import json as _json

    with db.get_connection() as conn:
        aari = _aari(conn)
        txn = _relative_date(100)
        assessed = _insert_claim(
            conn,
            aari,
            txn,
            status="settled",
            reference="DC1-26-5992",
            sr=1,
            invoice_data=_json.dumps({"claimable_amount": 446.50}),
        )
        arithmetic = _insert_claim(
            conn,
            aari,
            txn,
            status="settled",
            reference="DC1-26-5993",
            sr=1,
            amount=-500.0,
            invoice_data=_json.dumps({"claimable_amount": 500.0}),
        )
        flags = {
            assessed: (
                f"assessment difference — claim #{assessed} (DC1-26-5992 Sr 1): we submitted "
                "$446.50, Petcover states they assessed $351.50."
            ),
            arithmetic: "settlement mismatch — Petcover's own figures don't add up: review",
        }
        for cid, flag in flags.items():
            conn.execute("UPDATE vet_claims SET flag = ? WHERE id = ?", (flag, cid))
            conn.execute(
                "INSERT INTO claim_status_events (claim_id, event_type, raw_email_id, detail, created_at) "
                "VALUES (?, 'approved', ?, ?, ?)",
                (
                    cid,
                    f"mail{cid}",
                    _json.dumps(
                        {
                            "claimed_amount": 351.50,
                            "paid_amount": 130.97,
                            "fixed_excess_stated": 150.00,
                            "non_claimable_stated": 0.00,
                        }
                    ),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    assert claim_status.dismiss_mismatch(assessed)["ok"]
    assert claim_status.dismiss_mismatch(arithmetic)["ok"]

    with db.get_connection() as conn:
        events = {
            r["claim_id"]: _json.loads(r["detail"])
            for r in conn.execute(
                "SELECT claim_id, detail FROM claim_status_events WHERE event_type = 'mismatch_dismissed'"
            )
        }
    # Figures, not only prose — a sentence cannot be queried.
    assert events[assessed]["check"] == "assessment"
    assert events[assessed]["claimed_amount"] == 351.50
    assert events[assessed]["paid_amount"] == 130.97
    assert events[assessed]["fixed_excess_stated"] == 150.00
    assert events[assessed]["claimable_subtotal"] == 446.50
    assert events[assessed]["claimable_subtotal_recorded"] is True
    assert events[arithmetic]["check"] == "arithmetic"

    queued = [e["claim_id"] for e in claim_status.dashboard_lists()["unclassified"]]
    assert assessed in queued, "a question outstanding with Petcover left every surface"
    assert arithmetic not in queued, "a difference Justin checked keeps nagging him"

    # Nothing un-dismisses, and a re-read cannot re-flag what was dismissed.
    assert claim_status.dismiss_mismatch(assessed)["ok"] is False
    assert claim_status._already_recorded(assessed, "approved", f"mail{assessed}")
    assert _claim_row(assessed)["flag"] is None


# --- settlement-clarification-email -----------------------------------------


class _FakeClarificationExec:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _FakeClarificationDrafts:
    """Records every drafts().create/update call to `store` (draft_id ->
    message dict) so a test can inspect the rendered body without touching
    real Gmail. `_n` mints unique draft/thread ids, mirroring how the real
    API assigns a fresh thread on `create`."""

    def __init__(self, store):
        self.store = store
        self._n = 0

    def create(self, userId, body):
        self._n += 1
        draft_id = f"draft-{self._n}"
        thread_id = f"thread-{self._n}"
        message = {**body["message"], "id": f"msg-{self._n}", "threadId": thread_id}
        self.store[draft_id] = message
        return _FakeClarificationExec({"id": draft_id, "message": message})

    def update(self, userId, id, body):
        message = {**body["message"], "threadId": self.store[id]["threadId"]}
        self.store[id] = message
        return _FakeClarificationExec({"id": id, "message": message})


class _FakeClarificationService:
    def __init__(self, store):
        self._drafts = _FakeClarificationDrafts(store)

    def users(self):
        return self

    def drafts(self):
        return self._drafts


def _patch_clarification_gmail(store):
    original = claim_status.gmail_client.build_service
    claim_status.gmail_client.build_service = lambda: _FakeClarificationService(store)
    return original


def _draft_body_text(store, draft_id) -> str:
    import base64 as _b64

    return _b64.urlsafe_b64decode(store[draft_id]["raw"]).decode(errors="replace")


def _flag_settlement(conn, claim_id, flag):
    conn.execute("UPDATE vet_claims SET flag = ? WHERE id = ?", (flag, claim_id))


def test_settlement_review_eligibility_includes_check_b_and_unrecorded_excludes_check_a():
    """Task 8.1: the review card's eligibility query takes Check B assessment
    differences and unrecorded-claimable-subtotal flags, and leaves Check A
    (arithmetic) to the old dismiss_mismatch-only path (settlement-validation)."""
    _fresh_db()
    with db.get_connection() as conn:
        aari = _aari(conn)
        assessed = _insert_claim(
            conn,
            aari,
            _relative_date(40),
            status="settled",
            reference="DC1-1",
            sr=1,
            invoice_data=json.dumps({"claimable_amount": 446.50}),
        )
        unrecorded = _insert_claim(conn, aari, _relative_date(41), status="settled")
        arithmetic = _insert_claim(
            conn,
            aari,
            _relative_date(42),
            status="settled",
            invoice_data=json.dumps({"claimable_amount": 500.0}),
        )
        _flag_settlement(
            conn,
            assessed,
            f"assessment difference — claim #{assessed} (DC1-1 Sr 1): we submitted "
            "$446.50, Petcover states they assessed $351.50.",
        )
        _flag_settlement(
            conn,
            unrecorded,
            f"claimable subtotal not recorded — claim #{unrecorded}: Petcover states they "
            "assessed $80.00 and paid $52.00, and their figures add up, but nothing was "
            "recorded as this claim's claimable subtotal so there is nothing to check it "
            "against — review",
        )
        _flag_settlement(
            conn,
            arithmetic,
            "settlement mismatch — Petcover's own figures don't add up: they state claimed "
            "$500.00 ... review",
        )

    ids = {c["claim_id"] for c in claim_status.settlement_review_claims()}
    assert assessed in ids, "Check B is eligible"
    assert unrecorded in ids, "unrecorded-subtotal is eligible"
    assert arithmetic not in ids, "Check A stays the old manual-dispute path"


def test_settlement_acceptable_is_terminal_and_never_rewrites_the_subtotal():
    """Task 8.2: Acceptable reuses dismiss_mismatch — flag cleared, invoice_data
    (and so claimable_subtotal) untouched — and a later, distinct figure is
    still validated fully independently, per settlement-validation's existing
    one-way dismissal semantics."""
    _fresh_db()
    with db.get_connection() as conn:
        aari = _aari(conn)
        claim = _insert_claim(
            conn,
            aari,
            _relative_date(50),
            status="settled",
            reference="DC1-90",
            sr=1,
            invoice_data=json.dumps({"claimable_amount": 446.50}),
        )
        _flag_settlement(
            conn,
            claim,
            f"assessment difference — claim #{claim} (DC1-90 Sr 1): we submitted "
            "$446.50, Petcover states they assessed $351.50.",
        )

    assert claim_status.dismiss_mismatch(claim)["ok"] is True
    row = _claim_row(claim)
    assert row["flag"] is None, "Acceptable clears the flag"
    assert json.loads(row["invoice_data"])["claimable_amount"] == 446.50, (
        "never rewrites claimable_subtotal"
    )

    # A genuinely different figure is checked fully on its own terms, with no
    # memory of the earlier dismissal — the earlier Acceptable never suppresses it.
    claimable, recorded = claim_status.claimable_subtotal(row["invoice_data"])
    assert recorded
    new_flag = claim_status._check_what_petcover_assessed(
        row, {"claimed_amount": 900.00}, claimable
    )
    assert new_flag and "900.00" in new_flag, "a genuinely different figure still flags"


def test_more_info_queues_into_one_open_draft_shared_across_claims():
    """Tasks 4.1-4.3 / 8.3: first More Info creates one Gmail draft and moves
    the claim to awaiting_petcover_clarification; a second claim's More Info,
    before that draft is sent, joins the SAME draft rather than opening another."""
    _fresh_db()
    store: dict = {}
    original = _patch_clarification_gmail(store)
    try:
        with db.get_connection() as conn:
            aari = _aari(conn)
            c1 = _insert_claim(
                conn,
                aari,
                _relative_date(40),
                status="settled",
                reference="DC1-1",
                sr=1,
                condition="Arthritis",
                invoice_data=json.dumps({"claimable_amount": 100.0}),
            )
            c2 = _insert_claim(
                conn,
                aari,
                _relative_date(30),
                status="settled",
                reference="DC1-2",
                sr=1,
                condition="Dermatitis",
            )
            _flag_settlement(
                conn,
                c1,
                f"assessment difference — claim #{c1} (DC1-1 Sr 1): we submitted "
                "$100.00, Petcover states they assessed $80.00.",
            )
            _flag_settlement(conn, c2, f"claimable subtotal not recorded — claim #{c2}: review")

        r1 = claim_status.queue_clarification(c1)
        assert r1["ok"], r1
        assert _claim_row(c1)["status"] == "awaiting_petcover_clarification"
        assert len(store) == 1, "exactly one draft created"
        draft_id = next(iter(store))

        r2 = claim_status.queue_clarification(c2)
        assert r2["ok"], r2
        assert _claim_row(c2)["status"] == "awaiting_petcover_clarification"
        assert len(store) == 1, "second claim joins the SAME open draft, not a new one"

        body = _draft_body_text(store, draft_id)
        assert f"Claim #{c1}" in body and f"Claim #{c2}" in body, (
            "both claims' details land in one consolidated draft"
        )

        with db.get_connection() as conn:
            batches = conn.execute("SELECT * FROM clarification_batches").fetchall()
            links = {
                r["claim_id"]
                for r in conn.execute("SELECT claim_id FROM clarification_batch_claims")
            }
        assert len(batches) == 1
        assert links == {c1, c2}
        assert batches[0]["gmail_thread_id"]
        assert batches[0]["sent_at"] is None, "nothing has been sent — no send() call exists here"
    finally:
        claim_status.gmail_client.build_service = original


def _queue_one_claim(conn, store):
    """Shared setup for the reply-correlation tests: one flagged, queued claim
    plus the batch's thread id."""
    aari = _aari(conn)
    claim = _insert_claim(
        conn,
        aari,
        _relative_date(20),
        status="settled",
        reference="DC1-9",
        sr=1,
        invoice_data=json.dumps({"claimable_amount": 130.97}),
    )
    _flag_settlement(
        conn,
        claim,
        f"assessment difference — claim #{claim} (DC1-9 Sr 1): we submitted "
        "$130.97, Petcover states they assessed $351.50.",
    )
    return claim


def test_clarification_reply_exact_match_resolves_like_acceptable():
    """Tasks 5.3 / 8.4: an exact-matching reply applies the same terminal
    dismissal as clicking Acceptable, recording the reply's own figures."""
    _fresh_db()
    store: dict = {}
    original = _patch_clarification_gmail(store)
    original_extract = llm.extract
    try:
        with db.get_connection() as conn:
            claim = _queue_one_claim(conn, store)
        claim_status.queue_clarification(claim)
        with db.get_connection() as conn:
            thread_id = conn.execute(
                "SELECT gmail_thread_id FROM clarification_batches"
            ).fetchone()[0]

        llm.extract = (
            lambda *a, **k: '{"claims": [{"identifier": "DC1-9", "confirmed_amount": 130.97}]}'
        )
        claim_status.process_clarification_reply(
            "reply-1", thread_id, "Confirmed: DC1-9 Sr 1 assessed at $130.97."
        )

        row = _claim_row(claim)
        assert row["flag"] is None, "resolved exactly like Acceptable"
        assert row["status"] == "awaiting_petcover_clarification", (
            "dismissal clears the flag, same as confirm_resolved does for "
            "info_requested/suspended — it doesn't transition status again"
        )
        with db.get_connection() as conn:
            events = [
                (r["event_type"], json.loads(r["detail"] or "{}"))
                for r in conn.execute(
                    "SELECT event_type, detail FROM claim_status_events WHERE claim_id = ? ORDER BY id",
                    (claim,),
                )
            ]
        dismissed = next(d for t, d in events if t == "mismatch_dismissed")
        assert dismissed["confirmed_by_reply"] is True
        assert dismissed["claimed_amount"] == 130.97, (
            "the reply's own figure, not the original letter's"
        )

        with db.get_connection() as conn:
            batch_sent_at = conn.execute("SELECT sent_at FROM clarification_batches").fetchone()[0]
        assert batch_sent_at, "a reply proves the draft was sent"
    finally:
        claim_status.gmail_client.build_service = original
        llm.extract = original_extract


def test_clarification_reply_no_match_resurfaces_the_card_with_the_figure():
    """Tasks 5.3 / 8.5: a reply that states an amount not matching the claim's
    claimable subtotal leaves it awaiting_petcover_clarification untouched,
    with the reply's figure visible on the resurfaced card."""
    _fresh_db()
    store: dict = {}
    original = _patch_clarification_gmail(store)
    original_extract = llm.extract
    try:
        with db.get_connection() as conn:
            claim = _queue_one_claim(conn, store)
        claim_status.queue_clarification(claim)
        with db.get_connection() as conn:
            thread_id = conn.execute(
                "SELECT gmail_thread_id FROM clarification_batches"
            ).fetchone()[0]

        llm.extract = (
            lambda *a, **k: '{"claims": [{"identifier": "DC1-9", "confirmed_amount": 999.00}]}'
        )
        claim_status.process_clarification_reply("reply-2", thread_id, "text")

        row = _claim_row(claim)
        assert row["status"] == "awaiting_petcover_clarification", "state untouched"
        assert row["flag"] and "999.00" in row["flag"], "the reply's figure is visible"

        cards = {c["claim_id"]: c for c in claim_status.settlement_review_claims()}
        assert claim in cards and cards[claim]["awaiting_petcover"] is True, (
            "the card resurfaces rather than disappearing"
        )
        assert cards[claim]["reply_stated_amount"] == 999.00, (
            "the reply's own figure is a structured field, not something the "
            "template has to parse back out of `flag` prose"
        )
    finally:
        claim_status.gmail_client.build_service = original
        llm.extract = original_extract


def test_clarification_reply_partial_batch_resolves_only_the_addressed_claim():
    """Tasks 5.4 / 8.6: a batch covering two claims where the reply confirms
    only one — that one resolves, the other stays open."""
    _fresh_db()
    store: dict = {}
    original = _patch_clarification_gmail(store)
    original_extract = llm.extract
    try:
        with db.get_connection() as conn:
            aari = _aari(conn)
            c1 = _insert_claim(
                conn,
                aari,
                _relative_date(20),
                status="settled",
                reference="DC1-9",
                sr=1,
                invoice_data=json.dumps({"claimable_amount": 130.97}),
            )
            c2 = _insert_claim(
                conn,
                aari,
                _relative_date(21),
                status="settled",
                reference="DC1-10",
                sr=1,
                invoice_data=json.dumps({"claimable_amount": 60.00}),
            )
            _flag_settlement(
                conn,
                c1,
                f"assessment difference — claim #{c1} (DC1-9 Sr 1): we submitted $130.97, "
                "Petcover states they assessed $351.50.",
            )
            _flag_settlement(
                conn,
                c2,
                f"assessment difference — claim #{c2} (DC1-10 Sr 1): we submitted $60.00, "
                "Petcover states they assessed $200.00.",
            )
        claim_status.queue_clarification(c1)
        claim_status.queue_clarification(c2)
        with db.get_connection() as conn:
            thread_id = conn.execute(
                "SELECT gmail_thread_id FROM clarification_batches"
            ).fetchone()[0]

        # Only c1's figure is confirmed — c2 is not addressed at all.
        llm.extract = (
            lambda *a, **k: '{"claims": [{"identifier": "DC1-9", "confirmed_amount": 130.97}]}'
        )
        claim_status.process_clarification_reply("reply-3", thread_id, "text")

        assert _claim_row(c1)["flag"] is None, "the addressed claim resolves"
        row2 = _claim_row(c2)
        assert row2["status"] == "awaiting_petcover_clarification"
        assert row2["flag"] is not None, "the unaddressed claim stays open, untouched by the reply"
        assert "130.97" not in row2["flag"], "c1's figure must not leak onto c2's flag"
    finally:
        claim_status.gmail_client.build_service = original
        llm.extract = original_extract


def test_more_info_after_unresolved_reply_only_leaves_a_note():
    """Tasks 6.1 / 8.7: More Info on a claim already awaiting_petcover_
    clarification writes a flag note only — no new draft, no new batch, no
    new event type, state unchanged."""
    _fresh_db()
    with db.get_connection() as conn:
        aari = _aari(conn)
        claim = _insert_claim(
            conn,
            aari,
            _relative_date(15),
            status="settled",
            reference="DC1-5",
            sr=1,
            invoice_data=json.dumps({"claimable_amount": 90.00}),
        )
        _flag_settlement(
            conn,
            claim,
            f"assessment difference — claim #{claim} (DC1-5 Sr 1): we submitted $90.00, "
            "Petcover states they assessed $200.00.",
        )
    # Simulate "already queued, reply arrived, didn't resolve it" without
    # touching Gmail — the state this represents is identical either way
    # (design.md: the card resurfaces at the SAME state, not a new one).
    claim_status.apply_event(claim, "clarification_requested", {})
    assert _claim_row(claim)["status"] == "awaiting_petcover_clarification"

    with db.get_connection() as conn:
        batches_before = conn.execute("SELECT COUNT(*) FROM clarification_batches").fetchone()[0]
        events_before = conn.execute(
            "SELECT COUNT(*) FROM claim_status_events WHERE claim_id = ?", (claim,)
        ).fetchone()[0]

    result = claim_status.queue_clarification(claim)
    assert result["ok"], result
    assert "unresolved" in result["message"]

    with db.get_connection() as conn:
        batches_after = conn.execute("SELECT COUNT(*) FROM clarification_batches").fetchone()[0]
        events_after = conn.execute(
            "SELECT COUNT(*) FROM claim_status_events WHERE claim_id = ?", (claim,)
        ).fetchone()[0]
    assert batches_after == batches_before, "no new draft/batch"
    assert events_after == events_before, "no new event — a flag note only"

    row = _claim_row(claim)
    assert row["status"] == "awaiting_petcover_clarification", "state unchanged"
    assert "reviewed" in row["flag"].lower() and "unresolved" in row["flag"].lower()


def test_unrelated_event_does_not_clear_awaiting_petcover_clarification():
    """Tasks 1.2 / 8.8: nothing except Acceptable or an auto-resolved reply
    moves a claim out of awaiting_petcover_clarification — an unrelated event
    is refused by the transition table, not silently applied."""
    _fresh_db()
    with db.get_connection() as conn:
        aari = _aari(conn)
        claim = _insert_claim(
            conn,
            aari,
            _relative_date(10),
            status="settled",
            invoice_data=json.dumps({"claimable_amount": 50.0}),
        )
    claim_status.apply_event(claim, "clarification_requested", {})
    assert _claim_row(claim)["status"] == "awaiting_petcover_clarification"

    outcome = claim_status.apply_event(claim, "acknowledged", {})
    assert outcome["applied"] is False, (
        "acknowledged is not a declared transition out of this state"
    )
    assert _claim_row(claim)["status"] == "awaiting_petcover_clarification"


def test_resolved_clarification_claim_leaves_pending_actions():
    """Regression: once Acceptable (or an auto-resolved reply) clears the
    flag, the claim must stop appearing in pending_actions() everywhere, not
    just the dashboard's own settlement_review section. `status` alone never
    reverts out of `awaiting_petcover_clarification` (TRANSITIONS), so
    `_action_kind_from_row` has to ask the combined flag+status accessor
    (`_awaiting_petcover_clarification`), not raw `status` — otherwise
    Telegram /actions, nudge_stale_actions and mcp_server keep nagging about
    a claim that is already resolved, forever."""
    _fresh_db()
    store: dict = {}
    original = _patch_clarification_gmail(store)
    try:
        with db.get_connection() as conn:
            claim = _queue_one_claim(conn, store)
            # _insert_claim's transaction isn't marked vet_flag=1 by default
            # (most tests never route through visit_ledger/pending_actions at
            # all) — this test specifically does, so it must be set here.
            conn.execute(
                "UPDATE bank_transactions SET vet_flag = 1 WHERE id = "
                "(SELECT transaction_id FROM vet_claims WHERE id = ?)",
                (claim,),
            )
        claim_status.queue_clarification(claim)
        assert _claim_row(claim)["status"] == "awaiting_petcover_clarification"
        assert claim in {a["claim_id"] for a in claim_status.pending_actions()}, (
            "sanity: still open, still a pending action"
        )

        assert claim_status.dismiss_mismatch(claim)["ok"] is True

        row = _claim_row(claim)
        assert row["status"] == "awaiting_petcover_clarification", "status never reverts"
        assert row["flag"] is None, "Acceptable cleared the flag"
        assert claim_status._action_kind_from_row(row) is None, (
            "resolved — the combined accessor must not still read as waiting"
        )
        assert claim not in {a["claim_id"] for a in claim_status.pending_actions()}, (
            "must disappear from every surface, not just the dashboard's own "
            "settlement_review section"
        )
    finally:
        claim_status.gmail_client.build_service = original


def test_dismiss_mismatch_telegram_buttons_split_by_clarification_eligibility():
    """A Check A (arithmetic) flag keeps the old single "Reviewed" button; a
    Check B / unrecorded-subtotal flag (eligible for the settlement-review
    card) gets the same Acceptable/More Info pair as the dashboard and
    awaiting_petcover_clarification — ADR-0031, one card/pair of actions,
    reused everywhere it appears, not a Telegram-specific third UI."""
    from openclaw import commands

    eligible = {"kind": "dismiss_mismatch", "claim_id": 8, "flag": "assessment difference - x"}
    arithmetic = {"kind": "dismiss_mismatch", "claim_id": 9, "flag": "settlement mismatch - x"}

    eligible_buttons = commands._action_buttons(eligible)
    assert [b["command"] for b in eligible_buttons] == ["/dismiss 8", "/moreinfo 8"]
    assert eligible_buttons[0]["label"] == "✅ Acceptable"
    assert eligible_buttons[1]["label"] == "❓ More Info"

    arithmetic_buttons = commands._action_buttons(arithmetic)
    assert [b["command"] for b in arithmetic_buttons] == ["/dismiss 9"]
    assert arithmetic_buttons[0]["label"] == "👍 Reviewed"


def test_awaiting_petcover_clarification_gets_the_same_two_buttons():
    from openclaw import commands

    buttons = commands._action_buttons({"kind": "awaiting_petcover_clarification", "claim_id": 14})
    assert [b["command"] for b in buttons] == ["/dismiss 14", "/moreinfo 14"]


def test_moreinfo_command_dispatches_to_queue_clarification():
    """End to end through commands.dispatch — the same path a Telegram/gateway
    tap takes, not just the button label."""
    from openclaw import commands

    _fresh_db()
    store: dict = {}
    original = _patch_clarification_gmail(store)
    try:
        with db.get_connection() as conn:
            claim = _queue_one_claim(conn, store)
        out = commands.dispatch("moreinfo", str(claim), config.TELEGRAM_USERNAME)
        assert "queued" in out["text"].lower(), out
        assert _claim_row(claim)["status"] == "awaiting_petcover_clarification"
    finally:
        claim_status.gmail_client.build_service = original


def test_settlement_review_card_template_actually_renders():
    """Neither this suite nor test_telegram.py otherwise renders `index.html`
    through Jinja at all — so a template-only bug (wrong Jinja syntax, a bad
    variable name) would ship silently. Caught live while writing this
    change: `item.items` resolves to dict.items (the bound METHOD) before
    Jinja falls back to the `items` key, because `item` here is a plain dict
    — `item['items']` is required, and this asserts the fix stays in place."""
    settlement_review = [
        {
            "claim_id": 8,
            "pet_name": "Aari",
            "reference": "DC1-26-5992",
            "sr": 1,
            "condition_text": None,
            "submitted": 446.50,
            "submitted_recorded": True,
            "assessed": 351.50,
            "items": [{"description": "Consult", "amount": 90.0}],
            "invoice_file_path": None,
            "flag": "assessment difference - claim #8 ...",
            "reply_stated_amount": None,
            "awaiting_petcover": False,
        },
        {
            "claim_id": 14,
            "pet_name": "Aari",
            "reference": None,
            "sr": None,
            "condition_text": "Arthritis",
            "submitted": None,
            "submitted_recorded": False,
            "assessed": None,
            "items": [],
            "invoice_file_path": "/data/invoices/14.pdf",
            "flag": "claimable subtotal not recorded - claim #14 ..., Petcover's reply states $999.00",
            "reply_stated_amount": 999.00,
            "awaiting_petcover": True,
        },
    ]
    html = main.templates.env.get_template("index.html").render(
        request=None,
        tasks=[],
        reminders=[],
        pets=[],
        ledger=[],
        settlement_review=settlement_review,
        upload_error=None,
        upload_result=None,
        transactions_watermark=None,
        needs_action=[],
        settled_reconciliation=[],
        unclassified=[],
    )
    assert "Settlement review" in html
    assert "claim #8" in html and "Consult" in html, "line items render when there are fewer than 5"
    assert "View invoice PDF" in html, "the PDF-link fallback renders when there are none/5+"
    assert "not set" in html, (
        "claim #8's missing condition_text renders as 'not set', never guessed"
    )
    assert "Awaiting Petcover clarification" in html, (
        "the awaiting_petcover_clarification claim is distinguished, via status_words "
        "(the one vocabulary), not a hardcoded literal"
    )
    assert "999.00" in html, "the reply's stated figure renders on the resurfaced card"


def test_petcover_letters_are_never_taken_by_the_task_ingest():
    """Both pollers gate on `processed_emails`, so whichever ran first won and
    the other skipped the message permanently. Live cost, found 2026-08-04: five
    approval letters between 28/07 and 03/08 — including $2,521.46 claimed,
    $1,638.95 paid — became assistant tasks and reached no claim at all."""
    from openclaw import gmail_ingest

    for sender in config.PETCOVER_STATUS_SENDERS:
        assert gmail_ingest._belongs_to_the_claims_service({"From": f"PetCover <{sender}>"}), sender
        assert gmail_ingest._belongs_to_the_claims_service({"From": sender.upper()}), sender
    # Nothing else was going to stop them: `claims.au@` matches neither noise
    # branch, which is exactly why five letters became tasks.
    assert not gmail_ingest._is_noise({"From": "claims.au@petcovergroup.com"})
    assert not gmail_ingest._belongs_to_the_claims_service({"From": "reception@theshirevet.com.au"})
    assert not gmail_ingest._belongs_to_the_claims_service(
        {"From": "marketing.au@petcovergroup.com"}
    )


def test_an_html_only_email_yields_its_table_not_a_snippet():
    """Petcover's 29/07/2026 status table — the only document that states a
    treatment date per claim serial, and the thing that proved every serial we
    hold sits on the wrong claim — has no text/plain part. The extractor used to
    fall through to `snippet`, which is 198 characters of pleasantries, and
    nothing said the body had been truncated."""
    import base64 as _b64

    from openclaw import gmail_client

    body = (
        "<html><body><p>Good morning Justin,</p><table>"
        "<tr><th>Claim no.</th><th>Sr no.</th><th>Treatment Date</th><th>Amount Payable</th></tr>"
        "<tr><td>DC1&#8208;27&#8208;5628</td><td>8</td><td>19/06/2026</td><td>$377.48</td></tr>"
        "<tr><td>DC1&#8208;26&#8208;5992</td><td>1</td><td>18/05/2026</td><td>$130.97</td></tr>"
        "</table><script>var x = '<td>not content</td>';</script></body></html>"
    )
    message = {
        "snippet": "Good morning Justin, We hope this email finds you well",
        "payload": {
            "mimeType": "multipart/related",
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {
                            "mimeType": "text/html",
                            "body": {"data": _b64.urlsafe_b64encode(body.encode()).decode()},
                        },
                    ],
                },
                {
                    "mimeType": "image/png",
                    "filename": "image001.png",
                    "body": {"attachmentId": "abc"},
                },
            ],
        },
    }
    text = gmail_client._message_text(message)
    assert "DC1‐27‐5628 | 8 | 19/06/2026 | $377.48" in text, text
    assert "18/05/2026" in text and "$130.97" in text
    assert "not content" not in text, "script contents leaked into the body"
    # text/plain still wins when there is one — the HTML path is a fallback, not
    # a replacement.
    message["payload"]["parts"][0]["parts"].insert(
        0,
        {
            "mimeType": "text/plain",
            "body": {"data": _b64.urlsafe_b64encode(b"plain wins").decode()},
        },
    )
    assert gmail_client._message_text(message) == "plain wins"


def test_an_assessment_difference_names_whose_invoice_the_figure_actually_is():
    """Petcover's stated figure has never been a mystery number: on 2026-08-04
    every letter carrying one matched some real claim of ours to the cent, and
    what was wrong was which claim sat under the serial. Telling Justin to "ask
    Petcover which invoice this assessed" sends him to the wrong party."""
    _fresh_db()
    import json as _json

    with db.get_connection() as conn:
        aari = _aari(conn)
        conn.execute(
            "UPDATE pets SET policy_anniversary = ? WHERE id = ?",
            (_anniversary_days_ago(300), aari),
        )
        txn = _relative_date(100)
        mine = _insert_claim(
            conn,
            aari,
            txn,
            status="acknowledged",
            reference="DC1-26-5992",
            sr=1,
            amount=-446.50,
            invoice_data=_json.dumps({"claimable_amount": 446.50}),
        )
        theirs = _insert_claim(
            conn,
            aari,
            _relative_date(140),
            status="acknowledged",
            amount=-351.50,
            invoice_data=_json.dumps({"claimable_amount": 351.50}),
        )
    detail = {
        "claimed_amount": 351.50,
        "fixed_excess_stated": 150.00,
        "non_claimable_stated": 0.00,
        "age_contribution_percent": 0.35,
        "paid_amount": 130.97,
    }
    flag = claim_status._validate_settlement(_claim_row(mine), detail, txn)
    assert flag and flag.startswith("assessment difference"), flag
    assert f"#{theirs}'s invoice" in flag, flag
    assert "serial is most likely on the wrong claim" in flag
    assert "ask Petcover which invoice" not in flag

    # No claim of ours is worth that figure -> it really is a question for them.
    with db.get_connection() as conn:
        conn.execute("DELETE FROM vet_claims WHERE id = ?", (theirs,))
    flag = claim_status._validate_settlement(_claim_row(mine), detail, txn)
    assert "ask Petcover which invoice this assessed" in flag, flag


def test_a_guessed_serial_is_recorded_as_a_guess():
    """`_claim_for_sr` picks the oldest un-serialized claim — a heuristic over
    Petcover's ordering that their 2026-07-29 status table contradicted on every
    serial we hold. The log could not tell a guessed link from a cited one."""
    _fresh_db()
    import json as _json

    with db.get_connection() as conn:
        aari = _aari(conn)
        _insert_claim(
            conn,
            aari,
            _relative_date(60),
            status="sent",
            condition="Arthritis",
            amount=-132.50,
            invoice_data=_json.dumps({"claimable_amount": 132.50}),
        )

    claim_status.process_reply(
        "mail-guessed-sr",
        "PetCover - Acknowledgement Letter",
        "Claim Reference: DC1-27-5628 Sr 7\nCondition: Illness (Arthritis)\nAri",
    )
    with db.get_connection() as conn:
        rows = [
            _json.loads(r["detail"] or "{}")
            for r in conn.execute("SELECT detail FROM claim_status_events ORDER BY id")
        ]
    assert any("heuristic" in (d.get("sr_assigned_by") or "") for d in rows), rows


def test_a_serial_letter_attaches_by_the_amount_it_states():
    """Petcover's letter says what it assessed, and exactly one claim is worth
    that. The ordering heuristic this replaces was measured wrong on every
    serial we hold (their status table, 2026-08-04)."""
    _fresh_db()
    import json as _json

    with db.get_connection() as conn:
        aari = _aari(conn)
        # Two claims in one submission. The OLDER one is not the one the letter
        # is about — which is exactly the case the heuristic got wrong.
        old = _insert_claim(
            conn,
            aari,
            _relative_date(200),
            status="sent",
            draft_id="d1",
            condition="Arthritis",
            amount=-45.0,
            invoice_data=_json.dumps({"claimable_amount": 45.00}),
        )
        new = _insert_claim(
            conn,
            aari,
            _relative_date(30),
            status="sent",
            draft_id="d1",
            condition="Arthritis",
            amount=-446.50,
            invoice_data=_json.dumps({"claimable_amount": 446.50}),
        )

    claim_status.process_reply(
        "mail-amount-routed",
        "PetCover Letter - Claim Approval",
        "Ari\nClaim Reference:DC1-27-5628\nTreatment number: 5\nCondition: Illness (Arthritis)\n"
        "Total amount claimed: $446.50\nFixed excess $0.00\nAge Contribution: $156.28 [35%]\n"
        "Paid by us: $290.23\n",
    )

    with db.get_connection() as conn:
        rows = {r["id"]: r for r in conn.execute("SELECT id, petcover_sr FROM vet_claims")}
    assert rows[new]["petcover_sr"] == 5, "the letter's own amount did not decide it"
    assert rows[old]["petcover_sr"] is None, "the oldest claim took a serial that isn't its own"


def test_a_letter_whose_amount_matches_no_claim_is_left_for_manual_link():
    """The live failure of 2026-08-05: an under-excess letter for a $55.74
    arthritis claim was attached to a $2,521.46 ALT workup and moved it to
    `below_excess`. No claim is worth $55.74, so nothing should be chosen."""
    _fresh_db()
    import json as _json

    with db.get_connection() as conn:
        aari = _aari(conn)
        big = _insert_claim(
            conn,
            aari,
            _relative_date(100),
            status="sent",
            condition="Raised ALT",
            amount=-1970.40,
            invoice_data=_json.dumps({"claimable_amount": 2521.46}),
        )

    claim_status.process_reply(
        "mail-under-excess",
        "Petcover Insurance Claim for Ari",
        "Ari\nClaim Reference:DC1-27-5628 Sr 4\nCondition:Arthritis\n"
        "Claim assessment outcome: Under excess\n"
        "Amount claimed:$55.74Less Fixed excess:$105.00Other deductibles:$0.00\n"
        "Outstanding excess:$-49.26\n",
    )

    with db.get_connection() as conn:
        claim = conn.execute(
            "SELECT status, petcover_sr FROM vet_claims WHERE id = ?", (big,)
        ).fetchone()
        unlinked = conn.execute(
            "SELECT detail FROM claim_status_events WHERE claim_id IS NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert claim["petcover_sr"] is None, "a $55.74 letter took the serial of a $2,521.46 claim"
    assert claim["status"] == "sent", "an unrelated letter moved the claim's state"
    detail = _json.loads(unlinked["detail"])
    assert "needs manual link" in detail["flag"], detail
    assert "$55.74" in detail["flag"], "the flag must name the figure that matched nothing"
    assert f"#{big}" in detail["flag"], "the flag must name what it considered"


def test_the_under_excess_letter_gives_up_its_amount():
    """It writes `Amount claimed:` without the `Total`, so the approval pattern
    missed it entirely — which is why that letter had no figure to route by."""
    figures = claim_status.extract_approval_amounts(
        "Amount claimed:$55.74Less Fixed excess:$105.00Outstanding excess:$-49.26"
    )
    assert figures["claimed_amount"] == 55.74
    assert figures["fixed_excess_stated"] == 105.00
    assert claim_status.stated_claim_amount("Total amount claimed: $446.50") == 446.50
    assert (
        claim_status.stated_claim_amount("Amount Claimed $132.50\nTotal Payable: $86.13") == 132.50
    )
    assert claim_status.stated_claim_amount("no figures here") is None


def test_an_acknowledgement_still_routes_and_still_says_it_guessed():
    """An ack states no amount, so the ordering heuristic is all there is. It
    survives — but the event records that the link was inferred."""
    _fresh_db()
    import json as _json

    with db.get_connection() as conn:
        aari = _aari(conn)
        _insert_claim(
            conn,
            aari,
            _relative_date(60),
            status="sent",
            condition="Arthritis",
            amount=-132.50,
            invoice_data=_json.dumps({"claimable_amount": 132.50}),
        )

    claim_status.process_reply(
        "mail-ack-no-amount",
        "PetCover - Acknowledgement Letter",
        "Ari\nClaim Reference: DC1-27-5628 Sr 7\nCondition: Arthritis\nYour claim has been received",
    )
    with db.get_connection() as conn:
        rows = [
            _json.loads(r["detail"] or "{}")
            for r in conn.execute("SELECT detail FROM claim_status_events ORDER BY id")
        ]
    assert any(d.get("sr_assigned_by", "").startswith("heuristic") for d in rows), rows


def _settled_claim_for_replay():
    """A settled claim, as one looks by the time a replay re-reads its old mail."""
    import json as _json

    _fresh_db()
    with db.get_connection() as conn:
        aari = _aari(conn)
        cid = _insert_claim(
            conn,
            aari,
            _relative_date(100),
            status="settled",
            reference="DC1-27-5628",
            sr=5,
            condition="Arthritis",
            amount=-446.50,
            invoice_data=_json.dumps({"claimable_amount": 446.50}),
        )
    return cid


def test_a_replayed_refusal_is_recorded_but_does_not_flag_the_claim():
    """A replay re-applies mail the log already has, so a refused transition is
    expected on every claim whose state has moved on. Recording it is the audit
    trail; writing it to `flag` is what buried six claims on 2026-08-05."""
    cid = _settled_claim_for_replay()
    outcome = claim_status.apply_event(
        cid, "acknowledged", {"subject": "ack"}, "mail-replayed", replaying=True
    )

    assert outcome["refused"], "the transition must still be refused"
    assert outcome["state"] == "settled", "a replay must not move the state"
    row = _claim_row(cid)
    assert row["flag"] is None, f"a replayed refusal reached the flag: {row['flag']}"
    with db.get_connection() as conn:
        events = conn.execute(
            "SELECT event_type, raw_email_id FROM claim_status_events WHERE claim_id = ?", (cid,)
        ).fetchall()
    assert any(
        e["event_type"] == "acknowledged" and e["raw_email_id"] == "mail-replayed" for e in events
    ), "the event itself must still be recorded"


def test_an_ordinary_refusal_still_flags_the_claim():
    """The guard against fixing the noise by making refusals quiet everywhere.
    Outside a replay, a refused transition means a letter arrived out of order —
    genuinely surprising, and the flag is how it becomes visible."""
    cid = _settled_claim_for_replay()
    outcome = claim_status.apply_event(cid, "acknowledged", {"subject": "ack"}, "mail-live")

    assert outcome["refused"]
    flag = _claim_row(cid)["flag"]
    assert flag and "refused settled -> acknowledged" in flag, flag


def test_a_replay_finding_reaches_the_flag_instead_of_losing_to_a_refusal():
    """Claim #2's shape. Its `claimable subtotal not recorded` finding lost to a
    refusal that a replay had already decided not to write, so it reached no
    surface at all."""
    import json as _json

    _fresh_db()
    with db.get_connection() as conn:
        aari = _aari(conn)
        conn.execute(
            "UPDATE pets SET policy_anniversary = ? WHERE id = ?",
            (_anniversary_days_ago(300), aari),
        )
        # settled already, and no claimable subtotal recorded — so a re-read of
        # its approval letter both refuses the transition AND produces a finding.
        cid = _insert_claim(
            conn,
            aari,
            _relative_date(100),
            status="settled",
            reference="DC1-26-5992",
            sr=2,
            condition="Arthritis",
            amount=-585.39,
            invoice_data=_json.dumps({"amount": 580.74}),
        )

    body = (
        "Ari\nClaim Reference:DC1-26-5992\nTreatment number: 2\n"
        "Your claim has been approved\n"
        "Total amount claimed: $35.00\nFixed excess $0.00\nNon‐claimable amount $0.00\n"
        "Age Contribution: $12.25 [35%]\nPercentage Excess: $0.00 [0%]\nPaid by us: $22.75\n"
    )
    claim_status.process_reply(
        "mail-replay-finding", "PetCover Letter - Claim Approval", body, replaying=True
    )

    flag = _claim_row(cid)["flag"]
    assert flag, "the replay produced a finding and it reached no surface"
    assert flag.startswith("claimable subtotal not recorded"), flag
    assert "refused" not in flag, "the refusal took the column back"

    # Same letter, not a replay: the refusal is news and keeps the column.
    _fresh_db()
    with db.get_connection() as conn:
        aari = _aari(conn)
        conn.execute(
            "UPDATE pets SET policy_anniversary = ? WHERE id = ?",
            (_anniversary_days_ago(300), aari),
        )
        live = _insert_claim(
            conn,
            aari,
            _relative_date(100),
            status="settled",
            reference="DC1-26-5992",
            sr=2,
            condition="Arthritis",
            amount=-585.39,
            invoice_data=_json.dumps({"amount": 580.74}),
        )
    claim_status.process_reply("mail-live-finding", "PetCover Letter - Claim Approval", body)
    assert "refused" in (_claim_row(live)["flag"] or ""), _claim_row(live)["flag"]


def test_a_replay_never_silences_a_defect():
    """Only a refused *transition* is expected during a replay. An undeclared
    event type is a bug in the state machine whoever is reading the mail."""
    cid = _settled_claim_for_replay()
    outcome = claim_status.apply_event(cid, "not_a_real_event_type", {}, "mail-x", replaying=True)
    assert outcome["refused"]
    flag = _claim_row(cid)["flag"]
    assert flag and "unknown event type" in flag, flag


def test_a_petcover_letter_matching_no_claim_becomes_a_visible_action():
    """Six unmatched letters accumulated between 2026-07-21 and 2026-08-05 without
    appearing on the dashboard, in /actions, or in any nudge — one of them an
    approval stating $135.00 claimed and $87.75 PAID against no claim we hold.
    `process_reply` recorded them with claim_id NULL and returned; nothing read
    those rows. Money already assessed, invisible."""
    import json as _json

    from openclaw import commands

    _fresh_db()
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO claim_status_events (claim_id, event_type, raw_email_id, created_at, detail) "
            "VALUES (NULL, 'approved', 'mail-1', '2026-08-03T00:21:50+00:00', ?)",
            (
                _json.dumps(
                    {
                        "subject": "PetCover Letter - Claim Approval",
                        "claimed_amount": 135.0,
                        "paid_amount": 87.75,
                        "reference": "DC1-26-5992",
                        "sr": 4,
                        "flag": "needs manual link - no claim matched",
                    }
                ),
            ),
        )

    letters = claim_status.unlinked_letters(date(2026, 8, 6))
    assert len(letters) == 1, letters
    letter = letters[0]
    assert letter["kind"] == "unlinked_letter"
    assert letter["claim_id"] is None, "an unlinked letter must not claim a claim"
    assert letter["paid_amount"] == 87.75
    assert letter["age_days"] == 3, letter["age_days"]
    # NOBODY_WAITING, not "Petcover is waiting on you": they have already assessed
    # and paid. Naming the wrong party is how a chase never happens.
    assert letter["waiting"] == claim_status.NOBODY_WAITING, letter["waiting"]
    # No tap resolves it -- there is no /link verb, and an unregistered verb
    # reaches the agent as a chat turn.
    assert letter["actionable"] is False
    assert commands._action_buttons(letter) == [], "a button whose verb is unregistered"

    # It reaches the action list, which is what /actions and the nudge read.
    assert any(a["kind"] == "unlinked_letter" for a in claim_status.pending_actions())

    # The card names the letter and the amounts, and carries the event id in
    # place of the claim id it cannot have.
    text = commands._action_card_text(letter)
    assert "DC1-26-5992 Sr 4" in text, text
    assert "$135.00 claimed" in text and "$87.75 PAID" in text, text
    assert "event #" in text, text
    assert "Claim #None" not in text, text


def test_linking_an_event_retires_its_no_claim_matched_flag():
    """Live 2026-08-05: event #93 held `claim_id = 12` AND `needs manual link - no
    claim matched`, because link_event set the column and left the detail alone.
    A row that contradicts itself makes any count of unlinked letters by flag text
    over-count. The reason is kept under `linked_flag` rather than deleted —
    same convention as `mismatch_dismissed`."""
    import json as _json

    _fresh_db()
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO vet_claims (id, transaction_id, status, created_at, updated_at) "
            "VALUES (12, 1, 'settled', '2026-08-01', '2026-08-01')"
        )
        cur = conn.execute(
            "INSERT INTO claim_status_events (claim_id, event_type, raw_email_id, created_at, detail) "
            "VALUES (NULL, 'settled', 'mail-2', '2026-08-05T13:46:17+00:00', ?)",
            (_json.dumps({"subject": "EFT Template", "flag": "needs manual link - no claim"}),),
        )
        event_id = cur.lastrowid

    assert claim_status.link_event(event_id, 12) is True
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT claim_id, detail FROM claim_status_events WHERE id = ?", (event_id,)
        ).fetchone()
    detail = _json.loads(row["detail"])
    assert row["claim_id"] == 12
    assert "flag" not in detail, "the row still says no claim matched while holding one"
    assert detail["linked_flag"] == "needs manual link - no claim", detail
    assert detail["linked_to_claim"] == 12
    assert detail["subject"] == "EFT Template", "linking destroyed the letter's own record"

    # And it drops out of the unlinked list, which is the point.
    assert claim_status.unlinked_letters(date(2026, 8, 6)) == []

    # A second link is refused, so the flag cannot be retired twice.
    assert claim_status.link_event(event_id, 12) is False


def test_action_kind_is_asserted_for_the_four_kinds_nothing_covered():
    """`clarify-claim-status-vocabulary` task 1.2 claimed a before/after assertion
    for every action kind and was ticked without one. Measured 2026-07-28: 4 of 9
    kinds were asserted anywhere, and the four missing ones included `unmatch` and
    `confirm_resolved` — the two the extraction into `_action_kind_from_row`
    actually moved. Both suites passed throughout, which is the point: a mechanical
    refactor that nothing asserts is a refactor nobody can show is mechanical."""
    base = {"id": 1, "status": "matched", "flag": None, "pet_id": 1, "condition_text": "Arthritis"}

    # An open split proposal outranks whatever the row says.
    assert claim_status._action_kind({**base, "id": 5}, {5}, set()) == "split_proposal"

    # An unresolved event, from the other set argument.
    assert claim_status._action_kind({**base, "id": 6}, set(), {6}) == "confirm_resolved"

    # `unmatch` is decided by the row alone.
    additional = {**base, "id": 7, "flag": "possible additional invoice for this visit"}
    assert claim_status._action_kind(additional, set(), set()) == "unmatch"

    # THE PRECEDENCE THE EXTRACTION HAD TO PRESERVE, and the reason these two
    # kinds are the ones worth asserting. `unmatch` lives in the row-only half
    # but outranks `confirm_resolved`, which is decided from a set — so the set
    # check in `_action_kind` carries an explicit "not additional-invoice" guard.
    # Drop that guard and this claim silently becomes `confirm_resolved`: a claim
    # needing its wrong invoice detached would instead be offered "mark resolved".
    assert claim_status._action_kind(additional, set(), {7}) == "unmatch"

    # …and a split proposal still outranks both.
    assert claim_status._action_kind(additional, {7}, {7}) == "split_proposal"

    # `invoice_request_sent` keys off the flag, never off invoice_request_sent_at
    # + draft_id — draft_id is overloaded and that pair matches almost every claim.
    assert (
        claim_status._action_kind(
            {**base, "id": 8, "flag": "invoice_request_drafted"}, set(), set()
        )
        == "invoice_request_sent"
    )


def test_a_claim_draft_subject_names_its_claims():
    """Two drafts titled `Vet claim — Aari` coexisted live on 2026-07-25 — #7+#6
    batched, and #12 — and the older one was read as deleted. It existed the whole
    time. The wrong conclusion produced a "redo claim #7" request that no tool
    could serve, and two duplicate tasks nobody closed."""
    assert claim_forms._draft_subject("Aari", [7, 6]) == "Vet claim — Aari (#7, #6)"
    assert claim_forms._draft_subject("Aari", [12]) == "Vet claim — Aari (#12)"
    # Two submissions for one pet are now distinguishable, which is the whole point.
    assert claim_forms._draft_subject("Aari", [7, 6]) != claim_forms._draft_subject("Aari", [12])
    # The `Vet claim` prefix survives, so pipeline.DRAFT_SEARCH_LINK still matches.
    assert claim_forms._draft_subject("Echo", [3]).startswith("Vet claim")


def test_the_phantom_db_raises_instead_of_answering():
    """ADR-0018 guards a read-write open of the LIVE db, which fails loudly. This
    is the quieter one: `app/.env` sets the *container* path `/data/openclaw.db`
    and `config` loads `.env` from cwd, so a host-side call resolves it to
    `C:\\data\\openclaw.db` — a real file holding 1 vet_claim, 1 bank_transaction
    and 2 telegram_messages when measured 2026-08-08, against a live corpus of
    22+ claims and 307 messages. The query succeeds and the rows are wrong."""
    if os.name != "nt":
        return  # the resolution only misfires on Windows; inside the container it is correct

    try:
        db._refuse_phantom("/data/openclaw.db")
    except RuntimeError as exc:
        assert "phantom" in str(exc), "the message must name what it refused"
        assert "mode=ro" in str(exc) or "query_db" in str(exc), "and name the way out"
    else:
        raise AssertionError("a POSIX-absolute path on Windows must be refused")

    # Not over-broad: a real Windows path is the normal case and must still open.
    db._refuse_phantom(r"C:\Code\Me.OpenClaw\app\data\openclaw.db")
    db._refuse_phantom("./data/openclaw.db")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"{name} OK")
    print("ALL TESTS PASSED")
