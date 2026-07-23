# Tasks — condition-thread-tracking

Legend: `[x]` code + hermetic test done; live-DB/Gmail steps flagged **LIVE** stay
open until run against the real DB in the deploy worktree container.

## 1. Schema + backfill

- [x] 1.1 `petcover_sr INTEGER` on `vet_claims`, `policy_anniversary TEXT` on `pets` (both mirrored in `db.py` schema + additive migration `_migrate_added_columns`); `ops_alerts` table added (CREATE IF NOT EXISTS). **LIVE**: run the two `ALTER TABLE` against `app/data/openclaw.db` before deploy (migration only touches fresh DBs).
- [ ] 1.2 **LIVE** Backfill `petcover_sr` for claims 18/19/21 from the July acks (Sr 2/3/4, oldest-txn-first).
- [ ] 1.3 **LIVE** Mine renewal emails for Aari's policy anniversary → `pets.policy_anniversary` (fallback: ask Justin on Telegram); record what was found.

## 2. Event routing (claim_status.py)

- [x] 2.1 `extract_sr` — reads `SR N` only where it sits right after the reference (anchored, can't misfire).
- [x] 2.2 Routing precedence in `process_reply`: (reference, Sr) → single claim; reference-only → thread's non-terminal claims; shared `TERMINAL_STATUSES`. Reference finders (`find_claim_by_reference_and_sr`, `find_claims_by_reference`) carry `_txn_date` for Sr assignment.
- [x] 2.3 Thread isolation: `find_claims_by_reference` excludes `settled`/`declined`; decline routes only to its own reference. Tests prove sibling threads/settled claims untouched.
- [x] 2.4 Tests: `test_route_reference_and_sr_to_single_claim`, `test_reference_reuse_never_touches_settled_claims`, `test_decline_isolated_to_its_thread`.

## 3. Ack correlation (claim_status.py)

- [x] 3.1 Reference + Sr parsed from letters; condition matched by content (the submission's own `condition_text` appearing in the letter) rather than parsing Petcover's phrase — Petcover re-conditions documents, so their printed condition is deliberately NOT trusted to overwrite ours.
- [x] 3.2 `correlate_ack` replaces pet-only pool: condition-content → most-recently-sent fallback; per-Sr letters assign within a submission oldest-txn-first (`_claim_for_sr`).
- [x] 3.3 Recency fallback leaves `condition_text` untouched (`test_ack_recency_fallback_leaves_condition_untouched`).
- [x] 3.4 Tests: condition decides, recency fallback, same-day distinct submissions, re-conditioned document, batch Sr assignment (`test_batch_ack_assigns_serials_oldest_txn_first`).

## 4. Settlement validation (claim_status.py + pipeline notify)

- [x] 4.1 `_validate_settlement`: expected = claimable − excess-if-thread-unconsumed-this-policy-year, bounded by remaining cap; $2 tolerance; degraded rule (thread-lifetime excess, unbounded cap, "anniversary unknown" wording) when the anniversary is missing.
- [x] 4.2 Shortfall → `flag = "settlement short — expected $X, paid $Y (...)"`; pipeline `_review_pdf` attaches the settlement letter's own PDF (via the settled event's `raw_email_id`), `_REVIEW_FLAG_MARKERS` gains `"settlement short"`.
- [ ] 4.3 **LIVE** Seed pre-system thread history (Feb 2026 arthritis settlement) so excess state starts correct.
- [x] 4.4 Tests: second-settlement-same-year shortfall, within-tolerance no-flag, unknown-anniversary degradation, anniversary boundary re-deducts excess.

## 5. Gmail auth alerting (pipeline.py)

- [x] 5.1 `_ensure_gmail_auth` probes credentials at the top of the Gmail phase; `_is_gmail_auth_failure` distinguishes `RefreshError`/missing-token from transient errors (which re-raise); auth death skips the Gmail-dependent tick.
- [x] 5.2 `ops_alerts` state: ≤5 alerts / rolling 24h; recovery confirmed once on first success after alerts; rows persist so a restart can't re-spam.
- [x] 5.3 Tests: `test_gmail_auth_alert_caps_at_five_per_day`, `test_gmail_auth_recovery_confirmed_once_and_resets`.

## 6. Continuation default (claim_forms.py)

- [x] 6.1 `process_claim`/`process_claim_batch` default `continuation=True`; `test_continuation_box_defaults_ticked` asserts the form field `/0` and both defaults.

## 7. Ship + live verify

- [x] 7.1 Full suite green (79 tests). **LIVE**: commit, deploy worktree compose rebuild.
- [ ] 7.2 **LIVE**: next tick processes the 23 Jul Petcover emails (DC1-26-5978 Sr1 + DC1-27-5628 Sr3 letters) — verify routing lands on the right claims and nothing touches settled ones; record results here.
