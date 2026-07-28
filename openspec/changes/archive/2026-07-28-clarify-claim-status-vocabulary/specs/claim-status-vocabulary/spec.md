## ADDED Requirements

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

### Requirement: An information request is worded by who owes the document
`info_requested` covers two different situations that must not read alike: Petcover has asked the **vet** for a document (Justin is only Cc'd, and his job is to chase), or Petcover has asked **Justin**. `vet-info-request-chase` already records which on the event. The vocabulary SHALL word them differently, and SHALL fall back to a neutral wording when the owner is unrecorded rather than asserting either.

- vet owes it → **More vet info required**
- Justin owes it → **Petcover needs info from you**
- unrecorded → **Info requested**

The word "suspended" SHALL NOT appear in any of these labels; a claim is only labelled suspended when Petcover has actually suspended it.

#### Scenario: The vet owes the document
- **WHEN** an `info_requested` claim's event records that the vet owes the requested document
- **THEN** its label reads "More vet info required"

#### Scenario: Justin owes the document
- **WHEN** an `info_requested` claim's event records that Justin owes it
- **THEN** its label reads "Petcover needs info from you"

#### Scenario: Owner unrecorded
- **WHEN** an `info_requested` claim's event records no owner
- **THEN** its label reads "Info requested" and makes no claim about who must act

#### Scenario: A real suspension is still labelled suspended
- **WHEN** a claim's status is `suspended`
- **THEN** its label says so, and no information request borrows that word

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
