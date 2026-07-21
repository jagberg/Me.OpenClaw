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
                 pipeline.poll_petcover_status, pipeline.notify_claim_states)
    pipeline.vet_detection.classify_unflagged = lambda: stages.append("classify")
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
         pipeline.poll_petcover_status, pipeline.notify_claim_states) = originals

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
                 pipeline.poll_petcover_status, pipeline.notify_claim_states)
    pipeline.vet_detection.classify_unflagged = lambda: None
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
         pipeline.poll_petcover_status, pipeline.notify_claim_states) = originals


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"{name} OK")
    print("ALL TESTS PASSED")
