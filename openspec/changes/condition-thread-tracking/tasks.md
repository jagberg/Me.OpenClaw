# Tasks — condition-thread-tracking

Legend: `[x]` code + hermetic test done; live-DB/Gmail steps flagged **LIVE** stay
open until run against the real DB in the deploy worktree container.

## 1. Schema + backfill

- [x] 1.1 `petcover_sr INTEGER` on `vet_claims`, `policy_anniversary TEXT` on `pets` (both mirrored in `db.py` schema + additive migration `_migrate_added_columns`); `ops_alerts` table added. **LIVE DONE**: `init_db()` (which now runs the additive migration) applied against `app/data/openclaw.db` — columns + `ops_alerts` present. DB backed up first (`openclaw.db.bak-preconditionthread`).
- [x] 1.2 **LIVE DONE** Backfilled `petcover_sr` on the DC1-27-5628 arthritis thread oldest-txn-first: #21 (2025-08-08)=Sr2, #19 (2025-09-11)=Sr3, #18 (2025-09-26)=Sr4 (Sr1 = the pre-system Feb-2026 settlement, confirmed in Gmail 2026-02-03).
- [ ] 1.3 **LIVE — BLOCKED on Justin** No renewal / policy-schedule / certificate email exists in Gmail (searched Petcover senders + subject variants), so the anniversary can't be mined. `pets.policy_anniversary` left NULL → settlement validation runs in its degraded (thread-lifetime, cap-unbounded, "anniversary unknown" wording) mode. Needs Justin to supply Aari's policy anniversary (MM-DD).

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
- [x] 4.3 **LIVE — deliberately not seeded.** The Feb-2026 SR1 settlement predates the system (no claim/txn row; `transaction_id` is NOT NULL, so seeding needs synthetic production rows). Analysis: a *missing* thread-settlement record is fail-open — it can only make us expect *less* (claimable − excess), never raise a false shortfall flag (we flag only when paid < expected). Not worth polluting the live DB. Documented as a known limitation: the first real settlement of #18/19/21 may under-flag, never over-flag.
- [x] 4.4 Tests: second-settlement-same-year shortfall, within-tolerance no-flag, unknown-anniversary degradation, anniversary boundary re-deducts excess.

## 5. Gmail auth alerting (pipeline.py)

- [x] 5.1 `_ensure_gmail_auth` probes credentials at the top of the Gmail phase; `_is_gmail_auth_failure` distinguishes `RefreshError`/missing-token from transient errors (which re-raise); auth death skips the Gmail-dependent tick.
- [x] 5.2 `ops_alerts` state: ≤5 alerts / rolling 24h; recovery confirmed once on first success after alerts; rows persist so a restart can't re-spam.
- [x] 5.3 Tests: `test_gmail_auth_alert_caps_at_five_per_day`, `test_gmail_auth_recovery_confirmed_once_and_resets`.

## 6. Continuation default (claim_forms.py)

- [x] 6.1 `process_claim`/`process_claim_batch` default `continuation=True`; `test_continuation_box_defaults_ticked` asserts the form field `/0` and both defaults.

## 7. Ship + live verify

- [x] 7.1 Full suite green (79 tests); committed `3aa5e60`; container `meopenclaw-telegram-claimquery-app-1` rebuilt + recreated from the worktree, clean startup.
- [x] 7.2 **LIVE VERIFIED**: reprocessed the 23 Jul letters through the deployed code — `DC1-27-5628 Sr3` routed to claim **#19 only** (its exact serial), leaving #18/#21 and their statuses untouched (unclassified never regresses); `DC1-26-5978 Sr1` correlated to claim **#22** by pet (new re-conditioned thread), learning ref+Sr. Both letters classify `unclassified` (subject "Petcover Insurance Claim for Ari" has no status keyword) → linked to the right claim for review. NOTE: that letter type isn't classified — a keyword-coverage follow-up, separate from this routing change.
