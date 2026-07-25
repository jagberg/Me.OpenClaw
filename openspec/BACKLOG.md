# Backlog

Work that is genuinely open, pulled out of changes that were archived because they **shipped**. Without this file these items would disappear: an archived change isn't a tracker, and leaving a change unarchived to hold two stragglers is what left `openspec/specs/` stubbed for months.

Each entry says where it came from, so its original reasoning is still reachable.

## Blocked on Justin

### Echo / Bow Wow Insurance — the claim process itself
*From `vet-claim-automation` task 6.0. Capability: `vet-payment-detection`, `claim-form-automation`.*

Bow Wow's template format, submission method (email vs portal) and required fields are all unknown until Justin asks them. Until then Echo's claims stop at `matched` with a "process not yet defined" flag — deliberately, rather than guessing a process.

**Six claims, ~$6.6k**, of which two account for ~$5.4k. This is the largest outstanding number in the system and no code change can clear it.

## Decisions needed

### Do closed policy years drain the excess on the dashboard?
*Found 2026-07-25 during the baseline sync. Capability: `dashboard-visit-ledger` vs `settlement-validation`.*

Two shipped code paths disagree about the same domain fact:

- the dashboard ledger (`_apply_excess_and_cap`) drains the $150 excess for **every** `(condition, policy year)` group, closed years included;
- settlement validation (`_validate_settlement`, per ADR-0013's amendment) treats a claim whose transaction falls in an **already-closed** policy year as having passed the threshold already, because our history for a closed year is presumed incomplete.

So a last-year claim shows an expected reimbursement $150 lower on the dashboard than settlement validation expects for the same claim.

The closed-year default was Justin's explicit instruction for settlement validation. Whether he meant it to govern the dashboard's estimates too **was never asked**. Not resolved either way, because silently changing either path would fabricate a decision.

## Deferred features

### Dashboard view of open split/merge proposals
*From `fix-email-matching-gaps` tasks 7.5 and 9.6 — deferred at the time with "at some stage".*

Merge proposals and inadequate-invoice items are actionable from Telegram but have no dashboard list. Not blocking anything; Telegram covers the actual workflow.

### Assistant-side reminders don't push
*Found 2026-07-25 during the baseline sync. Capability: `reminder-scheduling`.*

ADR-0003 chose dashboard-only reminders with no push, deliberately. That deferral was later lifted for the *claims* side (Telegram), but a task reminder coming due is still only visible if Justin opens the dashboard. Whether it should now push was never asked — a gap, not a decision.

### Claim #17 vision-OCR retry never resumed
*Ongoing operational item, no owning change.*

Claim #17's vision-OCR has attempted once in six days despite two attempts remaining and the source email still being found by the live query (maxResults truncation ruled out). Root cause unidentified; needs a live trace of `match_claim`'s vision branch rather than a guess.

### Claim #21 figure discrepancy
*Ongoing operational item. Capability: `settlement-validation`.*

We extracted a claimable of $44.75; Petcover's approval letter states $35.00 claimed and $22.75 paid. The mismatch is flagged and visible via `claim_detail`, but which figure is wrong — our extraction or their assessment — has not been investigated.

### ADR-0012 successor: derive the continuation box from Condition Thread existence
*Recorded in ADR-0012 as future work.*

The continuation box is currently hard-defaulted to ticked. Now that Condition Threads are modelled (ADR-0011), it could be derived. Unbuilt.
