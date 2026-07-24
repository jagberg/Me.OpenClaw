# Tasks — excess-threshold-accrual

## 1. below_excess classification (claim_status.py)

- [ ] 1.1 Add `below_excess` to classification, keyed on the body phrase "under your fixed excess"; ordered ahead of `acknowledged` (below-excess letters use the acknowledgement subject). Add a body-match path since the subject is generic.
- [ ] 1.2 `process_reply`: `below_excess` sets status `below_excess`, keeps `invoice_data`/invoice intact; NOT added to `TERMINAL_STATUSES`; routes via the same (reference, Sr)/reference/ack precedence.
- [ ] 1.3 Tests: below-excess body under an acknowledgement subject classifies `below_excess`; a submitted claim → status `below_excess`, invoice retained, not terminal.

## 2. Accrual math + gate (claim_status.py)

- [ ] 2.1 `condition_accrual(pet_id, condition_text, on_date)` — sum claimable of non-terminal, non-settled claims for `(pet, condition, policy year)` (matched/below_excess/sent/acknowledged/info_requested/suspended); reuse `_policy_year_start`; degrade to lifetime when anniversary unknown.
- [ ] 2.2 `accrued_over_excess(pet_id, condition_text, on_date)` — True if the thread already settled this policy year (excess consumed) OR accrual **> POLICY_EXCESS** (strict).
- [ ] 2.3 Tests: under/at/over $150 boundaries; excess-consumed-this-year disables the gate; case-insensitive condition grouping; unknown-anniversary lifetime degrade.

## 3. Draft-step gate + auto-roll (pipeline.py + claim_forms.py)

- [ ] 3.1 In `_draft_matched_claims`, before batching: for each ready matched claim, skip (hold) unless `accrued_over_excess`; held claims get flag `holding — <condition> accrued $X of $150 excess; waiting for more invoices`.
- [ ] 3.2 On release, include the condition's `below_excess` claims in the ready pool (they already have invoices); re-drafting reuses the existing `invoice_file_path`, moves them back to `drafted`. Batch ≤4 deterministically (txn date, id).
- [ ] 3.3 Tests: below-threshold condition holds (no draft, holding flag); over-threshold releases held + below_excess claims as ≤4 batches; excess-consumed condition drafts immediately.

## 4. Near-anniversary expiry alert (pipeline.py)

- [ ] 4.1 `EXCESS_EXPIRY_ALERT_DAYS = 30`. Within the window before a pet's anniversary, for each `(pet, condition, policy year)` with accrual `> 0` and `<= 150`, send one Telegram alert (condition, accrued $, renewal date); dedupe via `ops_alerts` kind `excess_expiry:<pet>:<condition>:<policy_year>`; reset next policy year.
- [ ] 4.2 Tests: alert fires once inside the window for a below-excess condition; not re-sent same policy year; not sent when accrual already exceeds the excess or is zero.

## 5. Ship + live verify

- [ ] 5.1 Full suite green; commit; deploy worktree compose rebuild.
- [ ] 5.2 **LIVE**: reclassify the 2 existing below-excess claims (currently sit under acknowledgement/decline) to `below_excess` so they enter accrual — reprocess their letters or a one-off UPDATE; record which claims.
- [ ] 5.3 **LIVE**: confirm the arthritis invoices hold (nothing drafts until the pool exceeds $150), and a synthetic over-threshold case releases a batch draft; record results here.
