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
