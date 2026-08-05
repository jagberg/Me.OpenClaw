# settlement-validation Specification

## Purpose
Check every Petcover settlement with two independent checks — is their own arithmetic right, and did they assess what we submitted — and flag (never auto-dispute) any difference so Justin can review. Where the letter states its own line items, the expectation is re-added from those figures; where it does not, it falls back to the deterministic policy model (claimable subtotal minus the per-condition-thread excess, once per *current* policy year, bounded by the pet's remaining annual cap), degrading to "assume already claimable" when a claim's own transaction falls in an already-closed policy year or the pet's anniversary is unknown.

See ADR-0011 for the origin of the excess/policy-year model; see ADR-0013 (including its 2026-07-24 amendment) for the transaction-date-bucketing and closed-year-default correction this spec reflects, and for the related (but distinct) decision to gate *submission* on the same math.

**Revision note (2026-07-24):** a live audit found the original policy-year bucketing (keyed off "now", the time a reply was processed) was wrong — two real claims processed the same week belonged to different policy years by their own transaction dates. This also prompted dropping an intermediate idea to reverse-engineer Petcover's own internal excess mechanics (observed varying between $0 and $105 across real letters) from limited data; the model deliberately stays simple and compares against Petcover's reported figures rather than trying to replicate them.

## Requirements

### Requirement: Settlements are validated by two independent checks, never fused into one flag
On a settlement or approval event carrying a paid amount, the system SHALL run **two independent checks** and SHALL NOT fuse them into one flag, because they ask different questions of different parties.

**Check A — Petcover's own arithmetic.** When the letter states its own line items, expected paid SHALL be re-added from those stated figures and nothing else:

```
expected_paid = (claimed_amount - fixed_excess_stated - non_claimable_stated
                 - percentage_excess_stated)
                * (1 - age_contribution_percent)
```

with `age_contribution_percent` read from the letter (`Age Contribution: $12.25 [35%]` → `0.35`) and each absent term treated as `0.00`. Check A SHALL be skipped entirely when the letter states no percentage, rather than assuming one. A difference greater than `SETTLEMENT_TOLERANCE` ($2.00) flags the claim naming Petcover's own figures — this is an arithmetic dispute with Petcover and the only check that may say so. Verified exact to the cent against all ten live approval letters (2026-08-04); `DC1-27-5628` Tr 8, the only one with a non-zero non-claimable amount ($580.74 − $135.00 → $289.73), is the one that proves the deduction order.

**Check B — does the assessed amount match what we submitted under this serial.** `claimed_amount` SHALL be compared to this claim's recorded claimable subtotal (see `claimable-subtotal-provenance` — never the invoice total, and no comparison at all when the subtotal is unrecorded). A difference beyond tolerance flags the claim as an **assessment difference** naming the claim id, its reference and serial, the amount we submitted and the amount Petcover states it assessed. The system SHALL NOT re-route the settlement and SHALL NOT rewrite any historical row — re-routing moves money and is Justin's decision.

**Check B's flag SHALL name the likelier cause, and it is usually ours.** Where another claim's invoice matches Petcover's stated figure exactly, the flag SHALL say so and point at our serial→claim map; only where no invoice of ours matches is it a question for Petcover. Evidence, 2026-08-04: Petcover's status table of 2026-07-29 states a **treatment date per serial**, and against it every serial we hold was on the wrong claim (0 for 10) while every letter's stated amount matched its true claim's invoice to the cent (7 for 7). `_claim_for_sr` assigns a serial to "the oldest-transaction claim not yet serialized", an inference nothing ever confirmed, so an event whose serial came from that path SHALL record the fact (`sr_assigned_by`).

**The letter's stated excess wins.** When `fixed_excess_stated` is present, the system SHALL use it and SHALL NOT emit the phrase "fresh $150 excess this policy year" or its already-used counterpart. The inferred excess applies only when no letter figure exists.

**Fallback when the letter states no line items** (the older PDF style, `Amount Claimed` / `Total Payable` only): the system SHALL determine the claim's policy year from **its own transaction date** (never from when the reply was processed) and compute expected payout = the claim's recorded claimable subtotal minus the $150 excess (only when the thread has no other approved/settled claim whose own transaction also falls in that same, current policy year), bounded by the pet's remaining annual cap. Any query reading the claim's `petcover_reference` to find thread siblings SHALL see the reference the current letter teaches — the learn must precede the check.

Every flag is a warning to review in **either direction**, never an assertion that Petcover is wrong. The system SHALL NOT auto-dispute and SHALL NOT send mail.

**Revision (2026-08-04): this requirement was one fused check and is now two.** The single flag read *"we expected $430.74, Petcover paid $22.75 (fresh $150 excess this policy year)"* — an expectation built from an invoice total never submitted as claimable, an excess the letter had actually stated, and no mention of the one thing that did not reconcile. One flag cannot carry two findings and the one it carried was the false one.

The 2026-07-24 revision note below dropped an idea to "reverse-engineer Petcover's own internal excess mechanics (observed varying between $0 and $105 across real letters)". That observation was accurate and attributed to the wrong term: across the letters the **fixed excess** is what varies, while the age contribution is a constant 35% printed in the letter's own text. In choosing not to *infer* their mechanics we also stopped *reading the figures they state* — and `extract_approval_amounts` had been capturing three of them into the event detail for weeks, where nothing used them. Check A is arithmetic on given data, not a model of their policy: every input is a labelled field on the letter being validated.

#### Scenario: Petcover's arithmetic is correct and the assessed amount matches
- **WHEN** a letter states claimed $446.50, fixed excess $49.26, non-claimable $0.00, age contribution 35%, paid $258.21, and the claim's recorded claimable subtotal is $446.50
- **THEN** no flag is raised and the normal notification is sent

#### Scenario: Petcover's arithmetic is correct but they assessed a different amount
- **WHEN** an approval letter for claim #8 (`DC1-26-5992` Sr 1, recorded claimable subtotal $446.50) states claimed $351.50, fixed excess $150.00, age contribution 35%, paid $130.97
- **THEN** Check A passes and no arithmetic flag is raised
- **AND** Check B flags an assessment difference naming claim #8, the reference and serial, submitted $446.50, assessed $351.50 — and names the claim whose invoice $351.50 actually is
- **AND** the flag does not contain "fresh $150 excess this policy year", and no historical row is rewritten

#### Scenario: Petcover's arithmetic does not add up
- **WHEN** a letter states claimed $200.00, excess $0.00, non-claimable $0.00, age contribution 35% and paid $150.00, where the stated figures come to $130.00
- **THEN** the claim is flagged as an arithmetic difference naming all of Petcover's stated figures and the gap

#### Scenario: The letter states no percentage
- **WHEN** an `approved` event was written before the age-contribution pattern shipped and carries no `age_contribution_percent`
- **THEN** Check A is skipped rather than run against a missing term, and the gap is treated as historical

#### Scenario: The claimable subtotal is not recorded
- **WHEN** an approval letter states a paid amount for a claim whose `invoice_data` has no `claimable_amount` key (live: claim #2)
- **THEN** Check B does not run, no expected-payout figure is produced from the invoice total, and the claim is flagged that its subtotal is not recorded, naming the letter's stated figures

#### Scenario: The reference is learned by the same letter being validated
- **WHEN** a letter both teaches a claim its `petcover_reference` and states a paid amount, and a sibling already carries that reference with an approved event in the same policy year
- **THEN** the sibling is found — the validation sees the learned reference, not NULL — and the excess is not treated as fresh

#### Scenario: Older settlement style with no line items
- **WHEN** a settlement email carries only `Amount Claimed` and `Total Payable`
- **THEN** the transaction-date-bucketed excess/cap fallback runs and a difference beyond $2.00 in either direction is flagged

#### Scenario: Excess already used this policy year by an earlier-transaction sibling
- **WHEN** a thread has an earlier-transaction-dated claim already approved/settled within the current policy year, and a later-transaction-dated sibling pays less than its full claimable
- **THEN** the claim is flagged `settlement mismatch` naming both figures, since no further excess should have been deducted this year

#### Scenario: Petcover pays more than expected
- **WHEN** the reported paid amount exceeds our expectation by more than the tolerance
- **THEN** the claim is still flagged — both checks are bidirectional, not one-way shortfall tests

### Requirement: The approval letter's full line-item breakdown is captured
The system SHALL extract and persist, from each `Claim Approval` letter, every figure the letter states: `claimed_amount`, `paid_amount`, `fixed_excess_stated`, `non_claimable_stated`, `age_contribution_stated`, `age_contribution_percent` and `percentage_excess_stated`. This is the only email in the lifecycle that states them.

A term the letter omits SHALL be absent from the detail rather than stored as a guessed zero. All ten live `approved` letters state `Percentage Excess: $0.00 [0%]`; it has never been non-zero, so **its position in the order of operations is unverified** and is named as such in `_check_petcovers_arithmetic`.

The five `approved` events written before 2026-07-24 lack `age_contribution_stated` even though today's extraction reads it from those same emails. `_already_recorded` blocks a re-read from backfilling them, correctly — so the log's under-record is a known permanent historical gap, not something to repair by re-reading mail (ADR-0020).

#### Scenario: A full breakdown is captured
- **WHEN** a letter contains `Total amount claimed: $580.74`, `Fixed excess $0.00`, `Non‐claimable amount $135.00`, `Age Contribution: $156.01 [35%]`, `Percentage Excess: $0.00 [0%]`, `Paid by us: $289.73`
- **THEN** all seven figures are recorded, with `age_contribution_percent` as `0.35`

#### Scenario: A term the letter omits
- **WHEN** a letter states no `Non-claimable amount` line
- **THEN** `non_claimable_stated` is absent from the detail and the arithmetic treats it as $0.00, rather than the key being stored as a guessed zero

### Requirement: An unexplained assessment difference is not dismissible to invisible
Dismissing a settlement difference SHALL record, on the `mismatch_dismissed` event, the figures the difference was made of — the letter's stated claimed and paid amounts, the stated excess and non-claimable amount, the claim's recorded claimable subtotal, and which check raised it — not only the flag's prose. Where the dismissed difference was an **assessment** difference, the claim SHALL remain listed in the existing dashboard manual-review queue, because the question is still open.

Dismissal is one-way by design: `_validate_settlement` has one caller and `_already_recorded` blocks a re-read from re-flagging, so nothing un-dismisses and only a new letter carrying a paid amount flags again. That is correct for an arithmetic difference Justin has checked and wrong for a question he has asked and not had answered. Live proof: event 58 (2026-08-04) holds claim #2's entire dismissed difference as prose in `detail.dismissed_flag` with the claim's `flag` NULL, and nothing on any surface said a question was open.

#### Scenario: An arithmetic difference is dismissed
- **WHEN** Justin dismisses a difference raised by Check A
- **THEN** the event records the stated figures, the recorded claimable subtotal and `check: "arithmetic"`, and the claim leaves the review queue

#### Scenario: An assessment difference is dismissed
- **WHEN** Justin dismisses a difference raised by Check B
- **THEN** the event records the same figures with `check: "assessment"`, and the claim remains in the review queue with the submitted and assessed amounts visible

### Requirement: A claim in an already-closed policy year is assumed fully claimable
When a claim's own transaction date falls in a policy year that has already ended (relative to today), the system SHALL NOT apply any excess deduction — our claim history for a closed year is presumed incomplete (untracked spend, limited bank-CSV coverage), so the expectation is simply the full claimable subtotal. The same degradation applies when the pet's policy anniversary is unknown, since no year boundary can be determined at all.

#### Scenario: Claim's transaction predates the current policy year
- **WHEN** an approval/settlement event's claim has a transaction date before the pet's most recent policy anniversary
- **THEN** expected payout is the full claimable subtotal, with no excess deducted, regardless of other claims in that thread

#### Scenario: Anniversary not on record
- **WHEN** a settlement arrives for a pet without a stored policy anniversary
- **THEN** expected payout is the full claimable subtotal and any mismatch flag names the anniversary as unknown

## Known inconsistency — the older-style fallback does not net the 65% benefit rate (found 2026-08-04, undecided)

The dashboard's estimate now multiplies by `config.PETCOVER_BENEFIT_RATE` (see `dashboard-visit-ledger`, revised 2026-08-04). Check A takes the rate from each letter, so it agrees. **The older-style fallback above does not apply any rate** — it expects `claimable − excess`, bounded by cap.

So for a settlement arriving in the old `Amount Claimed` / `Total Payable` style, this capability expects roughly 35% more than the dashboard estimates for the same claim, and would flag a payment the dashboard considers correct.

Deliberately not resolved here. The 65% is confirmed as the policy's current term, but no old-style letter in the mailbox states a rate at all, so whether it applied to those settlements is **unrecorded** — and applying it to the fallback would assume it did. Every live letter is the newer style, so nothing currently takes this path; it is a latent disagreement, not an active one. Flagged rather than fixed for the same reason as the closed-year default below: changing either side silently would fabricate a decision.

## Known inconsistency — the dashboard disagrees about closed policy years (found 2026-07-25, undecided)

The closed-year default above is **not** applied by the dashboard's own estimate. `claim_status._apply_excess_and_cap` (see `dashboard-visit-ledger`) drains the $150 excess for every `(condition, policy year)` group, closed years included.

So for a claim whose transaction falls in a closed policy year, the dashboard displays an expected reimbursement $150 lower than this capability expects for the same claim.

Which is right is **unrecorded**. The closed-year default was Justin's explicit instruction for settlement validation; whether he intended it to govern the dashboard's estimates was never asked. Recorded in both specs and in `openspec/BACKLOG.md` rather than resolved in one of them — changing either path silently would fabricate a decision.
