## ADDED Requirements

### Requirement: The claim a message replies to is part of the turn's context
Telegram cards carry their claim id in both the card text (`Claim #N`) and their buttons' callback data (`setpet:N:…`). When Justin replies to a bot message, that claim id SHALL be resolved from the replied-to message and supplied to the agent as the turn's current claim, so "this claim" / "this invoice" resolves without him repeating an id or a reference.

Rationale: on 2026-07-27 he replied three times to the ASSIGN PET card for claim #1 saying the invoice was split with Echo. The agent had no reply context and no tool taking a claim id, asked for "the reference", and then emitted the tool schema's own description strings as arguments (`propose_assign_pet,{"merchant":"vet/merchant name to locate the unassigned claim",…}`), failing three times with `tool_use_failed`.

#### Scenario: Reply to a claim card
- **WHEN** the authorized user replies to a bot message that names claim #N
- **THEN** the turn is given claim #N as its current claim, and an action requested with no other target applies to #N

#### Scenario: Reply to a message with no claim
- **WHEN** the replied-to message names no claim
- **THEN** no current claim is supplied and the existing ambiguous-target behaviour applies

#### Scenario: An explicit target overrides the reply
- **WHEN** the message itself names a different claim than the card replied to
- **THEN** the claim named in the message wins

### Requirement: A per-pet invoice split can be proposed from chat
The agent SHALL be able to propose splitting a claim's claimable amount between pets, with the shares Justin stated, as a confirmable proposal. It MUST NOT invent a share, MUST NOT choose which pet gets the larger part, and MUST NOT report the split as done before the tap.

#### Scenario: Split described in one message
- **WHEN** Justin says an invoice covers two pets and gives one pet's amount
- **THEN** the agent proposes the split for those two pets with that share and the derived remainder, names the claim id, and asks for the Confirm tap

#### Scenario: Amounts missing
- **WHEN** Justin says an invoice is shared but gives no amount
- **THEN** the agent asks what each pet's share is and proposes nothing

#### Scenario: Split request is not turned into a task
- **WHEN** a split is requested for a claim that exists and is splittable
- **THEN** the agent proposes the split rather than saving a task describing the request

## MODIFIED Requirements

### Requirement: Act on claims with confirm-before-commit
The agent SHALL be able to perform the same mutations the slash commands expose (mark sent, set condition, assign pet, mark resolved, split between pets), but every mutation MUST be presented as a confirmation with an inline confirm button and MUST NOT commit until the user taps confirm. The agent MUST NOT describe a proposed action as done.

Every act tool SHALL accept an explicit claim id, and that id SHALL be how a target is named whenever one is known — the pet/reference/merchant filters exist for when it is not. A tool MUST NOT be left with no way to name the claim under discussion: with none available the model fabricated argument values from the schema's own description text (live, 2026-07-27).

#### Scenario: Requested mutation is confirmed
- **WHEN** the user asks the agent to perform a mutation (e.g. "mark Bella's claim sent")
- **THEN** the agent identifies the target claim, replies with a confirmation naming the claim and action plus a confirm button, and applies the mutation only after the button is tapped — reusing the existing `claim_forms` / `claim_status` functions

#### Scenario: Mutation not confirmed
- **WHEN** the confirmation is shown but not tapped
- **THEN** no data changes

#### Scenario: Hard rules are honored
- **WHEN** a requested action would send an email or supply a required claim field (e.g. `condition_text`) that the user did not provide
- **THEN** the agent refuses to send email (drafts only) and refuses to invent the field, flagging it for the user instead of guessing

#### Scenario: Ambiguous target
- **WHEN** the requested action matches zero or multiple claims
- **THEN** the agent asks for clarification and commits nothing

#### Scenario: Target named by id
- **WHEN** the claim id is known — stated in the message or carried by the message it replies to
- **THEN** the act tool is called with that id and no pet/reference guesswork is involved
