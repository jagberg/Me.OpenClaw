## ADDED Requirements

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

### Requirement: Read-only interrogation of claims and emails

The agent SHALL answer questions about claims, claim status, and matched emails by reading current data through a bounded set of read tools over existing `db` / `claim_status` / `gmail_client` functions. It MUST NOT expose bank credentials, secrets, or `.env` contents.

#### Scenario: Ask which claims are blocked

- **WHEN** the user asks something like "which claims are blocked?"
- **THEN** the agent reads current claim flags/status and replies with the blocked claims identified by pet name and Petcover reference (not internal claim ids), including the blocking reason

#### Scenario: Ask about an email/reply

- **WHEN** the user asks whether Petcover replied about a given pet or claim
- **THEN** the agent reads the relevant matched email/status events and answers, or states plainly that no reply is recorded

#### Scenario: Sensitive data is never returned

- **WHEN** the user asks for bank details, API keys, or `.env` contents
- **THEN** the agent declines and returns no secret values

### Requirement: Act on claims with confirm-before-commit

The agent SHALL be able to perform the same mutations the slash commands expose (mark sent, set condition, assign pet, mark resolved), but every mutation MUST be presented as a confirmation with an inline confirm button and MUST NOT commit until the user taps confirm.

#### Scenario: Requested mutation is confirmed

- **WHEN** the user asks the agent to perform a mutation (e.g. "mark Bella's claim sent")
- **THEN** the agent identifies the target claim, replies with a confirmation naming the claim (pet + Petcover reference) and action plus a confirm button, and applies the mutation only after the button is tapped — reusing the existing `claim_forms` / `claim_status` functions

#### Scenario: Mutation not confirmed

- **WHEN** the confirmation is shown but not tapped
- **THEN** no data changes

#### Scenario: Hard rules are honored

- **WHEN** a requested action would send an email or supply a required claim field (e.g. `condition_text`) that the user did not provide
- **THEN** the agent refuses to send email (drafts only) and refuses to invent the field, flagging it for the user instead of guessing

#### Scenario: Ambiguous target

- **WHEN** the requested action matches zero or multiple claims
- **THEN** the agent asks for clarification and commits nothing

### Requirement: Bounded LLM usage per turn

A single chat turn SHALL stay within the configured provider's per-request limits (including its context cap) by passing summarized claim/email data rather than raw full-email dumps, and SHALL bound the number of tool-calling iterations per turn.

#### Scenario: Turn stays within context cap

- **WHEN** answering a question that could involve many claims or long emails
- **THEN** the agent sends compact summaries sufficient to answer, keeping the request within the provider's context limit

#### Scenario: Tool loop is bounded

- **WHEN** the agent invokes read/act tools to satisfy a turn
- **THEN** the number of tool iterations is capped, and on reaching the cap the agent replies with its best answer rather than looping indefinitely
