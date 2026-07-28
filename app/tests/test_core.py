"""Runnable smoke checks — not a full suite. Run with: python tests/test_core.py"""

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

from openclaw import claim_forms, claim_status, config, db, gemini, invoice_matching, llm, netbank_csv, reminders, status_labels, tasks, vet_detection  # noqa: E402
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


def test_uncorrelated_reply_unlinked_then_manually_linked():
    """A reply naming no known pet correlates to nothing — stored unlinked
    (never guessed), then manual linking attaches it WITHOUT rewriting the
    claim's status (a late-linked old email must not regress a settled claim)."""
    db.init_db()
    with db.get_connection() as conn:
        aari = conn.execute("SELECT id FROM pets WHERE name='Aari'").fetchone()[0]
        claim_a = _insert_sent_claim(conn, aari, "2026-01-05", "draft-a")

    claim_status.process_reply(
        "msg-uncorr-1", "First request for consult note",
        "We recently received a claim for treatment provided to Rex. Please provide consult notes.",
    )
    with db.get_connection() as conn:
        event = conn.execute("SELECT * FROM claim_status_events WHERE raw_email_id = 'msg-uncorr-1'").fetchone()
        status_a = conn.execute("SELECT status FROM vet_claims WHERE id = ?", (claim_a,)).fetchone()[0]
    assert event["claim_id"] is None, "unknown-pet reply must not be attached to any claim"
    assert event["event_type"] == "info_requested"
    assert status_a == "sent", "uncorrelated reply must not change any claim's status"

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


def test_parse_invoices_multi_and_legacy_shapes():
    multi = '{"invoices": [{"date": "2026-06-17", "amount": 141.87, "items": []}, {"date": "2026-07-06", "amount": 407.56, "items": []}]}'
    parsed = invoice_matching._parse_invoices(multi)
    assert [i["amount"] for i in parsed] == [141.87, 407.56]
    # legacy single-invoice object (old cache rows / model regression) still parses
    legacy = '```json\n{"date": "2026-06-19", "amount": 585.39, "items": []}\n```'
    assert invoice_matching._parse_invoices(legacy) == [{"date": "2026-06-19", "amount": 585.39, "items": []}]
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
        {"date": None, "amount": 1134.82},         # grand total — over ceiling
    ]
    picked = invoice_matching._pick_invoice(invoices, -407.56, _date(2026, 7, 6))
    assert picked["amount"] == 407.56
    # nothing fits: every invoice over the ceiling
    assert invoice_matching._pick_invoice([{"date": "2026-07-06", "amount": 999.0}], -407.56, _date(2026, 7, 6)) is None
    # amount missing entirely: skipped, not crashed
    assert invoice_matching._pick_invoice([{"date": "2026-07-06", "amount": None}], -407.56, _date(2026, 7, 6)) is None
    # missing invoice date can't be checked — allowed through (absence of evidence)
    assert invoice_matching._pick_invoice([{"amount": 400.0}], -407.56, _date(2026, 7, 6))["amount"] == 400.0


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
    assert any("after:" in q and "before:" not in q for q in merchant_queries), "merchant needs an open-ended window"
    assert any("after:" in q and "before:" not in q for q in spouse_queries), "spouse forwards need an open-ended window"
    assert any("before:" in q for q in merchant_queries), "narrow window must remain (invoice can arrive before the charge settles)"
    assert all("NSW" not in q for q in merchant_queries), "state suffix must be stripped from search terms"
    # real failure: Justin's own outgoing invoice-request emails list visit
    # dates + amounts — extraction read them as invoices and 12 claims matched
    # his own requests. Own mail must be excluded query-side.
    assert all("-from:me" in q for q in merchant_queries), "own sent mail must never be an invoice candidate"


def test_extraction_cached_per_email_no_second_llm_call():
    db.init_db()
    calls = []
    original_extract = llm.extract
    llm.extract = lambda *a, **k: calls.append(1) or '{"invoices": [{"date": "2026-01-20", "amount": 10.50, "items": []}]}'
    try:
        first = invoice_matching._invoices_for_email("cache-test-1", "some invoice text")
        llm.extract = lambda *a, **k: (_ for _ in ()).throw(AssertionError("second extraction must come from cache"))
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
    assert invoice_matching._cached_extraction("cache-test-2") is None, "failed parse must not be cached"


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
    assert invoice_matching._oversized_candidate([{"date": None, "amount": 9999.0}], -551.06, _date(2026, 4, 13)) is None


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
            "WHERE vet_claims.id = ?", (claim_a,),
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
        proposal = conn.execute("SELECT status FROM split_proposals WHERE id = ?", (proposal["id"],)).fetchone()
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
            ("email-m-1", _json.dumps({"date": "2026-04-13", "amount": 2521.46, "items": []}),
             _json.dumps([claim_small, claim_large]), datetime.now(timezone.utc).isoformat()),
        )
        pid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    result = invoice_matching.merge_split_proposal(pid)
    assert result["ok"], result["message"]
    with db.get_connection() as conn:
        large = conn.execute("SELECT status FROM vet_claims WHERE id = ?", (claim_large,)).fetchone()[0]
        small = conn.execute("SELECT status FROM vet_claims WHERE id = ?", (claim_small,)).fetchone()[0]
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
        assert conn.execute("SELECT COUNT(*) FROM telegram_messages WHERE update_id = 9001").fetchone()[0] == 1


def _edited_reply_to_card_update(update_id=9401, text="This is actually split between echo and Aari. "
                                                     "Aari cost was $35 out of this"):
    """The real 2026-07-27 payload shape (telegram_messages id 96): Justin EDITED
    his message, and it was a reply to the ASSIGN PET card for claim #1."""
    from telegram import Update

    from openclaw import config

    chat = {"id": 8995277418, "type": "private", "username": "jagberg"}
    return Update.de_json({
        "update_id": update_id,
        "edited_message": {
            "message_id": 233,
            "date": 1785149076,
            "edit_date": 1785149753,
            "chat": chat,
            "from": {"id": 1, "is_bot": False, "first_name": "Justin",
                     "username": config.TELEGRAM_USERNAME or "jagberg"},
            "text": text,
            "reply_to_message": {
                "message_id": 227,
                "date": 1785148834,
                "chat": chat,
                "from": {"id": 2, "is_bot": True, "first_name": "BettyVet", "username": "bettyvet_bot"},
                "text": "🐾 ASSIGN PET\nClaim #1 · The Shire Veterinary Ca… · $407.56\n"
                        "2026-07-06 (21d ago)\nBlocks: the claim can't be filled",
                "reply_markup": {"inline_keyboard": [
                    [{"text": "Aari", "callback_data": "setpet:1:1"}],
                    [{"text": "Echo", "callback_data": "setpet:1:2"}],
                ]},
            },
        },
    }, bot=None)


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
    assert message_log._describe(update) == (
        "text", "edit: This is actually split between echo and Aari. Aari cost was $35 out of this"
    ), message_log._describe(update)
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
        return Update.de_json({
            "update_id": 9402,
            "message": {"message_id": 2, "date": 1785149076, "chat": chat, "text": "do it",
                        "from": {"id": 1, "is_bot": False, "first_name": "J"},
                        "reply_to_message": {**card, "message_id": 1, "date": 1785148834, "chat": chat,
                                             "from": {"id": 2, "is_bot": True, "first_name": "B"}}},
        }, bot=None).effective_message

    two_claims = _reply_to({"text": "SEND GMAIL DRAFT\nS6+7 · 2 claims\n  • Claim #6 …\n  • Claim #7 …"})
    assert telegram_bot._replied_to_claim_id(two_claims) is None, "two ids named → no target"

    proposal_token = _reply_to({"text": "Confirm this?", "reply_markup": {"inline_keyboard": [
        [{"text": "✅ Confirm", "callback_data": "act:2"}]]}})
    assert telegram_bot._replied_to_claim_id(proposal_token) is None, "act: token is not a claim id"

    from_caption = _reply_to({"caption": "Review this settlement for claim #21",
                              "document": {"file_id": "f", "file_unique_id": "u"}})
    assert telegram_bot._replied_to_claim_id(from_caption) == 21, "PDF alerts carry it in the caption"

    no_parent = Update.de_json({"update_id": 9403, "message": {
        "message_id": 3, "date": 1785149076, "chat": {"id": 1, "type": "private"}, "text": "hi",
        "from": {"id": 1, "is_bot": False, "first_name": "J"}}}, bot=None).effective_message
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
    config.TELEGRAM_BOT_TOKEN = "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"  # never used, never dialled
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
        conn.execute("UPDATE telegram_messages SET received_at = ? WHERE update_id = 9201", (stale,))

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
    message_log.record_outbound("send_message", "Claim #2 marked sent", {"chat_id": 1, "text": "Claim #2 marked sent"})

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
    assert pipeline._is_transient(HttpError(type("R", (), {"status": 503, "reason": "busy"})(), b""))
    assert not pipeline._is_transient(HttpError(type("R", (), {"status": 404, "reason": "gone"})(), b""))
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
    original_alive, original_send = telegram_bot.polling_alive, telegram_bot.send_message_sync
    telegram_bot.send_message_sync = lambda msg: sent.append(msg)
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
        telegram_bot.send_message_sync = original_send


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
            ("email-r-1", _json.dumps({"date": "2026-04-13", "amount": 2521.46}),
             _json.dumps([claim_a, claim_b]), datetime.now(timezone.utc).isoformat()),
        )
        pid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    result = invoice_matching.reject_split_proposal(pid)
    assert result["ok"]
    with db.get_connection() as conn:
        flags = [r[0] for r in conn.execute(
            "SELECT flag FROM vet_claims WHERE id IN (?, ?)", (claim_a, claim_b))]
        status = conn.execute("SELECT status FROM split_proposals WHERE id = ?", (pid,)).fetchone()[0]
    assert status == "rejected"
    assert all(f and "match this charge manually" in f for f in flags)
    # a rejected pair must never be re-proposed
    with db.get_connection() as conn:
        claim_row = conn.execute(
            "SELECT vet_claims.*, bank_transactions.amount AS txn_amount, "
            "bank_transactions.merchant AS txn_merchant FROM vet_claims "
            "JOIN bank_transactions ON bank_transactions.id = vet_claims.transaction_id "
            "WHERE vet_claims.id = ?", (claim_a,),
        ).fetchone()
    oversized = {"date": "2026-04-13", "amount": 2521.46, "_email_id": "email-r-1"}
    assert invoice_matching._propose_split(claim_row, oversized) is None, "rejected pair must not re-flag as a merge"
    with db.get_connection() as conn:
        assert conn.execute("SELECT count(*) FROM split_proposals").fetchone()[0] == 1, "no new proposal after reject"


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
            "WHERE vet_claims.id = ?", (claim_a,),
        ).fetchone()
    # real payment-section shape: 'Eftpos/Visa/Mastercard : -1970.40'
    text_amounts = invoice_matching._text_amounts(
        "Total: $2521.46 Payment method: Eftpos/Visa/Mastercard : -1970.40 Eftpos/Visa/Mastercard : -551.06"
    )
    oversized = {"date": "2026-04-13", "amount": 2521.46, "_email_id": "email-p-1", "_text_amounts": text_amounts}
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
            "WHERE vet_claims.id = ?", (claim_a,),
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
            ("email-n-1", _json.dumps({"date": "2026-04-13", "amount": 2521.46}),
             _json.dumps([claim_a, claim_b]), datetime.now(timezone.utc).isoformat()),
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

    originals = (pipeline.vet_detection.classify_unflagged, pipeline.reconcile_sent_invoice_requests,
                 pipeline.invoice_matching.match_claim, pipeline._maybe_draft_invoice_request,
                 pipeline.poll_petcover_status, pipeline.notify_claim_states, pipeline._ensure_gmail_auth)
    pipeline.vet_detection.classify_unflagged = lambda: stages.append("classify")
    pipeline._ensure_gmail_auth = lambda: True
    pipeline.reconcile_sent_invoice_requests = lambda: stages.append("reconcile")
    pipeline.invoice_matching.match_claim = fake_match
    pipeline._maybe_draft_invoice_request = lambda claim: stages.append(f"draft:{claim['id']}")
    pipeline.poll_petcover_status = lambda: stages.append("poll")
    pipeline.notify_claim_states = lambda: stages.append("notify")
    try:
        pipeline.run_once()
    finally:
        (pipeline.vet_detection.classify_unflagged, pipeline.reconcile_sent_invoice_requests,
         pipeline.invoice_matching.match_claim, pipeline._maybe_draft_invoice_request,
         pipeline.poll_petcover_status, pipeline.notify_claim_states, pipeline._ensure_gmail_auth) = originals

    assert attempted == [claim_a, claim_b], "claim B must still be attempted after claim A crashes"
    assert "poll" in stages and "notify" in stages, "downstream stages must run despite the failure"
    assert f"draft:{claim_b}" in stages, "claim B continues through the normal no-match path"
    with db.get_connection() as conn:
        flag_a = conn.execute("SELECT flag FROM vet_claims WHERE id = ?", (claim_a,)).fetchone()[0]
    assert flag_a and flag_a.startswith("invoice matching error"), "failure must be visible on the claim"


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

    originals = (pipeline.vet_detection.classify_unflagged, pipeline.reconcile_sent_invoice_requests,
                 pipeline.invoice_matching.match_claim, pipeline._maybe_draft_invoice_request,
                 pipeline.poll_petcover_status, pipeline.notify_claim_states, pipeline._ensure_gmail_auth)
    pipeline.vet_detection.classify_unflagged = lambda: None
    pipeline._ensure_gmail_auth = lambda: True
    pipeline.reconcile_sent_invoice_requests = lambda: None
    pipeline.invoice_matching.match_claim = unavailable_match
    pipeline._maybe_draft_invoice_request = lambda claim: None
    pipeline.poll_petcover_status = lambda: stages.append("poll")
    pipeline.notify_claim_states = lambda: stages.append("notify")
    try:
        pipeline.run_once()
        with db.get_connection() as conn:
            flag_a = conn.execute("SELECT flag FROM vet_claims WHERE id = ?", (claim_a,)).fetchone()[0]
            flag_b = conn.execute("SELECT flag FROM vet_claims WHERE id = ?", (claim_b,)).fetchone()[0]
        assert attempted == [claim_a], "outage must stop further matching this tick"
        assert flag_a and flag_a.startswith("invoice extraction unavailable")
        assert flag_b is None, "unattempted claims must not be flagged"
        assert stages == ["poll", "notify"], "downstream stages must still run during an outage"

        # next healthy tick: stale outage flag clears before the attempt
        attempted.clear()
        pipeline.invoice_matching.match_claim = lambda claim: attempted.append(claim["id"]) or False
        pipeline.run_once()
        with db.get_connection() as conn:
            flag_a = conn.execute("SELECT flag FROM vet_claims WHERE id = ?", (claim_a,)).fetchone()[0]
        assert flag_a is None, "recovered claim must not carry a stale outage flag"
    finally:
        (pipeline.vet_detection.classify_unflagged, pipeline.reconcile_sent_invoice_requests,
         pipeline.invoice_matching.match_claim, pipeline._maybe_draft_invoice_request,
         pipeline.poll_petcover_status, pipeline.notify_claim_states, pipeline._ensure_gmail_auth) = originals


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
    assert claim_forms.find_invoice_segment(["Tax Invoice\nPatient: Aari\nTotal: $2,521.46"], 2521.46, "Aari") == (0, 0)


def test_find_invoice_segment_handles_colonless_patient_and_unknown_words():
    """Real SAH format: 'Patient Echo' — no colon (the colon-required regex
    missed it live). A patient-word that isn't a known pet carries no signal."""
    sah_page = "Tax Invoice\nTransaction No 6351750 Patient Echo Reference Hannah\nTotal: $10.50"
    assert claim_forms.find_invoice_segment([sah_page], 10.50, "Echo", ("Aari",)) == (0, 0)
    assert claim_forms.find_invoice_segment([sah_page], 10.50, "Aari", ("Echo",)) is None, "names the other pet"
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


def _insert_matched_claim(conn, merchant, amount, txn_date, pet_id=None, email_id="em-x",
                          invoice_amount=None, condition=None, invoice_path=None):
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
        (txn_id, pet_id, email_id,
         _json.dumps({"amount": invoice_amount if invoice_amount is not None else abs(amount), "date": txn_date}),
         condition, invoice_path, now, now),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _matched_row(claim_id):
    with db.get_connection() as conn:
        return conn.execute(
            "SELECT vet_claims.*, bank_transactions.merchant AS txn_merchant, "
            "bank_transactions.date AS txn_date, bank_transactions.amount AS txn_amount "
            "FROM vet_claims JOIN bank_transactions ON bank_transactions.id = vet_claims.transaction_id "
            "WHERE vet_claims.id = ?", (claim_id,),
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
    assert row["flag"] and "isn't a per-visit itemised invoice" in row["flag"] and "MEDIPAWS TEST" in row["flag"]


def test_ensure_invoice_file_never_overwrites_manual_path():
    db.init_db()
    with db.get_connection() as conn:
        conn.execute("DELETE FROM vet_claims")
        conn.execute("DELETE FROM bank_transactions")
        cid = _insert_matched_claim(conn, "MEDIPAWS TEST", -100.0, "2026-04-13", invoice_path=r"G:\manual\inv.pdf")
    original = claim_forms._email_pdf_documents
    claim_forms._email_pdf_documents = lambda email_id: (_ for _ in ()).throw(AssertionError("must not fetch"))
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
            (_json.dumps({"invoice_number": "185019", "amount": 44.75, "date": "2025-08-08"}), other),
        )
    invoices = [
        {"invoice_number": "185019", "amount": 44.75, "date": "2025-08-08"},
        {"invoice_number": "185106", "amount": 152.5, "date": "2025-08-11"},
    ]
    picked = invoice_matching._pick_invoice(invoices, -152.5, _date(2025, 8, 11), claim_id=999999)
    assert picked["invoice_number"] == "185106", picked
    # the claimed one alone no longer matches either
    picked = invoice_matching._pick_invoice(invoices[:1], -152.5, _date(2025, 8, 11), claim_id=999999)
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
    replies = iter([
        '{"invoice_number": "184556", "date": "2025-07-28", "patient": "Aari", "amount": 45.0, "items": []}',
        '{"not_invoice": true}',
        "the model rambled and returned no JSON at all",
        '{"invoice_number": "9", "date": "2025-08-01", "patient": "Aari", "items": []}',  # no amount
    ])
    vision_calls = []
    original_att = claim_forms.email_pdf_attachments
    original_vision = llm.extract_vision
    claim_forms.email_pdf_attachments = lambda email_id: [("scans.pdf", _scan_pdf_bytes(4))]
    llm.extract_vision = lambda prompt, jpeg, purpose="vision_extraction": vision_calls.append(1) or next(replies)
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
            (_json.dumps({"invoice_number": "184556", "amount": 45.0, "date": "2025-07-28"}), other),
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
        cid = _insert_matched_claim(conn, "KINGS VET TEST", -45.0, "2025-07-28", email_id="em-scan-3")
        conn.execute(
            "UPDATE vet_claims SET invoice_data = ? WHERE id = ?",
            (_json.dumps({"amount": 45.0, "date": "2025-07-28", "source_pdf": "scans.pdf", "page": 99}), cid),
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
        cid = _insert_matched_claim(conn, "KINGS VET TEST", -45.0, "2025-07-28", email_id="em-scan-2")
        conn.execute(
            "UPDATE vet_claims SET invoice_data = ? WHERE id = ?",
            (_json.dumps({"amount": 45.0, "date": "2025-07-28", "patient": pet["name"],
                          "source_pdf": "scans.pdf", "page": 1}), cid),
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
    assert row["invoice_file_path"] and row["invoice_file_path"].endswith(f"claim-{cid}-2025-07-28.pdf")
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
            _insert_matched_claim(conn, "BATCH VET", -50.0 - i, f"2026-05-{10 + i:02d}", pet_id=aari,
                                  condition="arthritis", invoice_path=f"/data/invoices/t{i}.pdf")
            for i in range(6)
        ]
        lone = _insert_matched_claim(conn, "BATCH VET", -70.0, "2026-05-20", pet_id=aari)  # no condition/invoice

    batches, singles = [], []
    originals = (claim_forms.ensure_invoice_file, claim_forms.process_claim_batch, claim_forms.process_claim)
    claim_forms.ensure_invoice_file = lambda claim: None
    claim_forms.process_claim_batch = lambda ids, continuation=None: batches.append(ids)
    claim_forms.process_claim = lambda cid, continuation=None: singles.append(cid)
    try:
        pipeline._draft_matched_claims()
    finally:
        claim_forms.ensure_invoice_file, claim_forms.process_claim_batch, claim_forms.process_claim = originals

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
        conn.execute(
            "UPDATE vet_claims SET flag = 'manual review needed' WHERE id = ?", (pending,)
        )
    sent = []
    pipeline.notify_claim_states(send_fn=lambda text, markup=None: sent.append(text))
    cond_msg = next(t for t in sent if "IDCHECK VET" in t)
    pending_msg = next(t for t in sent if "IDCHECK PENDING" in t)
    assert f"#{needs_cond}" in cond_msg, cond_msg
    assert f"#{pending}" in pending_msg, pending_msg


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
        conn.execute("UPDATE pets SET policy_anniversary = NULL")


def _insert_claim(conn, pet_id, txn_date, status="sent", draft_id=None, reference=None,
                  sr=None, condition=None, invoice_data=None, amount=-50.0, merchant="THREAD VET"):
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
        c1 = _insert_claim(conn, aari, "2026-05-01", status="acknowledged", reference="DC1-27-5628", sr=1)
        c2 = _insert_claim(conn, aari, "2026-05-02", status="acknowledged", reference="DC1-27-5628", sr=2)
    claim_status.process_reply("m-sr", "Petcover Claim DC1-27-5628 SR1 - Claim suspended", "")
    assert _claim_row(c1)["status"] == "suspended"
    assert _claim_row(c2)["status"] == "acknowledged", "the other serial must be untouched"


def test_reference_reuse_never_touches_settled_claims():
    """Reference-only event on a thread that holds settled + open claims: only
    the open ones move; settled claims are done (the ref is reused for years)."""
    _fresh_db()
    with db.get_connection() as conn:
        aari = _aari(conn)
        done1 = _insert_claim(conn, aari, "2026-02-01", status="settled", reference="DC1-27-5628", sr=1)
        done2 = _insert_claim(conn, aari, "2026-02-02", status="declined", reference="DC1-27-5628", sr=2)
        open1 = _insert_claim(conn, aari, "2026-07-01", status="acknowledged", reference="DC1-27-5628", sr=3)
        open2 = _insert_claim(conn, aari, "2026-07-02", status="acknowledged", reference="DC1-27-5628", sr=4)
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
        t1 = _insert_claim(conn, aari, "2026-06-01", status="acknowledged", draft_id="d1",
                           reference="DC1-30-1", sr=1)
        t2 = _insert_claim(conn, aari, "2026-06-02", status="acknowledged", draft_id="d1",
                           reference="DC1-31-9", sr=1)
    claim_status.process_reply("m-dec", "Petcover Claim DC1-30-1 - Declined - Invoices over 12 months", "")
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
        "m-cond", "PetCover - Acknowledgement Letter",
        "Pet Name: Aari Condition: Arthritis Claim Number DC1-40-1 Thank you",
    )
    assert _claim_row(arth)["status"] == "acknowledged" and _claim_row(arth)["petcover_reference"] == "DC1-40-1"
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
        conn.execute("UPDATE vet_claims SET updated_at = '2026-07-01T00:00:00+00:00' WHERE id = ?", (older,))
        conn.execute("UPDATE vet_claims SET updated_at = '2026-07-10T00:00:00+00:00' WHERE id = ?", (newer,))
    claim_status.process_reply(
        "m-recon", "PetCover - Acknowledgement Letter",
        "Pet Name: Aari Condition: Lick Granuloma Claim Number DC1-41-2 Thank you",
    )
    assert _claim_row(newer)["status"] == "acknowledged", "recency picks the most-recently-sent submission"
    assert _claim_row(newer)["condition_text"] == "Dermatitis", "our condition_text must not be overwritten"
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
        conn.execute("UPDATE vet_claims SET updated_at = '2026-07-01T00:00:00+00:00' WHERE id = ?", (sub_old,))
        conn.execute("UPDATE vet_claims SET updated_at = '2026-07-10T00:00:00+00:00' WHERE id = ?", (sub_new,))
    claim_status.process_reply("m-a", "PetCover - Acknowledgement Letter",
                               "Pet Name: Aari Claim Number DC1-50-1 Thank you")
    claim_status.process_reply("m-b", "PetCover - Acknowledgement Letter",
                               "Pet Name: Aari Claim Number DC1-51-2 Thank you")
    refs = {_claim_row(sub_old)["petcover_reference"], _claim_row(sub_new)["petcover_reference"]}
    assert refs == {"DC1-50-1", "DC1-51-2"}, f"each ack must learn a distinct reference: {refs}"


def test_batch_ack_assigns_serials_oldest_txn_first():
    """One 3-claim submission; three acks (Sr 2/3/4 of one reference) attach to
    the claims oldest-transaction-first, each learning its own serial."""
    _fresh_db()
    with db.get_connection() as conn:
        aari = _aari(conn)
        ids = [_insert_claim(conn, aari, f"2025-08-{10 + i:02d}", draft_id="d-batch") for i in range(3)]
    for serial in (2, 3, 4):
        claim_status.process_reply(
            f"m-ack-{serial}", "PetCover - Acknowledgement Letter",
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
        second = _insert_claim(conn, aari, second_txn, status="acknowledged", reference="DC1-SS-1",
                               invoice_data=_json.dumps({"claimable_amount": 500.0, "amount": 500.0}))
    flag = claim_status._validate_settlement(_claim_row(second), 350.0, second_txn)
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
        cid = _insert_claim(conn, aari, txn, status="acknowledged", reference="DC1-T-1",
                            invoice_data=_json.dumps({"claimable_amount": 500.0}))
    # expected = 500 - 150 excess = 350; paid within $2 → no flag
    assert claim_status._validate_settlement(_claim_row(cid), 349.0, txn) is None
    # paid short beyond tolerance → flag, no prior sibling this year so "fresh excess"
    flag = claim_status._validate_settlement(_claim_row(cid), 300.0, txn)
    assert flag and "fresh $150 excess" in flag


def test_settlement_unknown_anniversary_degrades():
    _fresh_db()
    import json as _json
    with db.get_connection() as conn:
        aari = _aari(conn)
        conn.execute("UPDATE pets SET policy_anniversary = NULL WHERE id = ?", (aari,))
        txn = _relative_date(30)
        cid = _insert_claim(conn, aari, txn, status="acknowledged", reference="DC1-U-1",
                            invoice_data=_json.dumps({"claimable_amount": 500.0}))
    flag = claim_status._validate_settlement(_claim_row(cid), 200.0, txn)
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
        cid = _insert_claim(conn, aari, closed_year_txn, status="acknowledged", reference="DC1-CY-1",
                            invoice_data=_json.dumps({"claimable_amount": 55.74}))
    # expected = full claimable (no excess) since the year is closed; paid short of that -> flag
    assert claim_status._validate_settlement(_claim_row(cid), 55.74, closed_year_txn) is None
    flag = claim_status._validate_settlement(_claim_row(cid), 22.75, closed_year_txn)
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
        prior_txn = _relative_date(60)   # before the anniversary -> prior, closed year
        current_txn = _relative_date(5)  # after the anniversary -> current year
        first = _insert_claim(conn, aari, prior_txn, status="settled", reference="DC1-BD-1")
        _insert_settled_event(conn, first, datetime.now(timezone.utc).isoformat(), 400.0)
        second = _insert_claim(conn, aari, current_txn, status="acknowledged", reference="DC1-BD-1",
                               invoice_data=_json.dumps({"claimable_amount": 500.0}))
    flag = claim_status._validate_settlement(_claim_row(second), 300.0, current_txn)
    assert flag and "fresh $150 excess" in flag, "prior claim's closed-year txn must not count toward this year's excess"


def test_classify_approved_and_below_excess():
    """Real letters (Jul 2026) both use the generic subject 'Petcover Insurance
    Claim for Ari' — classification must come from the body phrase."""
    assert claim_status.classify(
        "Petcover Insurance Claim for Ari", "Your claim has been approved\nWe have assessed the recent claim"
    ) == "approved"
    assert claim_status.classify(
        "Petcover Insurance Claim for Ari",
        "Claim assessment outcome: Under excess\nWhile it is a claimable condition, the amount you have claimed is under your fixed excess.",
    ) == "below_excess"


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
    assert "paid_amount" not in amounts4, "this letter states no payout yet — must not fabricate one"


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
        cid = _insert_claim(conn, aari, txn, status="acknowledged", reference="DC1-OVER-1",
                            invoice_data=_json.dumps({"claimable_amount": 200.0}))
    # expected = 200 - 150 = 50; paid way more than expected -> still flagged
    flag = claim_status._validate_settlement(_claim_row(cid), 190.0, txn)
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
        cid = _insert_claim(conn, aari, closed_year_txn, status="acknowledged", reference="DC1-APR-1", sr=2,
                            invoice_data=_json.dumps({"claimable_amount": 55.74}))
    claim_status.process_reply(
        "m-approved-1", "Petcover Insurance Claim for Ari",
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
    from openclaw import pipeline
    from googleapiclient.errors import HttpError

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
    pipeline.gmail_client.build_service = lambda: (_ for _ in ()).throw(RuntimeError("No Gmail token at x"))
    try:
        results = [pipeline._ensure_gmail_auth(send_fn=lambda t, markup=None: sent.append(t)) for _ in range(7)]
    finally:
        pipeline.gmail_client.build_service = original
    assert results == [False] * 7
    assert len(sent) == 5, f"cap is 5/24h, got {len(sent)}"
    assert all("gmail_auth.py" in s for s in sent)
    with db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ops_alerts WHERE kind='gmail_auth'").fetchone()[0] == 5


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
        assert pipeline._ensure_gmail_auth(send_fn=spy) is True   # recovery
        assert pipeline._ensure_gmail_auth(send_fn=spy) is True   # nothing more
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
    assert claim_forms._shared_fields(pet, True)["claim_continuation_state"] == "/0", "ticked = Yes = /0"
    assert inspect.signature(claim_forms.process_claim).parameters["continuation"].default is True
    assert inspect.signature(claim_forms.process_claim_batch).parameters["continuation"].default is True


def _insert_txn(conn, date, amount, merchant="KINGS VET CLINIC"):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO bank_transactions (date, amount, merchant, vet_flag, created_at) VALUES (?, ?, ?, 1, ?)",
        (date, amount, merchant, now),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _insert_ledger_claim(conn, txn_id, pet_id, status, condition=None, claimable=None, item_conditions=None):
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
    # later one only clears the remaining $50 of excess -> $50 expected
    assert by_txn[t1]["expected"]["value"] == 0.0
    assert by_txn[t2]["expected"]["value"] == 50.0


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
    assert claim["expected"]["value"] == 100.0, claim["expected"]


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
    assert [r["date"] for r in rows] == [older, older, in_window], "stale row excluded, oldest-first order"
    assert {r["pet_name"] for r in rows if r["date"] == older} == {"Aari", "Echo"}, "shared charge yields one row per claim"


def test_claim_card_totals_split_actual_paid_from_estimate():
    """Card header: reimbursed counts ONLY money Petcover actually paid; the
    'to come' figure is our own estimate for everything unsettled and must not
    double-count a settled claim. Petcover's age contribution is deliberately
    not modelled (no rate is recorded for any pet), so the estimate reads high
    and is flagged as an estimate rather than quietly presented as fact."""
    from openclaw import claim_card

    rows = [
        {"date": "2026-07-06", "merchant": "V", "amount": -100.0, "status": "settled",
         "pet_name": "Aari", "condition_text": "Arthritis", "paid": 22.75, "expected": None},
        {"date": "2026-06-06", "merchant": "V", "amount": -200.0, "status": "sent",
         "pet_name": "Aari", "condition_text": "Arthritis",
         "paid": None, "expected": {"available": True, "value": 50.0, "estimate": True}},
        {"date": "2026-05-06", "merchant": "V", "amount": -60.0, "status": "pending_match",
         "pet_name": "Aari", "condition_text": None,
         "paid": None, "expected": {"available": False, "value": None}},
    ]
    agg = claim_card.totals(rows)
    assert agg["reimbursed"] == 22.75, "only the settled claim's real payment counts"
    assert agg["outstanding"] == 50.0, "unavailable estimate contributes nothing, settled isn't re-counted"
    assert agg["outstanding_is_estimate"] is True

    # Nothing estimable at all → no phantom '~$0' estimate marker.
    assert claim_card.totals(rows[:1]) == {"reimbursed": 22.75, "outstanding": 0.0, "outstanding_is_estimate": False}


def test_claim_card_renders_png_for_every_status():
    """Rendering must not crash on any real status (an unmapped one falls back
    to neutral colours) — the card is the whole /history reply, so a render
    error means Justin gets nothing."""
    from openclaw import claim_card

    rows = [
        {"date": f"2026-0{(i % 9) + 1}-1{i % 9}", "merchant": "The Shire Veterinary Hospital",
         "amount": -(50 + i), "status": status, "pet_name": "Aari", "condition_text": "Arthritis",
         "paid": None, "expected": {"available": True, "value": 10.0, "estimate": True}}
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
    assert by_claim[blocked]["actionable"] is False, "no button can clear an undefined insurer process"
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
    blocked = {"id": 1, "status": "matched", "flag": "Bow Wow Insurance claim process not yet defined",
               "pet_id": 2, "condition_text": None}
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
    vet = {"id": 1, "status": "info_requested", "flag": None, "pet_id": 1,
           "condition_text": "Raised ALT", "owed_by": "vet"}
    mine = {**vet, "owed_by": "justin"}
    unknown = {**vet, "owed_by": None}

    assert status_labels.label(vet) == "More vet info required"
    assert status_labels.label(mine) == "Petcover needs info from you"
    assert status_labels.label(unknown) == "Info requested", "no claim about who must act"
    # the word "suspended" belongs to an actual suspension and nothing else
    assert "suspend" not in " ".join(
        status_labels.label(c).lower() for c in (vet, mine, unknown)
    )
    assert status_labels.label({**vet, "status": "suspended"}) == "Suspended"


def test_the_label_names_the_document_petcover_asked_for():
    """"More vet info required" cannot be acted on; "consult notes needed" can.
    The document says WHAT, `owed_by` says WHO, and both matter — a request naming
    the document but not the party invites the wrong chase, so an unrecorded owner
    stays neutral whatever was asked for."""
    base = {"id": 1, "status": "info_requested", "flag": None, "pet_id": 1, "condition_text": "Raised ALT"}
    vet_doc = {**base, "owed_by": "vet", "requested_document": "Consultation notes dated 18/05/2026"}
    mine_doc = {**base, "owed_by": "justin", "requested_document": "Consultation notes dated 18/05/2026"}

    assert status_labels.label(vet_doc) == "Vet: consult notes needed"
    assert status_labels.label(mine_doc) == "Consult notes needed from you"
    # No document, or a kind we don't recognise: exactly the wording it had before.
    assert status_labels.label({**base, "owed_by": "vet", "requested_document": None}) == "More vet info required"
    assert status_labels.label({**base, "owed_by": "vet", "requested_document": "a signed affidavit"}) == "More vet info required"
    assert status_labels.label({**base, "owed_by": "justin", "requested_document": None}) == "Petcover needs info from you"
    # Owner unrecorded stays neutral even with a document named.
    assert status_labels.label({**base, "owed_by": None, "requested_document": "Consultation notes"}) == "Info requested"
    # The chase line names the document too, and it is an action not a state.
    assert status_labels.needs(vet_doc) == "Chase vet for consult notes"
    assert status_labels.needs(mine_doc) == "Send Petcover the consult notes"
    # A request is never worded as a suspension.
    assert all("suspend" not in status_labels.label(c).lower() for c in (vet_doc, mine_doc))


def test_short_document_recognises_the_kinds_seen_live():
    assert status_labels.short_document("Consultation notes dated 18/05/2026") == "consult notes"
    assert status_labels.short_document("Itemized invoice for the visit") == "itemised invoice"
    assert status_labels.short_document("Completed claim form") == "claim form"
    assert status_labels.short_document("Referral history from the treating vet") == "referral history"
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
    assert batch["date"] == "2026-04-17", "urgency comes from the oldest member — expiry is per visit"
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
    assert sorted(a["claim_id"] for a in mismatches) == sorted(ids), "one entry per claim, not per draft"


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
    assert "settlement mismatch" in _json.loads(event["detail"])["dismissed_flag"], "keeps what was dismissed"
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
    assert "cannot browse, search, or read justin's mailbox" in lowered, \
        "must state the mailbox limit rather than imply access"

    impls = agent._build_impls([])
    rejection = impls["propose_assign_pet"]("Whiskers")
    assert "No pet named" in rejection, "a made-up pet must be refused, not assigned"
    assert "Aari" in rejection and "Echo" in rejection, "and the real pets offered, so it can't guess again"
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
        wanted = _insert_claim(conn, aari, "2026-07-01", status="pending_match", merchant="BONDI VET")
        other_vet = _insert_claim(conn, aari, "2026-07-02", status="pending_match", merchant="OTHER VET")
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
        assert "does not mean" in quiet, \
            "must explicitly disclaim the stronger reading, not just avoid stating it"

        pipeline.poll_petcover_status = lambda: {"checked": 2, "events": 3, "claims_changed": [18, 21]}
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
        a = _insert_claim(conn, aari, "2026-07-01", status="sent", draft_id="draft-1", reference="DC1-1")
        b = _insert_claim(conn, aari, "2026-07-02", status="sent", draft_id="draft-1", reference="DC1-1")
        solo = _insert_claim(conn, aari, "2026-07-03", status="acknowledged", draft_id="draft-2")
        no_draft = _insert_claim(conn, aari, "2026-07-04", status="drafted")
        settled = _insert_claim(conn, aari, "2026-07-05", status="settled", draft_id="draft-3")
        _insert_settled_event(conn, solo, datetime.now(timezone.utc).isoformat(), 90.0)

    rows = claim_status.submissions_awaiting_reply()
    by_ids = {tuple(r["claim_ids"]): r for r in rows}
    assert (a, b) in by_ids, f"the batch is one entry, got {[r['claim_ids'] for r in rows]}"
    assert all(settled not in r["claim_ids"] for r in rows), "a settled submission isn't awaiting anything"

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
        invoice = _json.dumps({
            "invoice_number": "INV-9",
            "amount": 55.74,
            "claimable_amount": 44.75,
            "items": [{"description": "Consultation", "amount": 44.75},
                      {"description": "Food", "amount": 10.99}],
        })
        claim = _insert_claim(conn, aari, "2025-09-26", status="approved", reference="DC1-27-5628",
                              sr=4, condition="Arthritis", invoice_data=invoice)
        conn.execute("UPDATE vet_claims SET flag = ? WHERE id = ?",
                     ("settlement mismatch — we expected $44.75, Petcover paid $22.75 — review", claim))
        conn.execute(
            "INSERT INTO claim_status_events (claim_id, event_type, raw_email_id, detail, created_at) "
            "VALUES (?, 'approved', 'mail-1', ?, ?)",
            (claim, _json.dumps({"subject": "Claim Approval", "claimed_amount": 35.0,
                                 "paid_amount": 22.75, "fixed_excess_stated": 0.0,
                                 "age_contribution_stated": 12.25,
                                 "body": "a long body that must not reach the chat turn"}),
             datetime.now(timezone.utc).isoformat()),
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
            assert conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()[0] == "open", \
                "still open until the tap"
        telegram_bot._execute_action(proposals[-1])
        with db.get_connection() as conn:
            row = conn.execute("SELECT status, outcome FROM tasks WHERE id = ?", (task_id,)).fetchone()
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
        assert f"#{claim_id}" in answer, f"#{claim_id} missing — a dropped id is an unactionable answer"
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
    assert "does not mean petcover has never replied" not in prompt  # that wording lives in the tool
    assert "nothing new" in prompt, "the new-mail-only limit is stated"
    assert "cannot read openclaw's code" in prompt, "no code/spec reading in the container"


_shared_invoice_charges = 0


_AARI_INVOICE = {"invoice_number": "SHV49c1622284e5", "date": "2026-06-19", "patient": "Aari",
                 "amount": 35.0, "items": [{"description": "Prescription fee", "amount": 35.0}]}
_ECHO_INVOICE = {"invoice_number": "SHVd5b232905fdb", "date": "2026-06-30", "patient": "Echo",
                 "amount": 369.33, "items": [{"description": "CLINDAMYCIN 150MG CAPSULES", "amount": 206.12},
                                             {"description": "ENROFLOXACIN 150MG TABLETS", "amount": 163.21}]}
# The receipts' own wording. Both visits are weeks before the 6 Jul charge, so
# only the payment line makes them matchable at all.
_AARI_RECEIPT_TEXT = ("TAX INVOICE - RECEIPT 19 Jun 2026 # SHV49c1622284e5\n"
                      "Aari 19 Jun 2026 Prescription fee 1.00 $0.00 $31.82 $35.00\n"
                      "TOTAL $35.00\nThe following payments have been received with thanks\n"
                      "Paid Date Payment Method Payment\n06/07/2026 Credit Card $35.00")
_ECHO_RECEIPT_TEXT = ("TAX INVOICE - RECEIPT 30 Jun 2026 # SHVd5b232905fdb\n"
                      "Echo 30 Jun 2026 CLINDAMYCIN 150MG CAPSULES 28.00 $206.12\n"
                      "Echo 30 Jun 2026 ENROFLOXACIN 150MG TABLETS 11.00 $163.21\n"
                      "TOTAL $369.33\nThe following payments have been received with thanks\n"
                      "Paid Date Payment Method Payment\n06/07/2026 Credit Card $369.33")


def test_a_receipt_paid_on_the_charge_date_is_matchable_though_the_visit_is_older():
    """INVOICE_MATCH_WINDOW_DAYS is 3, measured on the SERVICE date — which
    silently rejected both real invoices for this charge: The Shire Vet billed
    19 Jun and 30 Jun, the card was charged 6 Jul, and each receipt says so on
    its own payment line ("06/07/2026 Credit Card $35.00")."""
    from datetime import date as _date

    from openclaw import config

    txn_date = _date(2026, 7, 6)
    assert config.INVOICE_MATCH_WINDOW_DAYS == 3, "this test exists because the window is tight"
    assert not invoice_matching._invoice_date_plausible(_AARI_INVOICE, txn_date), "17 days out on service date"
    assert invoice_matching._paid_on_charge_date(_AARI_RECEIPT_TEXT, _AARI_INVOICE, txn_date)
    assert invoice_matching._paid_on_charge_date(_ECHO_RECEIPT_TEXT, _ECHO_INVOICE, txn_date)

    # Both facts required on ONE line: a bulk email full of other visits' payment
    # dates must not lend them to this invoice.
    assert not invoice_matching._paid_on_charge_date(
        "06/07/2026 Credit Card $999.00\nsome other invoice $35.00", _AARI_INVOICE, txn_date
    ), "the date and THIS invoice's amount have to be the same payment line"
    assert not invoice_matching._paid_on_charge_date(_AARI_RECEIPT_TEXT, _AARI_INVOICE, _date(2026, 7, 7))
    assert not invoice_matching._paid_on_charge_date("", _AARI_INVOICE, txn_date)

    # And it reaches the picker: the receipt is chosen where the window alone refuses.
    assert invoice_matching._pick_invoice([_AARI_INVOICE], -35.0, txn_date) is None, "window-only: refused"
    picked = invoice_matching._pick_invoice([_AARI_INVOICE], -35.0, txn_date, text=_AARI_RECEIPT_TEXT)
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
    pool = [chosen, {"email_id": "em-echo", "invoice": {**_ECHO_INVOICE}, "text": _ECHO_RECEIPT_TEXT}]

    assert invoice_matching._apply_match(claim, chosen, pool) is True
    rows = sorted(
        (dict(r) for r in _claims_on_transaction(claim["transaction_id"])), key=lambda r: r["id"]
    )
    assert len(rows) == 2, f"the second invoice must get its own claim: {rows}"
    kept, sibling = rows
    assert kept["id"] == claim_id and kept["pet_id"] == 1, kept
    assert sibling["pet_id"] == 2, "the pet comes off each invoice's printed patient field"
    assert sibling["matched_email_id"] == "em-echo", "each claim carries its OWN invoice email"
    assert kept["flag"] is None, f"nothing is unexplained once both invoices are known: {kept['flag']}"
    kept_invoice, sibling_invoice = _json.loads(kept["invoice_data"]), _json.loads(sibling["invoice_data"])
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
    assert complement([{"email_id": "e", "invoice": {**echo, "amount": 100.0,
                                                    "invoice_number": "X1", "date": "2026-07-06"}, "text": ""}]) is None
    # Together they exceed the charge.
    assert complement([{"email_id": "e", "invoice": {**echo, "amount": 400.0,
                                                     "invoice_number": "X2", "date": "2026-07-06"}, "text": ""}]) is None
    # Right amount, wrong visit — outside the date window.
    assert complement([{"email_id": "e", "invoice": {**echo, "date": "2025-06-30",
                                                     "invoice_number": "X3"}, "text": ""}]) is None
    # Two candidates would both close it: which one the charge paid is unknowable.
    assert complement([
        {"email_id": "e1", "invoice": echo, "text": _ECHO_RECEIPT_TEXT},
        {"email_id": "e2", "invoice": {**echo, "invoice_number": "X4"}, "text": _ECHO_RECEIPT_TEXT},
    ]) is None
    # The same invoice seen twice is not a complement.
    assert complement([{"email_id": "e-dup", "invoice": {**aari}, "text": _AARI_RECEIPT_TEXT}]) is None
    # Nothing left to explain (invoice covers the charge bar a surcharge).
    covered = {"email_id": "em", "invoice": {**aari, "amount": 404.33}, "text": ""}
    assert invoice_matching._complement_for(
        covered, [covered, {"email_id": "e", "invoice": echo, "text": _ECHO_RECEIPT_TEXT}],
        charge, txn_date, 999999) is None


def _claims_on_transaction(txn_id):
    with db.get_connection() as conn:
        return conn.execute("SELECT * FROM vet_claims WHERE transaction_id = ?", (txn_id,)).fetchall()


def _shared_invoice_claim(claimable=407.56, status="matched"):
    """Claim #1's real shape: The Shire Veterinary Caringbah, $407.56 charged
    2026-07-06, invoice matched, no pet, no condition, no itemization. Each call
    walks the charge date back a day — (date, amount, merchant) is unique."""
    import json as _json

    global _shared_invoice_charges
    txn_date = (date.fromisoformat("2026-07-06") - timedelta(days=_shared_invoice_charges)).isoformat()
    _shared_invoice_charges += 1
    now = datetime.now(timezone.utc).isoformat()
    with db.get_connection() as conn:
        conn.execute("INSERT INTO bank_transactions (date, amount, merchant, vet_flag, created_at) "
                     "VALUES (?, ?, ?, 1, ?)",
                     (txn_date, -407.56, "THE SHIRE VETERINARY CARINGBAH NSW", now))
        txn_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO vet_claims (transaction_id, pet_id, status, matched_email_id, invoice_data, "
            "created_at, updated_at) VALUES (?, NULL, ?, 'em-shire', ?, ?, ?)",
            (txn_id, status, _json.dumps({"date": "2026-07-06", "amount": 407.56, "items": [],
                                          "claimable_amount": claimable, "invoice_number": "INV-9"}),
             now, now),
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
    assert (echo["pet_name"], echo["amount"]) == ("Echo", 372.56), "the remainder is derived, not guessed"
    assert result["unapportioned"] == 0.0

    kept, sibling = _claim_row(claim_id), _claim_row(echo["claim_id"])
    assert kept["pet_id"] == 1 and sibling["pet_id"] == 2
    assert kept["transaction_id"] == sibling["transaction_id"], "one charge, one bank row"
    assert kept["matched_email_id"] == sibling["matched_email_id"] == "em-shire"
    for row, share in ((kept, 35.0), (sibling, 372.56)):
        invoice = _json.loads(row["invoice_data"])
        assert invoice["claimable_amount"] == share, invoice
        assert invoice["amount"] == 407.56 and invoice["invoice_number"] == "INV-9", "invoice untouched"
        assert f"#{echo['claim_id']} Echo $372.56" in invoice["split_note"], invoice["split_note"]
    assert sibling["condition_text"] is None, "the other pet's condition is never copied — that's a guess"

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
    assert "$500.00" in over["message"] and "$407.56" in over["message"], "both figures, so it's checkable"

    two_unknowns = claim_forms.split_between_pets(_shared_invoice_claim(), [(1, None), (2, None)])
    assert not two_unknowns["ok"] and "Only one share" in two_unknowns["message"], two_unknowns

    one_pet = claim_forms.split_between_pets(_shared_invoice_claim(), [(1, 35.0)])
    assert not one_pet["ok"] and "at least two pets" in one_pet["message"], one_pet

    dupe = claim_forms.split_between_pets(_shared_invoice_claim(), [(1, 35.0), (1, 100.0)])
    assert not dupe["ok"] and "twice" in dupe["message"], dupe

    already_sent = claim_forms.split_between_pets(_shared_invoice_claim(status="sent"), [(1, 35.0), (2, None)])
    assert not already_sent["ok"], already_sent
    assert "already with the insurer" in already_sent["message"], already_sent["message"]

    unmatched = claim_forms.split_between_pets(_shared_invoice_claim(status="pending_match"),
                                               [(1, 35.0), (2, None)])
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
        return {"text": "Aari $35.00, Echo $372.56 — tap Confirm.", "model": "llama-3.3-70b-versatile"}

    original_chat = agent.llm.chat
    try:
        agent.llm.chat = _fake_chat
        reply, proposal = agent.handle_message(
            "This is actually split between echo and Aari. Aari cost was $35 out of this",
            chat_id=None, claim_id=claim_id,
        )
    finally:
        agent.llm.chat = original_chat

    assert "propose_split_between_pets" in captured["tool_names"], "the tool has to exist to be reachable"
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
    assert agent._pets_named_in("the vet echoed the diagnosis") == [], "word-boundary, not substring"


def test_split_proposal_is_refused_before_the_tap_when_it_cannot_work():
    """An impossible split must be refused in the reply, not after Justin taps
    Confirm — the agent runs the same guards the write does."""
    from openclaw import agent

    db.init_db()
    claim_id = _shared_invoice_claim()
    proposals = []
    impls = agent._build_impls(proposals)

    over = impls["propose_split_between_pets"](
        claim_id=claim_id, pets_and_amounts=[{"pet": "Aari", "amount": 400}, {"pet": "Echo", "amount": 100}]
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
    assert message.content == "recovered" and len(attempts) == 2, "one garbled call must not fail the turn"

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

    edge_403 = Exception("Error code: 403 - {'error': {'message': 'Access denied. "
                         "Please check your network settings.'}}")
    bad_shape = Exception("Error code: 400 - {'error': {'message': "
                          "'messages[2].reasoning: reasoning is not supported with this model'}}")
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
            return conn.execute("SELECT COUNT(*) FROM llm_calls WHERE purpose = 'retrytest'").fetchone()[0]

    try:
        before = _llm_call_count()
        client = _client(edge_403, fail_times=1)
        message = llm._completion(client, "m", [{"role": "user", "content": "hi"}], None, "retrytest")
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

    # Primary spent -> second model's own budget answers, and only ONE attempt is
    # spent on the exhausted model (retrying can't free a daily cap).
    msg = llm._completion(_client({"llama-3.3-70b-versatile"}), "llama-3.3-70b-versatile",
                          [{"role": "user", "content": "hi"}], None, "test")
    assert msg.content == "answered by openai/gpt-oss-120b", tried
    assert tried == ["llama-3.3-70b-versatile", "openai/gpt-oss-120b"], f"no wasted retries: {tried}"
    assert llm._last_model_used == "openai/gpt-oss-120b", "the answering model is recorded"

    # Everything spent -> visible failure that says what's actually wrong, and
    # doesn't send him hunting an outage. The model set is DERIVED from the
    # configured chain, not hardcoded — hardcoding it broke the moment a fourth
    # link was added, by silently letting the "everything is spent" case succeed.
    tried.clear()
    chain = ["llama-3.3-70b-versatile", *llm._FALLBACK_MODELS["groq"]]
    try:
        llm._completion(_client(set(chain)), "llama-3.3-70b-versatile",
                        [{"role": "user", "content": "hi"}], None, "test")
        raise AssertionError("must fail visibly once every budget is gone")
    except llm.LLMUnavailableError as exc:
        assert "daily token budget" in str(exc) and "rolling window" in str(exc)
        assert tried == chain, f"tries each model exactly once, in order: {tried}"


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

    assert llm._assistant_turn(_NoContent())["content"] == "", "None content must not serialize as null"


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
    assert len(encoded) < 9000, f"tool schema is {len(encoded)} bytes — trim descriptions or drop a tool"

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
    assert claim_status.extract_reference("GABR-0305-Request for consult note -First Request") == "GABR-0305"
    # Live regression: the context phrase captures whatever token follows it, and
    # this subject puts junk there. Shape-first is what keeps it out of the DB.
    assert claim_status.extract_reference(
        "Petcover claim for--Aari--DC1-27-5628 Serial Number: 2"
    ) == "DC1-27-5628"


def test_every_live_serial_format():
    for text, expected in [
        ("DC1-27-5628 Sr 3", 3),           # original whitespace form
        ("Petcover claim for Ari DC1-27-5628 Sr.8", 8),   # dot separator, live 2026-07-27
        ("Petcover claim for Ari - DC1-27-5628 sr.1", 1),  # lowercase + dot, live 2026-07-27
        ("Petcover claim for--Aari--DC1-27-5628 Serial Number: 2", 2),  # live 2026-07-19
        ("DC1-27-5628 nothing adjacent\nTreatment number: 7", 7),
    ]:
        assert claim_status.extract_sr(text, "DC1-27-5628") == expected, text


def test_info_request_letter_is_not_filed_as_a_suspension():
    """It says "will be suspended" about itself. With `suspended` ordered first
    the live DB ended up with zero info_requested events and two suspended ones,
    both of which were actually requests."""
    assert claim_status.classify("PetCover - Claim Further Information Required", _INFO_REQUEST_LETTER) == "info_requested"


def test_a_genuine_suspension_is_still_a_suspension():
    """The pair the reorder must not collapse — this is a real subject."""
    assert claim_status.classify(
        "Petcover Claim DC1-27-5628 SR1 - Claim suspended", "Your claim has been suspended."
    ) == "suspended"


def test_vet_cover_note_is_classified_not_queued():
    """Was `unclassified` live — the one classification that produces no action."""
    assert claim_status.classify(_VET_COVER_NOTE_SUBJECT, _VET_COVER_NOTE_BODY) == "info_requested"
    assert claim_status.classify(
        _VET_COVER_NOTE_SUBJECT, "", claim_status.INFO_REQUEST_SENDER
    ) == "info_requested", "the dedicated channel classifies on the sender alone"


def test_auto_reply_from_the_required_info_channel_is_still_noise():
    assert claim_status.classify(
        "Automatic reply: Aari Goldberg - GOLD093", "", claim_status.INFO_REQUEST_SENDER
    ) == "ignore"


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
    known = claim_status.resolve_owed_by('"info@kingsvet.com.au" <info@kingsvet.com.au>, jagberg@gmail.com')
    assert known["owed_by"] == "vet" and known["clinic"] == "Kings Vet KINGSGROVE NSW"

    mine = claim_status.resolve_owed_by("jagberg@gmail.com")
    assert mine["owed_by"] == "justin"

    # An address we don't recognize must NOT quietly become Justin's problem —
    # that reassignment is exactly how a request goes unchased.
    unknown = claim_status.resolve_owed_by('"admin@newvet.com.au" <admin@newvet.com.au>, jagberg@gmail.com')
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
        claim = _insert_claim(conn, aari, "2026-04-02", status="acknowledged",
                             reference="DC1-26-5992", sr=1)
    claim_status.process_reply(
        "m-info", "PetCover - Claim Further Information Required", _INFO_REQUEST_LETTER,
        "claims.au@petcovergroup.com", '"info@kingsvet.com.au" <info@kingsvet.com.au>, jagberg@gmail.com',
    )
    with db.get_connection() as conn:
        event = conn.execute(
            "SELECT event_type, detail FROM claim_status_events WHERE claim_id = ? ORDER BY id DESC", (claim,)
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

    aari_receipt = _json.dumps({"date": "2026-06-19", "amount": 35.0,
                                "items": [{"description": "Prescription fee", "amount": 35.0,
                                           "date": "2026-06-19"}]})
    treated, known = claim_status.treatment_date(aari_receipt, "2026-07-06")
    assert (treated, known) == ("2026-06-19", True), "the receipt states the treatment date"

    # An invoice billing several visits expires on its OLDEST one.
    multi = _json.dumps({"date": "2026-06-30", "items": [{"date": "2026-06-18"}, {"date": "2026-06-30"}]})
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
        asked_on = _insert_claim(conn, aari, "2026-04-02", status="info_requested", merchant="Kings Vet",
                                 invoice_data=_json.dumps({"date": "2026-04-02", "invoice_number": "199464",
                                                           "amount": 446.5}))
        holds_visit = _insert_claim(conn, aari, "2026-05-18", status="settled", merchant="Kings Vet",
                                    invoice_data=_json.dumps({"date": "2026-05-18", "invoice_number": "1000229",
                                                              "amount": 351.5}))

    hits = invoice_matching.find_visit_by_date("2026-05-18")
    assert [h["claim_id"] for h in hits] == [holds_visit], "the date names its own visit, not the asking claim"
    assert hits[0]["invoice_number"] == "1000229" and hits[0]["amount"] == 351.5
    assert holds_visit != asked_on, "the whole point: the request and the visit are different claims"
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
        claim = _insert_claim(conn, aari, "2026-06-30", merchant="Kings Vet",
                              invoice_data=_json.dumps({
                                  "date": "2026-06-30", "invoice_number": "200500", "amount": 300.0,
                                  "items": [{"description": "Consultation", "amount": 96.5, "date": "2026-06-18"},
                                            {"description": "Bloods", "amount": 203.5, "date": None}]}))
    assert [h["claim_id"] for h in invoice_matching.find_visit_by_date("2026-06-18")] == [claim]
    assert [h["claim_id"] for h in invoice_matching.find_visit_by_date("2026-06-30")] == [claim], "header date still works"


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
        a = _insert_claim(conn, aari, "2026-07-06", merchant="The Shire Vet", amount=-35.0,
                          invoice_data=_json.dumps({"date": "2026-07-06", "invoice_number": "A1", "amount": 35.0}))
        b = _insert_claim(conn, aari, "2026-07-06", merchant="The Shire Vet", amount=-369.33,
                          invoice_data=_json.dumps({"date": "2026-07-06", "invoice_number": "B2", "amount": 369.33}))
    assert sorted(h["claim_id"] for h in invoice_matching.find_visit_by_date("2026-07-06")) == sorted([a, b])


def test_requested_document_stops_at_the_letters_boilerplate():
    """The item sits between the ask and the standard footer. An ask with nothing
    after it must yield nothing — an earlier cut of this captured "Please note we
    cannot process the claim…" and would have shown that to Justin as the document
    Petcover wanted."""
    assert claim_status.extract_requested_document(_INFO_REQUEST_LETTER) == "Consultation notes dated 18/05/2026"
    assert claim_status.extract_requested_document("we need a copy of\n\nPlease note we cannot process") is None
    assert claim_status.extract_requested_document("a letter with no recognized ask at all") is None
    # Two items asked for at once: the earlier first-line-only cut dropped the second.
    assert claim_status.extract_requested_document(
        "we need a copy of\nConsultation notes dated 18/05/2026\nItemised invoice\n\nPlease note"
    ) == "Consultation notes dated 18/05/2026; Itemised invoice"


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
    assert claim_status.extract_requested_document(ends_mid_sentence) is None, "filler is not a document"

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
    assert claim_status.requested_document_date("Consultation notes dated 18/05/2026") == "2026-05-18"
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
    args = ("m-ack", "PetCover - Acknowledgement Letter",
            "Pet's name: Ari\nClaim Reference: DC1-26-5992 Sr 1\nCondition: Raised ALT")
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
        claim = _insert_claim(conn, aari, "2026-06-19", status="suspended", reference="DC1", sr=None)
    assert claim_status.detach_reference(claim)["ok"] is True
    row = _claim_row(claim)
    assert row["petcover_reference"] is None and row["petcover_sr"] is None
    assert claim_status.detach_reference(claim)["ok"] is False, "nothing left to detach"

    with db.get_connection() as conn:
        types = [r["event_type"] for r in conn.execute(
            "SELECT event_type FROM claim_status_events WHERE claim_id = ?", (claim,))]
    assert "reference_detached" in types, "the undo is logged, not a silent wipe"

    # Detached, it is a candidate again and the real letter can route to it.
    assert claim_status.correlate_ack("Petcover claim for Ari DC1-27-5628 Sr.8")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"{name} OK")
    print("ALL TESTS PASSED")
