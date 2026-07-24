# excess-threshold-accrual — delta

## ADDED Requirements

### Requirement: Do not submit a condition until its claimable exceeds the annual excess
The system SHALL NOT draft or submit a claim for a condition until the total claimable subtotal accrued for that `(pet, condition, policy year)` **exceeds** the fixed excess ($150). Accrual sums the claimable of every non-settled, non-terminal claim for that key (`matched` held, `below_excess`, and in-flight submitted). If the condition's thread already has a settled claim within the current policy year (the excess is consumed), the gate is disabled and claims submit normally. The policy year runs anniversary-to-anniversary; when the pet's anniversary is unknown the gate degrades to lifetime accrual.

#### Scenario: Single small invoice under the excess
- **WHEN** a matched arthritis claim has claimable $44.75 and no other arthritis claimable is accrued this policy year
- **THEN** the claim stays `matched`, is not drafted, and carries a holding flag naming the accrued amount against the $150 excess

#### Scenario: Accrued claimable exceeds the excess
- **WHEN** further matched arthritis invoices bring the condition's accrued claimable to more than $150 in the policy year
- **THEN** the gate opens and the condition's held claims become eligible to draft

#### Scenario: Excess already consumed this policy year
- **WHEN** the condition's thread already has a claim settled in the current policy year
- **THEN** the gate is disabled and a new matched claim for that condition drafts without waiting to re-accrue $150

#### Scenario: Accrued exactly at the excess
- **WHEN** a condition's accrued claimable equals $150 exactly
- **THEN** it stays held (submitting would net $0), until accrual strictly exceeds $150

### Requirement: On release, held and below-excess claims of a condition draft together
When the gate opens for a condition, the system SHALL draft that condition's held `matched` claims together with its previously `below_excess`-declined claims for the same policy year, batched at most 4 per Petcover form (sharing one draft), for Justin to send himself. Re-drafting a `below_excess` claim SHALL reuse its already-attached invoice and move it back into the `drafted` lifecycle.

#### Scenario: Below-excess claim rolled into a release batch
- **WHEN** a condition accrues past the excess and it has one `below_excess` claim plus two held `matched` claims (all same pet)
- **THEN** all three are drafted as one batch submission (one form, one draft), reusing each claim's existing invoice, and no claim is auto-sent

#### Scenario: More than four claims released
- **WHEN** a released condition has more than four claims
- **THEN** they are split into batches of at most four in a deterministic order (transaction date, id)

### Requirement: Alert before below-excess claimable expires at the policy anniversary
Within a configured window before a pet's policy anniversary, the system SHALL send one Telegram alert per `(pet, condition, policy year)` that is still holding accrued claimable greater than $0 but not exceeding the excess — invoices that will become un-claimable at renewal. The alert SHALL be deduped so it is sent at most once per condition per policy year and SHALL reset for the new policy year.

#### Scenario: Condition holding below-excess invoices near renewal
- **WHEN** the policy anniversary is within the alert window and a condition holds $105 of accrued claimable (under the $150 excess)
- **THEN** one Telegram alert names the condition, the accrued amount, and the renewal date, warning the invoices will expire un-claimable

#### Scenario: Already alerted this policy year
- **WHEN** an expiry alert for a condition was already sent in the current policy year
- **THEN** no further alert is sent for that condition until the next policy year
