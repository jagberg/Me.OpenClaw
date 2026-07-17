## ADDED Requirements

### Requirement: Telegram entry point for condition text and pet assignment
In addition to the dashboard form, the system SHALL accept condition text and pet-assignment input via Telegram command, applying the identical update as the dashboard route so a claim can be unblocked from either surface.

#### Scenario: Condition supplied via Telegram unblocks a matched claim
- **WHEN** Justin sends `/mark <claim_id> <condition text>` for a claim at `matched` missing only condition text
- **THEN** `condition_text` is set exactly as the dashboard form would set it, and the claim becomes eligible to advance to `drafted` on the next fill attempt

#### Scenario: Pet assignment supplied via Telegram
- **WHEN** Justin sends the pet-assignment command for a vet-flagged transaction with no pet assigned
- **THEN** the transaction's pet is set exactly as the dashboard pet picker would set it

### Requirement: On-demand pipeline advance for a single claim
The system SHALL allow triggering the matched→drafted advance for one specific claim on demand (via Telegram `/process <claim_id>`), independent of the scheduled pipeline interval, reusing the same fill/draft logic and validation as the scheduled run.

#### Scenario: Process command on an already-complete claim
- **WHEN** Justin sends `/process <claim_id>` for a claim at `matched` with all required fields present
- **THEN** the claim is filled and drafted immediately, without waiting for the next scheduled tick

#### Scenario: Process command on a claim still missing a required field
- **WHEN** Justin sends `/process <claim_id>` for a claim still missing condition text or pet assignment
- **THEN** the claim stays at `matched` and the response names the missing field, consistent with the existing flag behavior
