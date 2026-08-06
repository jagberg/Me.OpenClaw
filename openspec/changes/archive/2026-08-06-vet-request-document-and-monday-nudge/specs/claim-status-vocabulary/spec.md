## MODIFIED Requirements

### Requirement: An information request is worded by who owes the document
`info_requested` covers two different situations that must not read alike: Petcover has asked the **vet** for a document (Justin is only Cc'd, and his job is to chase), or Petcover has asked **Justin**. The vocabulary SHALL word them differently from the `owed_by` recorded on the event, and SHALL fall back to a neutral wording when the owner is unrecorded rather than asserting either.

When the event also records **which document** was asked for, the label SHALL name it — "More vet info required" cannot be acted on, "consult notes needed" can — while still saying who owes it. When no document is recorded, or its kind is not recognized, the label SHALL fall back to the who-owes-it wording rather than inventing a document.

| `owed_by` | document known | label |
|---|---|---|
| vet | yes | **Vet: consult notes needed** (the document's short name) |
| vet | no / unrecognized kind | **More vet info required** |
| Justin | yes | **Consult notes needed from you** |
| Justin | no / unrecognized kind | **Petcover needs info from you** |
| unrecorded | either | **Info requested** |

Because a table chip and a card row cannot carry a full phrase with a date, the label SHALL use a short name for recognized document kinds (consultation notes, itemized invoice, completed claim form, referral history). The full recorded phrase SHALL remain visible where there is room.

The word "suspended" SHALL NOT appear in any of these labels; a claim is only labelled suspended when Petcover has actually suspended it.

#### Scenario: The vet owes consult notes
- **WHEN** an `info_requested` claim's event records the vet as owing consultation notes
- **THEN** its label names both — the vet, and consult notes

#### Scenario: Justin owes the document
- **WHEN** an `info_requested` claim's event records Justin as owing consultation notes
- **THEN** its label says the notes are needed from him

#### Scenario: The vet owes an unstated document
- **WHEN** an `info_requested` claim's event records the vet as owing it but no document
- **THEN** its label reads "More vet info required"

#### Scenario: Document recorded but not a recognized kind
- **WHEN** the recorded document matches no recognized kind
- **THEN** the label falls back to the who-owes-it wording, and the full phrase is still shown wherever the surface has room

#### Scenario: Justin owes an unstated document
- **WHEN** an `info_requested` claim's event records Justin as owing it but no document
- **THEN** its label reads "Petcover needs info from you"

#### Scenario: Owner unrecorded
- **WHEN** an `info_requested` claim's event records no owner
- **THEN** its label reads "Info requested" and makes no claim about who must act, whatever document is recorded

#### Scenario: A real suspension is still labelled suspended
- **WHEN** a claim's status is `suspended`
- **THEN** its label says so, and no information request borrows that word

## ADDED Requirements

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
