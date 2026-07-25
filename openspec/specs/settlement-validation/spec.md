# settlement-validation Specification

## Purpose
Check every Petcover settlement against a simple, deterministic policy expectation — claimable subtotal minus the per-condition-thread excess (once per *current* policy year), bounded by the pet's remaining annual cap — and flag (never auto-dispute) any mismatch against what Petcover actually reports so Justin can review, degrading to "assume already claimable" when a claim's own transaction falls in an already-closed policy year or the pet's anniversary is unknown.

See ADR-0011 for the origin of the excess/policy-year model; see ADR-0013 (including its 2026-07-24 amendment) for the transaction-date-bucketing and closed-year-default correction this spec reflects, and for the related (but distinct) decision to gate *submission* on the same math.

**Revision note (2026-07-24):** a live audit found the original policy-year bucketing (keyed off "now", the time a reply was processed) was wrong — two real claims processed the same week belonged to different policy years by their own transaction dates. This also prompted dropping an intermediate idea to reverse-engineer Petcover's own internal excess mechanics (observed varying between $0 and $105 across real letters) from limited data; the model deliberately stays simple and compares against Petcover's reported figures rather than trying to replicate them.

## Requirements

### Requirement: Settlements are validated against expected payout, bucketed by the claim's own transaction date
On a settlement or approval event carrying a paid amount, the system SHALL determine the claim's policy year from **its own transaction date** (never from when the reply was processed) and compute expected payout = the claim's claimable subtotal minus the $150 excess (only when the thread has no other approved/settled claim whose own transaction also falls in that same, current policy year), bounded by the pet's remaining annual cap for that year. The system SHALL flag the claim and notify Telegram when the amount Petcover actually reports differs from this expectation by more than a $2 tolerance, in **either direction** — the flag is a warning to review, not an assertion that Petcover is wrong. The system SHALL NOT auto-dispute.

#### Scenario: Excess already used this policy year by an earlier-transaction sibling
- **WHEN** a thread has an earlier-transaction-dated claim already approved/settled within the current policy year, and a later-transaction-dated sibling pays less than its full claimable
- **THEN** the claim is flagged `settlement mismatch` naming both figures, since no further excess should have been deducted this year

#### Scenario: Settlement pays the expected amount
- **WHEN** the reported paid amount is within $2 of expected
- **THEN** no flag is raised and the normal notification is sent

#### Scenario: Petcover pays more than expected
- **WHEN** the reported paid amount exceeds our expectation by more than the tolerance
- **THEN** the claim is still flagged as a mismatch — the check is bidirectional, not a one-way shortfall test

### Requirement: A claim in an already-closed policy year is assumed fully claimable
When a claim's own transaction date falls in a policy year that has already ended (relative to today), the system SHALL NOT apply any excess deduction — our claim history for a closed year is presumed incomplete (untracked spend, limited bank-CSV coverage), so the expectation is simply the full claimable subtotal. The same degradation applies when the pet's policy anniversary is unknown, since no year boundary can be determined at all.

#### Scenario: Claim's transaction predates the current policy year
- **WHEN** an approval/settlement event's claim has a transaction date before the pet's most recent policy anniversary
- **THEN** expected payout is the full claimable subtotal, with no excess deducted, regardless of other claims in that thread

#### Scenario: Anniversary not on record
- **WHEN** a settlement arrives for a pet without a stored policy anniversary
- **THEN** expected payout is the full claimable subtotal and any mismatch flag names the anniversary as unknown

## Known inconsistency — the dashboard disagrees about closed policy years (found 2026-07-25, undecided)

The closed-year default above is **not** applied by the dashboard's own estimate. `claim_status._apply_excess_and_cap` (see `dashboard-visit-ledger`) drains the $150 excess for every `(condition, policy year)` group, closed years included.

So for a claim whose transaction falls in a closed policy year, the dashboard displays an expected reimbursement $150 lower than this capability expects for the same claim.

Which is right is **unrecorded**. The closed-year default was Justin's explicit instruction for settlement validation; whether he intended it to govern the dashboard's estimates was never asked. Recorded in both specs and in `openspec/BACKLOG.md` rather than resolved in one of them — changing either path silently would fabricate a decision.
