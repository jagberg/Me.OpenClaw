## ADDED Requirements

### Requirement: One invoice spanning multiple conditions fills multiple form rows
When per-item condition assignments are recorded for a claim, the fill SHALL emit one Petcover form row per distinct condition, with each row's charge the sum of that condition's item amounts (skipping items marked not-claimable), instead of a single condition/charge for the whole claim.

#### Scenario: Grouped rows from per-item assignments
- **WHEN** a claim has item assignments Arthritis→$390 and Raised ALT/ALP→$135
- **THEN** the claim form has two condition rows charged $390 and $135 respectively

### Requirement: Invoice-request email carries the visit details
The system SHALL draft (never send) an invoice-request email to the vet using Justin's template: the visit date (`dd-MMM-yyyy`), the pet's name and surname (falling back to a generic pet placeholder when unassigned), the charged amount, and a sign-off. This replaces the terse prior body.

#### Scenario: Invoice request for a known pet
- **WHEN** an invoice request is drafted for an Aari transaction on 2025-08-08 charged $44.75
- **THEN** the body reads "…invoice for visit on 08-Aug-2025 for our dog Aari Goldberg. The amount was for $44.75."

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
