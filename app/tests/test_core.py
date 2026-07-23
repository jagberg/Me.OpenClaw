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
# Keep the suite hermetic: force every LLM backend unconfigured so extraction
# fails visibly (the intended assertion) instead of making a real API call from
# a key that happens to be in .env or the container env. Vision tests stub
# llm.extract_vision on top of this — tokens are limited, tests never spend them.
os.environ["GEMINI_API_KEY"] = ""
os.environ["GROQ_API_KEY"] = ""
os.environ["OPENAI_API_KEY"] = ""

from openclaw import claim_forms, claim_status, db, gemini, invoice_matching, llm, netbank_csv, reminders, tasks, vet_detection  # noqa: E402
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

    originals = (pipeline.vet_detection.classify_unflagged, pipeline._reconcile_sent_invoice_requests,
                 pipeline.invoice_matching.match_claim, pipeline._maybe_draft_invoice_request,
                 pipeline.poll_petcover_status, pipeline.notify_claim_states, pipeline._ensure_gmail_auth)
    pipeline.vet_detection.classify_unflagged = lambda: stages.append("classify")
    pipeline._ensure_gmail_auth = lambda: True
    pipeline._reconcile_sent_invoice_requests = lambda: stages.append("reconcile")
    pipeline.invoice_matching.match_claim = fake_match
    pipeline._maybe_draft_invoice_request = lambda claim: stages.append(f"draft:{claim['id']}")
    pipeline.poll_petcover_status = lambda: stages.append("poll")
    pipeline.notify_claim_states = lambda: stages.append("notify")
    try:
        pipeline.run_once()
    finally:
        (pipeline.vet_detection.classify_unflagged, pipeline._reconcile_sent_invoice_requests,
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

    originals = (pipeline.vet_detection.classify_unflagged, pipeline._reconcile_sent_invoice_requests,
                 pipeline.invoice_matching.match_claim, pipeline._maybe_draft_invoice_request,
                 pipeline.poll_petcover_status, pipeline.notify_claim_states, pipeline._ensure_gmail_auth)
    pipeline.vet_detection.classify_unflagged = lambda: None
    pipeline._ensure_gmail_auth = lambda: True
    pipeline._reconcile_sent_invoice_requests = lambda: None
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
        (pipeline.vet_detection.classify_unflagged, pipeline._reconcile_sent_invoice_requests,
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


def test_settlement_short_second_same_year_flags():
    """A thread that already had its excess deducted (Feb settlement) then pays
    claimable − $150 again in the same policy year → flagged short."""
    _fresh_db()
    import json as _json
    with db.get_connection() as conn:
        aari = _aari(conn)
        conn.execute("UPDATE pets SET policy_anniversary = '01-01' WHERE id = ?", (aari,))
        first = _insert_claim(conn, aari, "2026-02-01", status="settled", reference="DC1-SS-1")
        _insert_settled_event(conn, first, "2026-02-10T00:00:00+00:00", 400.0)
        second = _insert_claim(conn, aari, "2026-07-01", status="acknowledged", reference="DC1-SS-1",
                               invoice_data=_json.dumps({"claimable_amount": 500.0, "amount": 500.0}))
    flag = claim_status._validate_settlement(_claim_row(second), 350.0)
    assert flag and "settlement short" in flag and "$500.00" in flag and "$350.00" in flag
    assert "already deducted" in flag, flag


def test_settlement_within_tolerance_no_flag():
    _fresh_db()
    import json as _json
    with db.get_connection() as conn:
        aari = _aari(conn)
        conn.execute("UPDATE pets SET policy_anniversary = '01-01' WHERE id = ?", (aari,))
        cid = _insert_claim(conn, aari, "2026-07-01", status="acknowledged", reference="DC1-T-1",
                            invoice_data=_json.dumps({"claimable_amount": 500.0}))
    # expected = 500 - 150 excess = 350; paid within $2 → no flag
    assert claim_status._validate_settlement(_claim_row(cid), 349.0) is None
    # paid short beyond tolerance → flag, first settlement so "less excess"
    flag = claim_status._validate_settlement(_claim_row(cid), 300.0)
    assert flag and "less excess" in flag


def test_settlement_unknown_anniversary_degrades():
    _fresh_db()
    import json as _json
    with db.get_connection() as conn:
        aari = _aari(conn)
        conn.execute("UPDATE pets SET policy_anniversary = NULL WHERE id = ?", (aari,))
        cid = _insert_claim(conn, aari, "2026-07-01", status="acknowledged", reference="DC1-U-1",
                            invoice_data=_json.dumps({"claimable_amount": 500.0}))
    flag = claim_status._validate_settlement(_claim_row(cid), 200.0)
    assert flag and "anniversary unknown" in flag


def test_settlement_anniversary_boundary_rededucts_excess():
    """Thread settled in the previous policy year; a settlement after the
    anniversary deducts the excess again (new year)."""
    _fresh_db()
    import json as _json
    with db.get_connection() as conn:
        aari = _aari(conn)
        conn.execute("UPDATE pets SET policy_anniversary = '07-01' WHERE id = ?", (aari,))
        first = _insert_claim(conn, aari, "2026-01-01", status="settled", reference="DC1-BD-1")
        _insert_settled_event(conn, first, "2026-02-10T00:00:00+00:00", 400.0)  # previous policy year
        second = _insert_claim(conn, aari, "2026-07-05", status="acknowledged", reference="DC1-BD-1",
                               invoice_data=_json.dumps({"claimable_amount": 500.0}))
    flag = claim_status._validate_settlement(_claim_row(second), 300.0)
    assert flag and "less excess" in flag, "excess must be deducted again in the new policy year"


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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"{name} OK")
    print("ALL TESTS PASSED")
