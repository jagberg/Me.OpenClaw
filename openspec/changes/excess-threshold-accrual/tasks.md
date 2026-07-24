# Tasks — excess-threshold-accrual

## 1. below_excess / approved classification (claim_status.py)

- [x] 1.1 **Already shipped** (2026-07-24, ahead of this change, as part of the `_validate_settlement` hotfix — see ADR-0013): `approved` and `below_excess` added to classification, keyed on confirmed-live body phrases ("Your claim has been approved" / "Claim assessment outcome: Under excess"), since the real subject is the generic "Petcover Insurance Claim for Ari". No further work.
- [x] 1.2 **Already shipped**: `process_reply` sets status `below_excess`/`approved`, keeps `invoice_data`/invoice intact; neither in `TERMINAL_STATUSES`; routes via the same (reference, Sr)/reference/ack precedence.
- [x] 1.3 **Already shipped**: tests `test_classify_approved_and_below_excess`, `test_process_reply_approved_validates_and_flags_from_real_shape` (app/tests/test_core.py).

## 2. Accrual math + gate (claim_status.py)

- [ ] 2.1 `condition_accrual(pet_id, condition_text, txn_date)` — sum claimable of non-terminal claims for `(pet, condition, current policy year)`, each bucketed by **its own transaction date** via the shipped `_policy_year_start` (matched/below_excess/sent/acknowledged/info_requested/suspended/approved). A claim whose own transaction date is in a CLOSED prior policy year, or whose pet has no anniversary on record, is excluded from the pool entirely (see 2.2).
- [ ] 2.2 `accrued_over_excess(pet_id, condition_text, txn_date)` — True immediately if `txn_date`'s policy year isn't the current open one, or the anniversary is unknown (closed-year/unknown bypass, Justin's rule — assume already past threshold, our history there is incomplete). Otherwise: True if the thread already has an approved/settled sibling whose own transaction is also in the current year (excess already used this year), OR the current-year accrual **> POLICY_EXCESS** (strict, reusing `claim_status.POLICY_EXCESS`).
- [ ] 2.3 Tests: under/at/over $150 boundaries within the current year; excess-already-used-this-year disables the gate; case-insensitive condition grouping; closed-year claim bypasses the gate entirely; unknown-anniversary bypasses the gate entirely.

## 3. Draft-step gate + auto-roll (pipeline.py + claim_forms.py)

- [ ] 3.1 In `_draft_matched_claims`, before batching: for each ready matched claim, skip (hold) unless `accrued_over_excess`; held claims get flag `holding — <condition> accrued $X of $150 excess; waiting for more invoices`.
- [ ] 3.2 On release, include the condition's `below_excess` claims in the ready pool (they already have invoices); re-drafting reuses the existing `invoice_file_path`, moves them back to `drafted`. Batch ≤4 deterministically (txn date, id).
- [ ] 3.3 Tests: below-threshold current-year condition holds (no draft, holding flag); over-threshold releases held + below_excess claims as ≤4 batches; excess-already-used-this-year condition drafts immediately; closed-year claim drafts immediately without waiting.

## 4. Near-anniversary expiry alert (pipeline.py)

- [ ] 4.1 `EXCESS_EXPIRY_ALERT_DAYS = 30`. Within the window before a pet's anniversary, for each `(pet, condition, current policy year)` with accrual `> 0` and `<= 150`, send one Telegram alert (condition, accrued $, renewal date); dedupe via `ops_alerts` kind `excess_expiry:<pet>:<condition>:<policy_year>`; reset next policy year.
- [ ] 4.2 Tests: alert fires once inside the window for a below-excess condition; not re-sent same policy year; not sent when accrual already exceeds the excess or is zero.

## 5. Ship + live verify

- [ ] 5.1 Full suite green; commit; deploy worktree compose rebuild.
- [x] 5.2 **LIVE — already done via the hotfix**: the real below-excess/approved letters were reprocessed live and confirmed classifying correctly (Sr4 "Under excess", Sr2 "Claim Approval") ahead of this change.
- [ ] 5.3 **LIVE**: confirm current-year invoices hold (nothing drafts until the pool exceeds $150), a closed-year claim drafts immediately, and a synthetic over-threshold case releases a batch draft; record results here.
