# dashboard-visit-ledger Specification

## Purpose
One transaction-anchored ledger on the dashboard: every row is a `vet_flag = 1` bank charge, appearing exactly once no matter how many claims derive from it, expandable into per-claim sub-rows. Replaces the earlier pair of standalone claims and visits tables, which displayed the same charge twice.

Implemented by `claim_status.visit_ledger()` + `main.py`/`templates/index.html`.

## Requirements

### Requirement: Single transaction-anchored visit ledger
The dashboard SHALL present bank charges and their derived claims as one ledger in which every row is anchored on a `bank_transaction` with `vet_flag = 1`. A charge SHALL appear exactly once regardless of how many claims derive from it. The dashboard SHALL NOT render a separate standalone claims table and a separate standalone visits table for the same transactions.

#### Scenario: Charge appears once
- **WHEN** a bank transaction has one derived claim
- **THEN** the ledger shows a single row carrying both the charge (date, merchant, amount) and that claim's fields (pet, condition, claimable, status, reference, last event, action)

#### Scenario: No cross-table duplication
- **WHEN** the ledger renders
- **THEN** no transaction's charge amount is displayed in two separate tables

### Requirement: Charge is the ceiling; claimable never exceeds it
Each ledger row SHALL display the bank charge as the ceiling and the claimable subtotal (line items minus `NON_CLAIMABLE_KEYWORDS`) as a distinct value, consistent with ADR-0007. Where a charge splits across multiple claims, the sum of the claims' claimable subtotals SHALL NOT exceed the charge.

#### Scenario: Ceiling and claimable shown separately
- **WHEN** a claim's claimable subtotal is less than its charge (card surcharge, non-claimable items)
- **THEN** the row shows the charge as ceiling and the smaller claimable value alongside it, not one merged figure

#### Scenario: Split claims sum within ceiling
- **WHEN** a single charge splits into multiple per-pet claims
- **THEN** the anchor row shows the full charge and the sum of the sub-rows' claimable subtotals is at most that charge

### Requirement: Split charges expand into per-claim sub-rows
When a single charge has more than one derived claim (multi-pet or multi-invoice), the anchor row SHALL carry the charge and a claim count, and SHALL be expandable into one sub-row per claim showing that claim's pet, condition, claimable subtotal, and status.

#### Scenario: Multi-pet charge expands
- **WHEN** a charge produced a claim for one pet and a non-claimable line for another (e.g. arthritis consult plus a vaccination)
- **THEN** the anchor row shows the full charge with a claim-count badge, and its sub-rows show each pet's condition, claimable subtotal, and status

### Requirement: Charge with no claim is a first-class ledger row
A `vet_flag = 1` transaction that has no derived claim yet (no invoice matched) SHALL appear as a row in the same ledger, showing its charge and a no-invoice state, and SHALL expose the invoice-retrieval action rather than being relegated to a separate table.

#### Scenario: No-invoice charge listed inline
- **WHEN** a vet charge has been imported but no invoice has been matched to it
- **THEN** it appears in the ledger with a "no invoice — needs retrieval" state and its invoice-request action, in the same table as claimed visits

### Requirement: Expected reimbursement reflects excess, the insurer's benefit rate, and the annual cap
Each claimable row SHALL display an expected reimbursement derived from the claimable subtotal minus the applicable per-condition annual excess, then multiplied by the share of a claim the insurer actually pays (`config.PETCOVER_BENEFIT_RATE`, **65%**), bounded by the pet's remaining annual claim cap. That is the order Petcover's own letters use: excess and non-claimable lines first, then the rate, then the cap. The dashboard SHALL NOT display a fabricated or hard-coded deduction in place of the real excess/cap/rate math, and where the excess or cap inputs are not known for a pet the row SHALL flag the value as unavailable rather than guess it. A pet with no policy figures on record (live: Echo, insured with Bow Wow) therefore has no rate applied to it — its estimate stays unavailable.

Excess is drained greedily across a `(condition, policy year)` group in charge-date order, so the earliest charges absorb it. A claim covering more than one condition (`item_conditions` on file) is split into its real per-condition subtotals for this grouping — Petcover's excess applies per condition, so a joined claim drains two buckets rather than sharing one.

All figures are estimates. They do not net off what Petcover has actually paid this year, and a settled claim's actual paid amount overrides the estimate entirely rather than being re-derived.

**Revision (2026-08-04): the benefit rate was added, reversing a prior prohibition in this same requirement.** Until this date the requirement read "SHALL NOT display a fabricated or hard-coded deduction (**such as an invented age-contribution percentage**) in place of the real excess/cap math", and the prohibition was upheld as recently as `petcover-settlement-reconciliation`'s design.md Decision 3 on the reasoning that the dashboard projects onto invoices with no letter, so any rate there would be invented.

That reasoning had a false premise, corrected by Justin on 2026-08-04: **65% is the policy's own benefit rate**, which he had explained before ("Petcover only paying 65% of a claim"), not a percentage reverse-engineered from settlement letters. The constant `Age Contribution: … [35%]` printed on all ten live approval letters is the same term seen from the insurer's side — corroboration, not the source. So the original prohibition stands exactly as written for what it aimed at (inventing a deduction) and never covered applying a policy term known independently of the letters.

Consequence retired: the accepted divergence that every "Expected payment" would read ~35% above what Petcover pays. Consequence accepted instead: one global rate for the one Petcover-insured pet — a second insurer, or a rate that moves at an anniversary, needs a per-pet column, which on this live DB means a hand-run `ALTER TABLE`.

#### Scenario: The insurer's benefit rate is applied after the excess
- **WHEN** a claim's claimable subtotal is $200.00, its condition/year group has the full $150 excess remaining, and the pet has Petcover excess/cap on file
- **THEN** the expected reimbursement is $32.50 — $(200.00 − 150.00) × 0.65 — and the row's note names both the excess and the 65% benefit

#### Scenario: A claim wholly inside the excess
- **WHEN** the condition/year group's claimable total is below the excess
- **THEN** the expected reimbursement is $0.00 and the note still names the excess shortfall, unchanged by the rate

#### Scenario: A pet with no policy figures
- **WHEN** the pet has no `annual_excess` or `annual_cap` on record (live: Echo)
- **THEN** the estimate stays unavailable with the note `no policy excess/cap on file` and no benefit rate is applied

#### Scenario: A settled claim
- **WHEN** Petcover has actually paid
- **THEN** the recorded paid amount overrides the estimate and no rate is applied to it

#### Scenario: Excess applied once per condition per year
- **WHEN** the first claim for a given condition in a policy year has a claimable subtotal above the excess ($150 per condition per year)
- **THEN** the expected reimbursement equals the claimable subtotal minus the excess, and later claims for the same condition in the same year apply no further excess

#### Scenario: Annual cap bounds reimbursement
- **WHEN** a pet's prior reimbursements in the policy year approach the annual cap ($10,000/year)
- **THEN** the expected reimbursement for a further claim is bounded by the remaining cap

#### Scenario: Missing excess or cap is flagged, not guessed
- **WHEN** a pet's excess or annual cap is not recorded
- **THEN** the row flags expected reimbursement as unavailable rather than inventing a deduction

#### Scenario: No invoice matched yet
- **WHEN** a row has no claimable subtotal and no `invoice_data` at all
- **THEN** expected reimbursement is flagged unavailable with the note `no invoice yet`, and the row is excluded from the group excess/cap math

#### Scenario: An invoice is on file but its claimable subtotal was never recorded
- **WHEN** a row has `invoice_data` but no recorded `claimable_amount` (live: claims #2, #16, #18, #19, #21)
- **THEN** the note reads `invoice on file, but no claimable subtotal recorded` and the claimable column reads `Not recorded` — never the invoice total, and never `no invoice yet`, which would send Justin after a document he already holds

### Requirement: Ledger preserves the existing information density
The ledger SHALL retain, per visit, every field the two prior tables carried: date, merchant, pet(s), charge (ceiling), claimable subtotal, condition, claim status, Petcover reference, last status event, and the row's primary action.

#### Scenario: No column dropped in the merge
- **WHEN** the merged ledger renders a claimed visit
- **THEN** date, merchant, pet, charge, claimable, condition, status, reference, last event, and action are all present for that visit

### Requirement: Ledger status chips come from the shared vocabulary
The dashboard ledger and the `/basic` card view SHALL render a claim's state using the shared display vocabulary (`claim-status-vocabulary`), not a per-template label map. Neither template SHALL define its own status→wording table.

#### Scenario: A blocked claim reads as blocked
- **WHEN** the ledger renders a `matched` claim whose pet's insurer claim process is not defined
- **THEN** the chip states it is blocked on a missing claim process, and the same wording appears in `/basic`

#### Scenario: Chip wording changes in one place
- **WHEN** a label is renamed in the shared vocabulary
- **THEN** both the ledger chip and the `/basic` line change with no template edit

#### Scenario: Raw status still available on the claim detail
- **WHEN** Justin opens a claim's detail
- **THEN** the underlying stored status is still shown, so the derived label never hides what the pipeline recorded

## Known inconsistency — closed policy years (found 2026-07-25, undecided)

This ledger's excess math and `settlement-validation`'s expected-payout math disagree about **closed** policy years, and both are shipped:

- `claim_status._apply_excess_and_cap` (this ledger) drains the $150 excess for **every** `(condition, policy year)` group, including years that have already ended.
- `claim_status._validate_settlement` (settlement-validation, per ADR-0013's amendment) treats a claim whose transaction falls in an **already-closed** policy year as having already passed the threshold — expected = full claimable, no excess deducted — because our claim history for a closed year is presumed incomplete.

So for a last-policy-year claim the dashboard shows an expected reimbursement $150 lower than settlement validation expects for the same claim.

Which is right is **not recorded, and is not inferable from the code**. The closed-year default was Justin's explicit instruction for settlement validation; whether he intended it to govern the dashboard's estimates too was never asked. Flagged rather than resolved — silently changing either path would fabricate a decision. Needs Justin.
