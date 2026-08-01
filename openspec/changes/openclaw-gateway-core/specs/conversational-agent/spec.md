## MODIFIED Requirements

### Requirement: Authorized free-form chat
Any non-command text message from the authorized user SHALL become a chat turn handled by the gateway agent. Messages from anyone other than the authorized user MUST be ignored, matching existing command authorization.

Pending app-side free-text flows SHALL take precedence over the agent turn. Because the gateway routes non-command text to its agent by default, the app SHALL be consulted for a pending flow before the turn begins, and a message consumed by such a flow SHALL NOT also reach the agent.

#### Scenario: Authorized user sends free text
- **WHEN** the authorized user sends a plain-text message that is not a slash command and is not a pending free-text reply (e.g. condition entry)
- **THEN** it is routed to the conversational agent and the reply lands in the same chat

#### Scenario: Unauthorized user sends free text
- **WHEN** a user other than the authorized username sends any message
- **THEN** it is ignored and the rejection logged, exactly as commands are

#### Scenario: Existing typed-reply flows still win
- **WHEN** the user is mid free-text entry for an existing flow (e.g. after tapping "Other (type it)" for a condition)
- **THEN** that pending flow consumes the message and the agent does NOT receive it as a turn

#### Scenario: Pending-flow check is unavailable
- **WHEN** the app cannot be reached to check for a pending flow
- **THEN** the message is not speculatively handed to the agent, and the user is told the service is unavailable

### Requirement: Act on claims with confirm-before-commit
The agent SHALL be able to perform the same mutations the slash commands expose (mark sent, set condition, assign pet, mark resolved, split between pets), but every mutation MUST be presented as a confirmation with an inline confirm button and MUST NOT commit until the user taps confirm. The agent MUST NOT describe a proposed action as done.

The proposal gate SHALL be a property of code rather than of the agent's prompt or configuration: a `propose_*` tool records a pending action and returns a confirmation, and the commit happens only on the confirm path inside the app. This preserves the existing guarantee that the gate is a harness property, not a behaviour the model is trusted to observe.

**Which component owns the gate is unresolved** — the confirm tap is now a `command` button handled by the plugin and `/internal`, so the commit executes in Python and not in the MCP server as originally written. Same open question as `claims-mcp-surface` and design D3; the invariant holds under either placement.

Every act tool SHALL accept an explicit claim id, and that id SHALL be how a target is named whenever one is known — the pet/reference/merchant filters exist for when it is not. A tool MUST NOT be left with no way to name the claim under discussion: with none available the model fabricated argument values from the schema's own description text (live, 2026-07-27).

#### Scenario: Requested mutation is confirmed
- **WHEN** the user asks the agent to perform a mutation (e.g. "mark Bella's claim sent")
- **THEN** the agent identifies the target claim, replies with a confirmation naming the claim and action plus a confirm button, and the mutation is applied only after the button is tapped — reusing the existing `claim_forms` / `claim_status` functions

#### Scenario: Mutation not confirmed
- **WHEN** the confirmation is shown but not tapped
- **THEN** no data changes

#### Scenario: Model reports a proposal as done
- **WHEN** the model's reply asserts the mutation has been applied before any tap
- **THEN** the data is unchanged, because the commit path was never entered

#### Scenario: Hard rules are honored
- **WHEN** a requested action would send an email or supply a required claim field (e.g. `condition_text`) that the user did not provide
- **THEN** the agent refuses to send email (drafts only) and refuses to invent the field, flagging it for the user instead of guessing

#### Scenario: Ambiguous target
- **WHEN** the requested action matches zero or multiple claims
- **THEN** the agent asks for clarification and commits nothing

#### Scenario: Target named by id
- **WHEN** the claim id is known — stated in the message or carried by the message it replies to
- **THEN** the act tool is called with that id and no pet/reference guesswork is involved

### Requirement: A message naming two pets is never a one-pet assignment
When the user's own message names more than one pet on file, a single-pet assignment SHALL NOT be proposed. The refusal SHALL instead direct a per-pet split with each pet's share, asking for the amounts if none were given. The refusal is enforced by the MCP server, not by the agent's prompt or configuration.

Rationale: the prompt rule alone lost live. Replaying "This is actually split between echo and Aari. Aari cost was $35 out of this" against the ASSIGN PET card on 2026-07-27, the primary model — no API error, the split tool present in the schema — proposed assigning Aari *and* Echo. Assigning one pet when two are named claims the whole charge against that pet, which is the over-claim this capability exists to prevent. Moving the loop to a general-purpose agent runtime makes prompt-level enforcement weaker still, not stronger.

**Known limitation:** the check is on pet names appearing in the message, so a phrasing that names two pets while genuinely meaning one ("that one is Aari's, not Echo's") is refused too. The refusal explains itself and the user can restate; a wrong claim cannot be restated once sent.

#### Scenario: Two pets named
- **WHEN** the message names two pets on file and the model calls the single-pet assignment proposal
- **THEN** nothing is queued, and the response directs a split with each pet's share

#### Scenario: One pet named
- **WHEN** the message names a single pet
- **THEN** pet assignment is proposed exactly as before

### Requirement: The real pet list is supplied, never guessed
The pets actually on file SHALL be supplied to the agent as turn context, along with today's date. The agent MUST NOT invent a pet name, nor ask the user to confirm a name he did not say.

The list SHALL be read from the database at turn time rather than written into static agent configuration, so a new pet does not require a config edit and a stale list cannot outlive the data.

Rationale: left to guess, the model produced "Whiskers" and "Fluffy" — pets that do not exist — and guessed at the year with no date to anchor on.

#### Scenario: A pet is needed but unknown
- **WHEN** the agent needs a pet and cannot tell which
- **THEN** it asks using only names from the supplied list

#### Scenario: A pet is added
- **WHEN** a new pet exists in the data
- **THEN** the next turn's context includes it with no configuration change

### Requirement: The agent never claims mailbox access it does not have
The agent SHALL NOT imply it has read, searched, or browsed the mailbox. Where a named sweep over mail exists, the agent MAY run it and report what that sweep found, stating plainly that this is a specific check rather than a mailbox search.

This SHALL be enforced by the tool inventory rather than by prompt alone: no mailbox-search, filesystem or browser tool is available to the agent. See `gmail-isolation-boundary`.

Rationale: an early live session had the agent answer "I checked your sent mail" when it had no such capability — a fabricated action, the most damaging failure mode for a single-user assistant.

#### Scenario: Asked to check sent mail
- **WHEN** the user asks the agent to look through his email
- **THEN** the agent says it cannot browse or search the mailbox, offers the named sweep that does exist, and never implies it looked

#### Scenario: No tool could satisfy it
- **WHEN** the agent's inventory is enumerated
- **THEN** no tool can search the mailbox, so the claim is unmakeable rather than merely discouraged

### Requirement: Bounded LLM usage per turn
A single chat turn SHALL stay within the configured provider's per-request limits by passing summarized claim data rather than raw full-email dumps, and the number of tool-calling iterations per turn SHALL be bounded, with a final answer forced on reaching the cap.

The iteration cap now belongs to the gateway agent's configuration rather than the app's own loop. It SHALL be set explicitly rather than left to the runtime's default, and reaching it SHALL produce a best answer rather than a silent stop.

#### Scenario: Turn stays within limits
- **WHEN** answering a question that could involve many claims or long emails
- **THEN** compact summaries sufficient to answer are sent

#### Scenario: Tool loop is bounded
- **WHEN** the agent invokes read/act tools to satisfy a turn
- **THEN** the iteration count is capped by explicit configuration, and on reaching the cap the agent replies with its best answer rather than looping indefinitely

#### Scenario: Cap reached
- **WHEN** a turn hits the iteration cap
- **THEN** the user receives an answer, and the truncation is visible rather than silent
