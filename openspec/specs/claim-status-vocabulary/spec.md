# claim-status-vocabulary Specification

## Purpose
Own the words a claim's state is shown in. `vet_claims.status` is pipeline state; the label Justin reads is a separate mapping, defined once and read by every surface — the dashboard ledger, `/basic`, Telegram claim cards, and notification text. A label names who holds the claim and, when that is Justin, what he must supply. See ADR-0021.
## Requirements
### Requirement: One display vocabulary for claim state, defined once
A claim's stored `status` is pipeline state; the words shown to Justin are a separate, single mapping. The system SHALL define that mapping in exactly one module, and every surface that shows a claim's state — the dashboard ledger, `/basic`, Telegram claim cards, Telegram notification text — SHALL read it rather than carrying its own copy. Renaming a label SHALL require editing one map. A new state's wording SHALL be added in one place, including states introduced by other in-flight work.

#### Scenario: Every surface agrees
- **WHEN** a status's label is changed in the shared map
- **THEN** the dashboard chip, the `/basic` line, the rendered claim card and the notification text all show the new wording, with no second map to update

#### Scenario: Stored status is untouched by the vocabulary
- **WHEN** a label is added, renamed or derived
- **THEN** `vet_claims.status` values, `claim_status_events.event_type` values and every query keyed on them are unchanged

#### Scenario: Presentation keys off status, not wording
- **WHEN** a status's label is reworded
- **THEN** its chip colour and severity class are unaffected, because they are keyed by status rather than by label text

### Requirement: A label states who holds the claim, not what the software did
A label SHALL name the party the claim is waiting on and, when that party is Justin, what he must supply. A label SHALL NOT imply Justin must act when nobody is waiting on him, and SHALL NOT imply a claim is inert when it is permanently blocked.

#### Scenario: Waiting on Petcover
- **WHEN** a claim is `sent`, `acknowledged` or `approved`
- **THEN** its label conveys that Petcover holds it and no action is asked of Justin

#### Scenario: Waiting on Justin
- **WHEN** a claim needs a condition, a pet, or a draft sent
- **THEN** its label names the missing thing

### Requirement: A `matched` claim's label states what it is waiting for
`matched` means an invoice is attached and the claim has not yet been drafted, which spans "needs one field from Justin" and "can never proceed". The system SHALL derive the label for a `matched` claim from the same outstanding-action determination the action list uses, and SHALL NOT introduce a second stored field or a second copy of that logic.

#### Scenario: Blocked on an undefined insurer process
- **WHEN** a `matched` claim is flagged that its pet's insurer claim process is not yet defined
- **THEN** its label states it is blocked on a missing claim process, not the bare word "Matched"

#### Scenario: Waiting on a condition
- **WHEN** a `matched` claim has no `condition_text`
- **THEN** its label states a condition is needed

#### Scenario: Waiting on a pet assignment
- **WHEN** a `matched` claim has no pet assigned
- **THEN** its label states a pet is needed

#### Scenario: Nothing outstanding
- **WHEN** a `matched` claim has no outstanding action
- **THEN** its label remains "Matched"

#### Scenario: Derivation is not duplicated
- **WHEN** the outstanding-action determination changes
- **THEN** both the action list and the derived label change with it, because both call the same function

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

