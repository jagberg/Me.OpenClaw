## MODIFIED Requirements

### Requirement: The expected-reimbursement estimate nets Petcover's benefit rate

The visit ledger's expected-reimbursement estimate SHALL apply the share of a claim Petcover actually
pays, after the excess and before the annual cap — the order Petcover's own letters use. For the one
Petcover-insured pet the rate is **65%** (`config.PETCOVER_BENEFIT_RATE`), and the note on each
estimate SHALL say so (`est. after $150 excess, 65% benefit`) rather than presenting a net figure
without its basis.

A pet with no policy excess/cap on record is unaffected and its estimate stays unavailable — Echo is
insured with Bow Wow and has no Petcover figures, so no rate is applied to it.

**This reverses a recorded decision, deliberately and with Justin's authority.** This spec previously
required that the dashboard "SHALL NOT display a fabricated or hard-coded deduction (such as an
invented age-contribution percentage) in place of the real excess/cap math", and
`petcover-settlement-reconciliation`'s design.md Decision 3 upheld it — on the reasoning that the
dashboard projects onto invoices with no letter, so any rate there would be invented. That reasoning
had a false premise, corrected by Justin on 2026-08-04: **65% is the policy's own benefit rate**, which
he had explained before ("Petcover only paying 65% of a claim"), not a percentage reverse-engineered
from settlement letters. The letters' constant `Age Contribution: … [35%]` is the same term seen from
the other side, which is corroboration rather than the source.

So the original requirement stands as written for what it was aimed at — inventing a deduction — and
does not cover applying a policy term that is known independently of the letters. The consequence the
prior decision accepted, that every card's "Expected payment" would read ~35% above what arrives, is
removed rather than documented.

The closed-policy-year disagreement recorded in `settlement-validation` since 2026-07-25 is **not**
resolved by this and stays open in `openspec/BACKLOG.md`.

#### Scenario: An estimate for a Petcover-insured pet
- **WHEN** a claim's claimable subtotal is $200.00, its condition/year group has the full $150 excess remaining, and the pet has Petcover excess/cap on file
- **THEN** the estimate is $32.50 — $(200.00 − 150.00) × 0.65 — and its note names both the excess and the 65% benefit

#### Scenario: A claim wholly inside the excess
- **WHEN** the condition/year group's claimable total is below the excess
- **THEN** the estimate is $0.00 and the note still names the excess shortfall, unchanged by the rate

#### Scenario: A settled claim
- **WHEN** Petcover has actually paid
- **THEN** the recorded paid amount overrides the estimate entirely and no rate is applied to it

#### Scenario: A pet with no Petcover policy figures
- **WHEN** the pet has no `annual_excess` or `annual_cap` on record (live: Echo)
- **THEN** the estimate stays unavailable with the note `no policy excess/cap on file`, and no benefit rate is applied
