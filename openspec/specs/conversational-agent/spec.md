# conversational-agent Specification

## Purpose
Free-form Telegram chat over the claims domain: any non-command message from the authorized user becomes an LLM turn that can *read* current claim state immediately and *propose* mutations that commit only on a Confirm tap. The proposal gate is a harness property (`telegram_bot._execute_action`), not a behaviour the model is trusted to observe — that is what makes the project's hard rules enforceable here.

Implemented in `app/openclaw/agent.py` (tool registry + prompt) and `app/openclaw/telegram_bot.py` (routing, Confirm buttons, execution).

## Requirements

### Requirement: Authorized free-form chat
The Telegram bot SHALL treat any non-command text message from the authorized user as a chat turn handled by the LLM agent. Messages from anyone other than the authorized user MUST be ignored, matching existing command authorization.

#### Scenario: Authorized user sends free text
- **WHEN** the authorized user sends a plain-text message that is not a slash command and is not a pending free-text reply (e.g. condition entry)
- **THEN** the bot routes it to the conversational agent and replies in the same chat

#### Scenario: Unauthorized user sends free text
- **WHEN** a user other than the authorized username sends any message
- **THEN** the bot ignores it and logs the rejection, exactly as commands do

#### Scenario: Existing typed-reply flows still win
- **WHEN** the user is mid free-text entry for an existing flow (e.g. after tapping "Other (type it)" for a condition)
- **THEN** that pending flow consumes the message and the chat agent does NOT

### Requirement: Read-only interrogation of claims and their reply history
The agent SHALL answer questions about claims, claim status, and recorded Petcover replies by reading current data through a bounded set of read tools over existing `db` / `claim_status` functions. It MUST NOT expose bank credentials, secrets, or `.env` contents.

#### Scenario: Ask which claims are blocked
- **WHEN** the user asks something like "which claims are blocked?"
- **THEN** the agent reads current claim flags/status and replies with the blocked claims and the blocking reason

#### Scenario: Ask about a reply
- **WHEN** the user asks whether Petcover replied about a given pet or claim
- **THEN** the agent reads the recorded status events and answers, or states plainly that no reply is recorded

#### Scenario: Sensitive data is never returned
- **WHEN** the user asks for bank details, API keys, or `.env` contents
- **THEN** the agent declines and returns no secret values

### Requirement: Every claim reference carries its claim id
Any mention of a claim SHALL include its internal id as `#N`, alongside the amount and vet.

This **reverses** the original requirement, which said claims must be named by pet + Petcover reference and "not internal claim ids". That was wrong in practice and was reversed 2026-07-24 (commit `cc867e3`): Justin acts *by* id (`/mark 6 …`, `/pet 1 …`), so an id-less answer is unusable. Confirmed live — he asked what was outstanding, got a list with no ids, and could act on none of it. The original intent (don't make him decode internals) is served instead by including the human context next to the id, not by hiding it. A regression test enforces the id's presence.

#### Scenario: Listing claims in a chat answer
- **WHEN** the agent names any claim in a reply
- **THEN** the text includes `#<id>` for that claim plus its amount and vet

### Requirement: Outstanding work comes from the single authoritative derivation
"What do I need to do / what's outstanding / what's blocked" SHALL be answered by calling the shared `claim_status.pending_actions()` derivation, never by the agent assembling its own list from a claims query.

Rationale: `/actions` cards and chat must be incapable of disagreeing, and a hand-assembled list provably misses kinds (`pending_actions` covers nine).

#### Scenario: Asked what is outstanding
- **WHEN** the user asks what is waiting on him
- **THEN** the agent calls the shared pending-actions derivation and reports its entries

### Requirement: The agent never claims mailbox access it does not have
The agent SHALL NOT imply it has read, searched, or browsed the mailbox. Where a named sweep over mail exists, the agent MAY run it and report what that sweep found, stating plainly that this is a specific check rather than a mailbox search.

Rationale: an early live session had the agent answer "I checked your sent mail" when it had no such capability — a fabricated action, the most damaging failure mode for a single-user assistant.

#### Scenario: Asked to check sent mail
- **WHEN** the user asks the agent to look through his email
- **THEN** the agent says it cannot browse or search the mailbox, offers the named sweep that does exist, and never implies it looked

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

### Requirement: The real pet list is supplied, never guessed
The system prompt SHALL inject the pets actually on file. The agent MUST NOT invent a pet name, nor ask the user to confirm a name he did not say.

Rationale: left to guess, the model produced "Whiskers" and "Fluffy" — pets that do not exist.

#### Scenario: A pet is needed but unknown
- **WHEN** the agent needs a pet and cannot tell which
- **THEN** it asks using only names from the injected list

### Requirement: Bounded LLM usage per turn
A single chat turn SHALL stay within the configured provider's per-request limits by passing summarized claim data rather than raw full-email dumps, and SHALL bound the number of tool-calling iterations per turn, forcing a final answer on reaching the cap.

#### Scenario: Turn stays within limits
- **WHEN** answering a question that could involve many claims or long emails
- **THEN** the agent sends compact summaries sufficient to answer

#### Scenario: Tool loop is bounded
- **WHEN** the agent invokes read/act tools to satisfy a turn
- **THEN** the number of tool iterations is capped, and on reaching the cap the agent replies with its best answer rather than looping indefinitely

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

## Known limitation — "do X" with no tool for X becomes a saved task (found 2026-07-25, live)

`propose_create_task` accepts any free-text description, so it is the tool that always fits. When Justin asks for an operation the surface does not implement, the agent does not say "I can't do that" — it proposes a task describing the request, and the confirm tap saves it.

Observed live: "The #7 claim needs to be redone" produced task #125 *"Redo claim #7 for Aari"*, and a vaguer earlier attempt produced #124. Both were reasonable records of intent and neither was the action Justin expected; he read the ✅ as the claim having been redone. The requirement above about never claiming mailbox access it lacks has no counterpart for capabilities it lacks.

This is a **caveat, not a decided behaviour** — the alternative (teach the agent to refuse when no tool matches) was never evaluated, and saving the intent is arguably better than dropping it. What is clearly wrong is that the confirmation is indistinguishable from having done the thing. Unresolved; the concrete instance is tracked in `openspec/BACKLOG.md` ("What does 'redo claim #N' mean?"), which also has to be answered before a real redo tool can exist.
