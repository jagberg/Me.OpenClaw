# ADR-0013: Hold a condition's claim until its accrued claimable exceeds the annual excess

**Date**: 2026-07-24
**Status**: accepted (design agreed; implementation pending — see openspec change `excess-threshold-accrual`)
**Deciders**: Justin

## Context

Two real Petcover replies (Jul 2026) declined claims not because the condition isn't covered, but because the amount claimed is under the $150 fixed excess: *"While it is a claimable condition, the amount you have claimed is under your fixed excess… Less Fixed excess: $150.00 / Outstanding excess: $-105.25."* Today the pipeline drafts and submits every matched claim regardless of size, manufacturing these dead-end submissions, and the reply was being absorbed into the ordinary acknowledged/declined lifecycle with no distinction. Justin's own framing, mid-conversation: track these, and — on reflection — don't submit a condition's claim at all until the year's threshold has accrued.

## Decision

1. A matched claim is held (`matched` + holding flag) rather than drafted until its condition's accrued claimable for the current policy year **exceeds** $150 (strictly — at exactly $150 the payout is $0). If the thread already consumed its excess this policy year (a prior settlement), the gate is disabled.
2. A "below your fixed excess" reply is classified as a new non-terminal status, `below_excess` — distinct from `declined`. Its invoice is retained and keeps accruing.
3. When a condition's accrual crosses the threshold, its held and below-excess claims draft together as one batch (Justin still sends manually).
4. Near the policy anniversary, alert on any condition still holding claimable that will expire un-claimable at renewal.

## Alternatives considered

- **Submit every matched claim, let Petcover reply below-excess** (status quo) — rejected: manufactures known-dead submissions and silently absorbed the below-excess reply as an ordinary decline, losing the invoice.
- **Release at accrued ≥ $150** — rejected: a claim submitted at exactly $150 nets $0 (Justin's read of the letter's own math: "Outstanding excess: $-105.25" implies the excess must be *exceeded*, not just met).
- **Flag Justin to manually decide when to submit** instead of auto-rolling on release — rejected in favor of auto-roll: the accrual math is deterministic and the same logic that already validates settlements (ADR-0011/settlement-validation) can gate submission with no added judgment call.

## Consequences

### Positive
- No more manufactured below-excess submissions; invoices are never dropped while a condition accrues.
- Reuses the existing per-thread, per-policy-year excess/cap math (settlement-validation) rather than inventing new bookkeeping.

### Negative / Risks
- A condition's claimable is tracked pre-Petcover-adjudication (in-flight claims count toward accrual); if Petcover later disallows an item the accrual was optimistic — worst case: submit slightly early, get a fresh below-excess reply, re-hold. Fail-safe, not fail-silent.
- A condition never crossing the threshold before the policy anniversary loses those invoices permanently (the expiry alert is the mitigation, not a fix).
- The two already-existing below-excess replies (currently modeled as acknowledged/declined) are a **correction of an original modeling gap**, not a reversed decision — `below_excess` didn't exist as a concept before this ADR, so those claims were never correctly classifiable. They are relabeled, not reinterpreted.

## Amendment (2026-07-24) — transaction-date bucketing + closed-year default

A 2-day live audit of claims + Petcover comms (thread DC1-27-5628, Sr2/Sr4) surfaced two corrections to decision 1 above, made **before** this ADR's implementation started (the openspec change is still pending) — recorded as an amendment, not a reversal, since nothing had shipped against the original wording yet:

1. **"Current policy year" must be judged by each claim's OWN transaction date, not by "now"/when a reply is processed.** Real data: Sr2's transaction (2025-08-08) and Sr4's transaction (2025-09-26) straddle the pet's 09-23 anniversary — different policy years — despite both being *processed* the same week in July 2026. Bucketing by "now" would have wrongly merged them.
2. **Closed-policy-year default (Justin's explicit call): our claim history for any policy year that has already ended is presumed incomplete** — some vet spend never hits the tracked card, and bank-CSV coverage doesn't reach arbitrarily far back. So the $150-accrual gate and the settlement expected-payout math only apply to the CURRENT, still-open policy year. A claim whose own transaction falls in an already-closed year (or whose pet has no anniversary at all) is assumed to have already passed the threshold and is **not held** — it drafts/settles on the same footing as an over-threshold current-year claim.

A related, narrower correction: rather than trying to reverse-engineer Petcover's own internal excess/co-pay math from limited data (an earlier draft of this fix briefly modeled a cumulative running excess balance to mirror figures like "Less Fixed excess: $105.00"), the settlement side stays deliberately simple — our own $150-once-per-year expectation, compared against whatever Petcover actually reports, with any mismatch (either direction) surfaced as a warning. We compare against Petcover's numbers; we don't try to become them.

This amendment, along with the `approved`/`below_excess` classification, shipped as a hotfix to `claim_status._validate_settlement` ahead of and independent from the `excess-threshold-accrual` openspec change, since the settlement-validation capability (ADR-0011) had the identical "now"-based bucketing bug and needed the fix regardless of whether the submission-gating feature was built. `excess-threshold-accrual`'s design was updated to build on this corrected model rather than re-deriving it.
