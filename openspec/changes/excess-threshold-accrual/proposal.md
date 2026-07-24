# Hold claims below the annual excess; accrue per condition until claimable

## Why

Two Petcover letters (real, Jul 2026) declined claims not because they aren't covered but because the amount claimed is **under the fixed excess**: *"While it is a claimable condition, the amount you have claimed is under your fixed excess… Less Fixed excess: $150.00 / Outstanding excess: $-105.25."* The $150 fixed excess is per condition per policy year — a single small invoice below it always nets $0 and comes back below-excess. Today OpenClaw drafts and submits every matched claim regardless of size, so it manufactures these dead-end submissions and treats the reply as a plain decline, discarding the invoice. It should instead **not submit a condition's claim until that condition's claimable has accrued past the excess for the policy year**, and keep below-excess invoices alive so they roll into the eventual submission. The excess/policy-year math already exists (settlement-validation, ADR-0011; Aari's anniversary 09-23 is on record). Decision recorded in ADR-0013.

## What Changes

- **Accrual gate before submission**: a matched claim (pet + condition + invoice all known) is held at `matched` — not drafted — until the sum of claimable for its `(pet, condition, policy year)` **exceeds** $150. A prior settlement of that thread this policy year (excess already consumed) disables the gate — submit normally. Held claims show a plain-language holding flag on the dashboard/Telegram.
- **Below-excess outcome tracked distinctly**: Petcover's "under your fixed excess" letter classifies as a new `below_excess` event/status — NOT a terminal decline. The invoice is retained and continues to accrue toward the condition.
- **Auto-roll on release**: once a condition accrues past the excess, all its held `matched` claims and previously `below_excess`-declined claims for that policy year are drafted together as one batch (≤4 per Petcover form), which Justin sends himself (drafts only — never auto-send).
- **Near-anniversary expiry alert**: as the policy anniversary approaches, Telegram-alert any condition still holding below-excess claimable — those invoices will expire un-claimable at renewal.

## Capabilities

### New Capabilities
- `excess-threshold-accrual`: per-condition per-policy-year accrual math, the submit-when-accrued-past-excess gate, auto-roll of held/below-excess claims into one batch on release, and the near-anniversary expiry alert.

### Modified Capabilities
- `claim-status-tracking`: classification gains `below_excess` (recognized from the "under your fixed excess" wording), recorded as a non-terminal event that never discards the invoice.
- `claim-form-automation`: the matched→drafted step is gated on the accrual threshold; the released batch includes prior below-excess claims.

## Impact

- `claim_status.py` (classify + `below_excess` handling, accrual helper reusing the policy-year/excess logic), `claim_forms.py`/`pipeline.py` (accrual gate in the draft step, auto-roll batch, held-claim flag), `pipeline.py` (expiry alert reusing the `ops_alerts` dedupe machinery). No schema change — `below_excess` is a status string; holding is `matched` + flag.
- Depends on the already-shipped settlement-validation excess/policy-year math and `pets.policy_anniversary`.
- No new third-party calls; Telegram volume bounded (expiry alerts deduped like auth alerts).
