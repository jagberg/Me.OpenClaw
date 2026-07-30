## ADDED Requirements

### Requirement: The pipeline's own state transitions are recorded
Every state the system itself puts a claim into — invoice matched, form drafted, submission sent, invoice rejected back to unmatched, claim absorbed into a sibling — SHALL be recorded as an event, not only written to the claim row. Until now only insurer-caused states were recorded, so a claim's arrival at `matched`, `drafted` or `sent` left no trace and its previous state could only be inferred.

#### Scenario: An invoice is matched
- **WHEN** the matcher attaches an invoice to a claim
- **THEN** a `matched` event is recorded alongside the invoice fields, and the state change goes through the single write path

#### Scenario: A wrong invoice is rejected
- **WHEN** Justin rejects a matched invoice
- **THEN** an `unmatched` event is recorded and the claim returns to `pending_match` through the same path

#### Scenario: A submission is sent
- **WHEN** claims sharing a draft are marked sent
- **THEN** each records a `sent` event, so the submission is auditable per claim

### Requirement: A shadow comparison runs before the projection is trusted
Before the replayed state becomes authoritative, the system SHALL compute it on the existing cycle for every claim and compare it against the stored column, reporting disagreements without changing any data. The comparison SHALL be reportable as a count on the health endpoint. Only once it reports no disagreements across **at least seven days of cycles** SHALL the replayed state be given authority.

A zero count SHALL NOT by itself be read as evidence that the transition table is correct. The comparison's two sides are not independent: a claim backfilled from its stored status carries that status in its seed event, and a refused transition leaves both the column and the replay unchanged, so detector and enforcer agree by construction. The evidence the waiting period is for is therefore **claims that actually transition during it** — a period in which no claim changes state proves nothing, however long it runs.

The severity of a disagreement SHALL signal a developer defect rather than an action for Justin, per the existing alerting levels.

#### Scenario: Shadow mode changes nothing
- **WHEN** the comparison runs and finds a disagreement
- **THEN** it is logged with the claim ids and counted on the health endpoint, and no claim's state or flag is modified

#### Scenario: Clean before authority
- **WHEN** the replayed state is given authority over the column
- **THEN** the comparison had already reported zero disagreements across at least seven days of cycles, that period included claims which actually changed state, and the backfill was already applied

#### Scenario: A direct status write is detectable
- **WHEN** any code writes a claim's status outside the single write path
- **THEN** the next comparison reports that claim as a disagreement
