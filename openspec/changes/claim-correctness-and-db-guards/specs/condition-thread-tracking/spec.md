## ADDED Requirements

### Requirement: A serial → claim mapping is correctable, and only on stated evidence

A Petcover serial (`Sr` / `Treatment number`) assigned to the wrong claim SHALL be correctable after the fact. A correction SHALL be admissible only on evidence that states the serial's treatment date or its assessed amount — Petcover's own status table, or a letter whose stated amount matches exactly one candidate claim's invoice. Ordering SHALL NOT be evidence: the superseded rule assigned "the oldest-transaction claim not yet serialized" and was wrong on all ten serials held.

A correction is a money-affecting write. It SHALL run inside the app container, never from the host, and SHALL NOT be applied without explicit human go-ahead.

#### Scenario: Evidence states a treatment date for the serial

- **WHEN** a correction names a serial and the evidence states that serial's treatment date
- **THEN** the serial SHALL be linked to the claim whose treatment date matches
- **AND** the prior link SHALL be recorded as superseded rather than silently overwritten

#### Scenario: Amount identifies exactly one candidate

- **WHEN** no treatment date is available, and the letter's stated amount matches exactly one claim awaiting a serial
- **THEN** that claim SHALL receive the serial

#### Scenario: The amount is ambiguous

- **WHEN** two or more claims awaiting a serial share the stated amount
- **THEN** no serial SHALL be assigned
- **AND** the event SHALL be left unlinked for a manual link, surfaced as an `unlinked_letter`

#### Scenario: A correction is attempted from the host

- **WHEN** a correction is attempted against the live DB from the host
- **THEN** it SHALL be refused, because a host-side read-write open deletes the WAL sidecars and takes the app down

### Requirement: No scheduled feed supplies the serial → treatment-date table

Petcover's treatment-date-per-serial table is a one-off, requested by hand. The system SHALL NOT present it as a refreshable source, and where a future serial cannot be placed from a letter alone, the resolution SHALL be recorded as "ask Petcover again" rather than deferred to a re-derivation.

#### Scenario: A new serial arrives that the letter cannot place

- **WHEN** a serial cannot be placed by treatment date or by a unique amount match
- **THEN** the system SHALL surface it as needing a fresh ask, not as a pending automatic resolution
