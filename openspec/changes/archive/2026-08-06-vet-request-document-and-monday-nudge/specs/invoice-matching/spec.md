## ADDED Requirements

### Requirement: An invoice's line items may carry their own date
An invoice's header date is not always the date of the treatment on it — a vet's statement can bill several visits, and a consultation on the 18th can appear on an invoice dated the 30th. Extraction SHALL capture an optional date per line item, null when the document does not state one, so a date named elsewhere (an insurer's request for "consultation notes dated 18/05/2026") can be matched against the treatment rather than only against the invoice header.

Because successful extractions are cached indefinitely, changing what extraction must return SHALL be accompanied by invalidating the cached rows, with the number of re-extractions stated up front — the re-read spends real tokens against the daily budget.

#### Scenario: Multi-visit invoice
- **WHEN** an invoice dated the 30th lists a consultation dated the 18th
- **THEN** that line item carries its own date, distinct from the invoice's date

#### Scenario: No item dates printed
- **WHEN** an invoice's line items state no dates
- **THEN** their dates are null and matching falls back to the invoice's own date

#### Scenario: Cache invalidated deliberately
- **WHEN** the extraction schema changes to include item dates
- **THEN** the cached extractions are cleared as one deliberate step with the count of re-extractions stated, not left to expire silently

### Requirement: A date named by the insurer resolves to a visit we already hold
When Petcover names a date in a request (the treatment whose notes it wants), the system SHALL find the visit that date belongs to and report it — claim id where one exists, plus merchant, invoice number and amount — searching claims' stored invoices first and the extraction cache second. It SHALL NOT fall back to the nearest date: an adjacent visit is a different consultation, and chasing the wrong one wastes the request.

The resolved invoice is context for the chase, never a substitute for the document requested. Consultation notes are clinical records held by the practice; nothing resolved here is attached or offered in their place.

#### Scenario: The date belongs to a claim we hold
- **WHEN** a request names a date matching a claim's invoice (real: 18/05/2026 → Kings Vet invoice 1000229, $351.50, claim #6)
- **THEN** the request reports that claim, merchant, invoice number and amount, even though the request itself sits on a different claim

#### Scenario: The date belongs to a visit with no claim
- **WHEN** the date matches only an invoice in the extraction cache (a visit predating the bank-transaction coverage)
- **THEN** the invoice's own details are reported with no claim id

#### Scenario: No match
- **WHEN** no held invoice carries the requested date
- **THEN** the request states the date and that the visit is unknown, and no nearest-date guess is made

#### Scenario: Two visits share the date
- **WHEN** two invoices carry the requested date (e.g. one charge covering two pets)
- **THEN** both are reported rather than one being chosen
