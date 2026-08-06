## ADDED Requirements

### Requirement: An information request records the document it asked for
Petcover's letter names the document in a fixed template phrase (confirmed live: *"To assess your claim, we need a copy of / Consultation notes dated 18/05/2026"*). The system SHALL extract that phrase and record it on the `info_requested` event alongside who owes it, using pattern matching only — no LLM. When no recognized phrase is present the system SHALL record no document rather than inferring one.

#### Scenario: The letter names the document
- **WHEN** an information-request letter states `we need a copy of Consultation notes dated 18/05/2026`
- **THEN** the event records the requested document as that phrase

#### Scenario: No recognized phrase
- **WHEN** an information-request letter carries no recognized "we need a copy of" / "please provide the following" phrasing
- **THEN** no requested document is recorded, and the claim's handling is otherwise unchanged

#### Scenario: The trailing template boilerplate is not part of the document
- **WHEN** the requested-document phrase is followed by the letter's standard boilerplate (`Please note we cannot process the claim…`, `You can reach us on…`)
- **THEN** the recorded document stops at the requested item and excludes the boilerplate

### Requirement: Unanswered vet-directed requests are identifiable
A vet's reply to Petcover never reaches Justin's mailbox, so "unanswered" SHALL mean the claim still carries an unresolved information request owed by the vet — the same unresolved determination the dashboard's needs-action list uses. The system SHALL be able to list those claims with the clinic, the requested document, the age of the request, and the days remaining against the treatment-anchored one-year submission deadline. A request whose deadline has passed SHALL be excluded from that list.

The deadline SHALL be anchored on the date the pet was **treated**, not on the bank charge: Petcover's own wording is *"within one year of your pet receiving treatment"*, and the two dates differ by an unbounded amount (confirmed live: treated 19 Jun and 30 Jun 2026, both charged 06/07/2026 — over-granting 17 and 6 days). The treatment date SHALL be the **earliest** date the attached invoice states, its own or any line item's, because an invoice billing several visits expires on its oldest one. With no invoice attached the system SHALL fall back to the transaction date and SHALL disclose that the anchor was assumed rather than presenting it as known.

#### Scenario: A vet-owed request is outstanding
- **WHEN** a claim's latest unresolved information request is owed by the vet
- **THEN** it appears in the unanswered list with the clinic name and email, the requested document, days outstanding, and days remaining to the deadline

#### Scenario: Justin confirms it resolved
- **WHEN** Justin confirms the information request resolved
- **THEN** the claim leaves the unanswered list

#### Scenario: Past the treatment deadline
- **WHEN** an unanswered request's claim is past the one-year treatment deadline
- **THEN** it is excluded from the unanswered list rather than nudged indefinitely

#### Scenario: The request was addressed to Justin
- **WHEN** a claim's outstanding information request is owed by Justin, not the vet
- **THEN** it does not appear in the vet-unanswered list
