## ADDED Requirements

### Requirement: An information request's label names the document requested
"More vet info required" cannot be acted on; "consult notes needed" can. When the event records which document Petcover asked for, the label SHALL name it, and SHALL still say who owes it. When no document is recorded the label SHALL fall back to the who-owes-it wording rather than inventing a document.

Because a table chip and a card row cannot carry a full phrase with a date, the label SHALL use a short name for recognized document kinds (consultation notes, itemized invoice, completed claim form, referral history) and SHALL fall back to the generic wording for a kind it does not recognize. The full recorded phrase SHALL remain visible where there is room — the weekly nudge, the action card, and the claim detail.

#### Scenario: The vet owes consult notes
- **WHEN** an `info_requested` claim's event records the vet as owing consultation notes
- **THEN** its label names both — the vet, and consult notes

#### Scenario: Justin owes the document
- **WHEN** an `info_requested` claim's event records Justin as owing consultation notes
- **THEN** its label says the notes are needed from him

#### Scenario: Document recorded but not a recognized kind
- **WHEN** the recorded document matches no recognized kind
- **THEN** the label falls back to the who-owes-it wording, and the full phrase is still shown wherever the surface has room

#### Scenario: No document recorded
- **WHEN** no requested document is recorded on the event
- **THEN** the label is exactly the who-owes-it wording it is today

### Requirement: Where there is room, the request names the visit it refers to
A date on its own ("dated 18/05/2026") makes a clinic search; an invoice number does not. Where a surface has room — the weekly nudge, the action card, the claim detail — an information request SHALL name the visit its requested date resolves to: the invoice number, merchant and amount, and the claim it belongs to when it belongs to one. The wording SHALL make clear that the invoice identifies the visit and is not the document being asked for.

#### Scenario: The date resolves to a held invoice
- **WHEN** a vet-owed request for notes dated 18/05/2026 resolves to Kings Vet invoice 1000229 on claim #6
- **THEN** the nudge and the card name that invoice and claim, while the request itself stays on the claim it was sent about

#### Scenario: The visit is unknown
- **WHEN** the requested date resolves to no held invoice
- **THEN** the surfaces state the date and that the visit is unknown, rather than omitting the request or naming a nearby visit

#### Scenario: The invoice is not offered as the document
- **WHEN** a resolved invoice is shown alongside a request for consultation notes
- **THEN** the wording distinguishes the two, and no invoice is attached or presented as satisfying the request
