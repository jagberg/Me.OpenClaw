## ADDED Requirements

### Requirement: An edited message is handled, not dropped
Telegram delivers an edit as an `edited_message` update, where `update.message` is `None`. The text handler SHALL read the effective message so an edit is processed as the message it is, and the message log SHALL record it with a truthful kind and summary rather than an empty `other` row.

Rationale: on 2026-07-27 Justin edited his message to add the pet's share. The handler raised `'NoneType' object has no attribute 'text'`, the update was logged as kind `other` with an empty summary, and the correction he had just made was invisible in the log as well as unprocessed.

#### Scenario: Authorized user edits a text message
- **WHEN** an `edited_message` arrives from the authorized user
- **THEN** it is handled on the same path as a new text message and the log row names it as an edit with its text

#### Scenario: Edit acknowledged
- **WHEN** an edited message is received
- **THEN** the 👍 acknowledgement path runs without raising, as it does for a new message

### Requirement: A reply to a card identifies that card's claim
The claim id SHALL be recoverable from a replied-to bot message — from its text (`Claim #N`) or its buttons' callback data — and passed to whichever handler owns the reply, so a plain-language reply to a card acts on that card's claim.

#### Scenario: Reply to an ASSIGN PET card
- **WHEN** the user replies with free text to the ASSIGN PET card for claim #N
- **THEN** the chat turn is given claim #N as its target

#### Scenario: Reply to a PDF alert
- **WHEN** the replied-to message is a document whose caption names claim #N
- **THEN** the claim id is taken from the caption, matching the existing caption-vs-text handling

## MODIFIED Requirements

### Requirement: Assign a pet by tap
An unattributed claim SHALL offer a one-tap button per known pet, alongside (not replacing) the dashboard picker and invoice-based auto-assignment.

One invoice can cover more than one pet, which the tap surface cannot express — a share per pet is needed. The card SHALL say that a shared invoice can be described in a reply, and such a reply SHALL lead to a split proposal rather than being treated as an unknown message.

#### Scenario: Tap to assign
- **WHEN** Justin taps a pet on an unassigned claim
- **THEN** the claim's pet is set, identically to the dashboard picker

#### Scenario: One invoice covers two pets
- **WHEN** neither single-pet button is correct because the invoice is shared
- **THEN** replying to the card with the pets and one share leads to a split proposal with a Confirm button, and nothing is assigned until it is tapped
