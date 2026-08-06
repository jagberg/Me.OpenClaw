## ADDED Requirements

### Requirement: A weekly Monday nudge lists unanswered vet-directed information requests
The daily stale-action nudge is a summary keyed on charge age; an information request needs its own beat, because a vet practice is chased on a weekday and the claim's real clock is the treatment-anchored one-year deadline, not the charge date. The system SHALL send one message every Monday morning listing every claim whose outstanding information request is owed by the vet and unresolved.

Each line SHALL carry the claim id, the pet, the clinic's name and email, the document requested, how long the request has been outstanding, and the days remaining to the deadline. When nothing is outstanding the system SHALL send nothing — a weekly "nothing to do" trains the channel to be ignored. This nudge SHALL NOT replace the daily stale-action nudge.

#### Scenario: Two clinics owe documents on Monday morning
- **WHEN** the weekly job runs and two claims have unresolved vet-owed information requests
- **THEN** one message lists both, each with its clinic name and email, requested document, age, and days remaining

#### Scenario: Nothing outstanding
- **WHEN** the weekly job runs and no vet-owed information request is unresolved
- **THEN** no message is sent

#### Scenario: A request past the deadline
- **WHEN** an unresolved vet-owed request's claim is past the one-year treatment deadline
- **THEN** it does not appear in the weekly message

#### Scenario: The day is configurable and a missed firing is not skipped
- **WHEN** the machine is asleep at the scheduled time
- **THEN** the missed run is coalesced and still fires rather than being dropped for the week

#### Scenario: The document is unknown
- **WHEN** an unresolved vet-owed request has no recorded document
- **THEN** the line still names the claim, pet, clinic and dates, and says the document is unstated rather than omitting the claim
