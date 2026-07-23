# settlement-validation — delta

## ADDED Requirements

### Requirement: Settlements are validated against expected payout
On a settlement event, the system SHALL compute expected payout = the claim's claimable subtotal minus the $-excess (only when the thread has no prior settled claim within the current policy year), bounded by the pet's remaining annual cap, and SHALL flag the claim and notify Telegram (with the settlement PDF) when paid falls short of expected by more than a $2 tolerance. The system SHALL NOT auto-dispute — the flag is a prompt for Justin.

#### Scenario: Second settlement of a thread in the same policy year deducts excess again
- **WHEN** a thread settled in February (excess deducted) and a July settlement in the same policy year pays claimable − $150
- **THEN** the claim is flagged `settlement short — expected $X, paid $Y` naming the earlier excess deduction, and Telegram receives the flag with the settlement PDF attached

#### Scenario: Settlement pays expected amount
- **WHEN** paid is within $2 of expected
- **THEN** no flag is raised and the normal settled notification is sent

### Requirement: Excess and cap follow the policy year
Excess consumption ($150 per thread) and the $10,000 annual cap SHALL reset at the pet's policy anniversary (stored per pet), not the calendar year.

#### Scenario: Thread settles either side of the anniversary
- **WHEN** a thread settled in the previous policy year and settles again after the anniversary
- **THEN** expected payout deducts the excess again (new policy year)

### Requirement: Validation degrades gracefully when the anniversary is unknown
When the pet has no stored policy anniversary, the system SHALL still validate using thread-lifetime excess only (deduct excess only if the thread has never settled) and SHALL word any shortfall flag to say the anniversary is unknown.

#### Scenario: Anniversary not yet stored
- **WHEN** a settlement arrives for a pet without a policy anniversary on record
- **THEN** validation runs with the degraded rule and the flag text (if raised) names the missing anniversary
