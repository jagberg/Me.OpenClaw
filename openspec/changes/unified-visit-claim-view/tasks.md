## 1. Policy attributes (excess / cap)

- [x] 1.1 Add nullable `annual_excess` and `annual_cap` columns to the `pets` CREATE TABLE in `db.py` (for fresh DBs)
- [ ] 1.2 Run manual `ALTER TABLE pets ADD COLUMN annual_excess REAL` and `... ADD COLUMN annual_cap REAL` against `app/data/openclaw.db` (CLAUDE.md: IF NOT EXISTS won't touch existing tables)
      — NOT run: this dashboard checkout has no `app/data/openclaw.db`. Must run on Justin's deployment box. Fresh DBs get the columns from 1.1.
- [x] 1.3 Seed Aari's policy: `annual_excess = 150`, `annual_cap = 10000`; leave Echo NULL (Bow Wow, process undefined) — in `SEED_PETS` (fresh DBs). On the live DB, set with an UPDATE after the ALTER in 1.2.

## 2. Ledger builder

- [x] 2.1 Add `visit_ledger()` to `claim_status.py`: single query `bank_transactions` (vet_flag=1) LEFT JOIN `vet_claims` LEFT JOIN `pets`, ordered by date desc
- [x] 2.2 Group rows per transaction into `{txn, claims: [...], claim_count}`; a charge with zero claims yields an empty claim list (no-invoice row). Live reality: every vet txn already carries a `pending_match` claim (vet_detection), so no-invoice usually renders as that status; the zero-claim branch is defensive.
- [x] 2.3 Compute `expected_reimbursement` per claimable claim: excess drained greedily across a (pet, condition, policy year) group in charge-date order, then bounded by `annual_cap`. Policy year = calendar year (approximation, noted in code).
- [x] 2.4 When a pet's `annual_excess`/`annual_cap` is NULL, or the claim has no invoice yet, mark expected unavailable — never a guessed number
- [x] 2.5 Ceiling invariant holds via `claimable_amount` upstream (line items ≤ charge); split sub-rows show claimable, ceiling stays on the anchor

## 3. Route wiring

- [x] 3.1 Call `visit_ledger()` in `main.py::dashboard()` and pass it to the template
- [x] 3.2 Removed the parallel `needs_pet`/`pending_match`/`matched`/`drafted` queries — ledger covers them. `dashboard_lists()` (needs_action, settled_reconciliation, unclassified) still feeds its own sections.
- [x] 3.3 Row actions preserve `claim_id`: assign-pet, set-condition, mark-sent, invoice-request-sent, confirm-resolved, link-event all wired from ledger rows. Verified `/` returns 200 with actions rendered.

## 4. Template — single ledger

- [x] 4.1 Replaced the two-table region of `index.html` with one ledger table carrying all preserved columns
- [x] 4.2 Flat row for `claim_count <= 1`; empty/pending_match charge renders "No invoice" + invoice-request action
- [x] 4.3 Anchor + per-claim sub-rows for `claim_count > 1` (always-open, CSS-only, no JS); ceiling on anchor, claimable on sub-rows
- [x] 4.4 Status column spans the lifecycle (no-invoice → matched → drafted → sent → … → settled/declined)
- [x] 4.5 Expected-reimbursement column shows the estimate (est.) or "unavailable"
- [x] 4.6 Carried the mock's design system (theme vars, chips, pet dots, Georgia masthead) into the live template

## 5. Verify

- [x] 5.1 Added `test_visit_ledger_four_shapes` + `test_visit_ledger_expected_after_excess_and_settled_actual` to `tests/test_core.py` (flat, split, no-invoice, missing-excess, over-excess, settled-actual)
- [x] 5.2 `./.venv/Scripts/python.exe tests/test_core.py` — ALL TESTS PASSED (created venv from requirements.txt for this checkout)
- [~] 5.3 Loaded `/` and `/basic` via TestClient against a SEEDED DB — both 200, each visit appears once, split expands, arthritis batch expected $0 (under $150 excess), Echo unavailable, settled shows actual. NOT yet loaded against Justin's REAL `app/data/openclaw.db` (absent in this checkout) — do on the deployment box after the 1.2 ALTER.
- [x] 5.4 Verification notes recorded here (live-real-DB check still outstanding — see 5.3 / 1.2)

## 6. Basic mobile status view (discussed this session)

- [x] 6.1 Added `GET /basic` route serving `basic.html` — outstanding + recently-paid, phone-first stacked cards (460px, no horizontal scroll), from the same `visit_ledger()`
- [x] 6.2 Linked `/` ⇄ `/basic`. Telegram left design-only (no bot/tunnel) per this session's decision.
