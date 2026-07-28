## MODIFIED Requirements

### Requirement: Claim history and outstanding actions on demand
The system SHALL provide a paged view of the past year's claims and a view of everything currently waiting on Justin, both rendered as card images, with one tap-to-resolve card per outstanding actionable item.

Added after the original change. The actions view derives from the shared `claim_status.pending_actions()` so it cannot disagree with the chat agent's answer, separates blocked items (no tap can clear them), and states what it held back rather than truncating silently.

An outstanding information request SHALL be rendered as its own card kind carrying the clinic name and email address of whoever owes the document and the days remaining until one year from the treatment date, and SHALL appear before every other kind. Requests already past that deadline SHALL NOT appear as action cards at all — they belong to the register's manual-handling list, and a tap cannot recover them.

#### Scenario: Actions requested
- **WHEN** the actions view is requested
- **THEN** a summary card is sent plus one tap-to-resolve card per actionable item, with blocked items reported separately

#### Scenario: Information request card
- **WHEN** the actions view includes an outstanding information request owed by a vet
- **THEN** its card is sent first, naming the clinic, its email address, and the days remaining before the one-year deadline

#### Scenario: Expired request is not offered as an action
- **WHEN** an information request's treatment date is more than a year old and nothing resolved it
- **THEN** no action card is produced for it

#### Scenario: More actions than the card cap
- **WHEN** actionable items exceed the display cap
- **THEN** the count held back is stated explicitly
