# claim-form-automation — delta

## ADDED Requirements

### Requirement: Gate the matched→drafted step on the condition's accrual threshold
The matched→drafted step SHALL NOT draft a claim whose condition has not yet accrued claimable exceeding the fixed excess for the policy year (see `excess-threshold-accrual`). A held claim SHALL stay `matched` with a human-readable holding flag rather than producing a dead-end submission. When the threshold is crossed, the step SHALL draft the condition's held and previously below-excess claims together as ≤4-per-form batches.

#### Scenario: Matched claim below the condition threshold
- **WHEN** the draft step runs and a matched claim's condition has accrued claimable not exceeding $150 this policy year
- **THEN** the claim is not drafted and carries a holding flag naming the accrued amount against the excess

#### Scenario: Threshold crossed
- **WHEN** the draft step runs and the condition's accrued claimable now exceeds $150 (or its excess was already consumed this policy year)
- **THEN** the condition's held and below-excess claims are drafted as batched submissions for Justin to send
