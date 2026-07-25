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
The agent SHALL be able to perform the same mutations the slash commands expose (mark sent, set condition, assign pet, mark resolved), but every mutation MUST be presented as a confirmation with an inline confirm button and MUST NOT commit until the user taps confirm. The agent MUST NOT describe a proposed action as done.

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
