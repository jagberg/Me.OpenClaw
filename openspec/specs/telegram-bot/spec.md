# telegram-bot Specification

## Purpose
The phone-side interface: a single-user bot that pushes what needs Justin's attention and takes taps back. Commands, callbacks, notification dedup, and authorization. `telegram_bot.py` + `claim_card.py`.

ADR-0003 originally deferred any push channel to keep v1 narrow; this capability is that deferral being lifted. Related: ADR-0014 (durable message log + replay), ADR-0015 (dead-updater restart), ADR-0016 (the free-chat agent's tool surface — a separate capability, `conversational-agent`).

## Requirements

### Requirement: Single authorized user, identified by Telegram username
The system SHALL authorize inbound commands by comparing the sender's Telegram username against the configured `TELEGRAM_USERNAME`, and SHALL ignore any command from a different or missing username, logging the rejection.

The compare MUST be case-insensitive: Telegram usernames are case-insensitive but the API reports display casing (e.g. "Jagberg"), so an exact compare wrongly rejects the real user.

#### Scenario: Command from an unauthorized username
- **WHEN** an update arrives from a user whose username is not the configured one (or who has no username)
- **THEN** the command is not executed, no claim state changes, and the rejection is logged

#### Scenario: Display casing differs
- **WHEN** the API reports the authorized username with different capitalisation
- **THEN** it is still authorized

### Requirement: Self-service chat registration via /start
The system SHALL let the authorized user register the chat ID for outbound pushes by sending `/start`, persisting that chat ID. Outbound notifications SHALL be skipped and the gap logged visibly — never silently dropped — if no chat ID is registered.

#### Scenario: First-time registration
- **WHEN** the authorized user sends `/start` and no chat ID is registered
- **THEN** the chat ID from that update is persisted and a confirmation is sent back

#### Scenario: Notification attempted before registration
- **WHEN** a claim needs a notification but no chat ID is registered
- **THEN** no send is attempted and the gap is logged visibly, consistent with the project's failure-visibility rule

### Requirement: Outbound notification on claim state change, deduped
The system SHALL notify when a claim enters a state needing attention, and SHALL NOT re-notify a claim still sitting in the state it was last notified for.

**This dedup is a change-feed, not a state list** — it keys on `(telegram_notified_status, telegram_notified_flag)`, so a claim that *stays* outstanding is announced once and never again. That is how two drafted claims sat unsent for three days in silence. The state-based counterpart is `claim_status.pending_actions()` plus the daily `nudge_stale_actions` job; neither replaces the other.

#### Scenario: Claim newly stuck at matched, missing condition
- **WHEN** a claim advances to `matched` and lacks condition text
- **THEN** a message identifying the claim and the missing field is sent once, and the last-notified state is recorded

#### Scenario: Pipeline tick with no state change
- **WHEN** a tick finds a claim still at `matched` with the same missing field as last notified
- **THEN** no duplicate notification is sent

#### Scenario: An outstanding action goes stale
- **WHEN** an actionable claim has been waiting longer than the configured nudge threshold
- **THEN** the daily nudge job sends one summary covering everything stale, rather than re-notifying per claim

### Requirement: Every notification carries the claim id
Every outbound message mentioning a claim SHALL include its id as `#N`.

Justin acts by id (`/mark 6 …`, `/pet 1 …`), so a message without one is unusable. A regression test enforces it. The chat agent was originally told the opposite and produced answers he could act on none of.

#### Scenario: Any claim notification
- **WHEN** a message refers to a claim
- **THEN** the claim's `#id` appears in it

### Requirement: Batched claims notify once, self-contained
Claims sharing one Gmail draft (a batch submission) SHALL be summarized in a single message, not one per claim. Because a specific Gmail draft cannot be deep-linked on mobile, the message SHALL carry the claim details (pet, per-item date/service/amount, total) so it can be reviewed without opening the draft, plus a best-effort Drafts subject-search link.

#### Scenario: Three claims in one draft
- **WHEN** three drafted claims share one `draft_id`
- **THEN** a single message lists all three with amounts and a combined total, sent once

### Requirement: Notification on Petcover lifecycle status changes
The system SHALL notify on claims entering Petcover lifecycle states, with urgent tone for those requiring action (`info_requested`, `suspended`) and informational tone otherwise, using the same once-per-state dedup.

Notified states have grown since this capability shipped and now include `approved` and `below_excess`. `approved` matters specifically because it is the **only** letter carrying Petcover's dollar breakdown — the later `settled` mail carries none (see `settlement-validation`). A settlement mismatch is surfaced with a warning marker rather than reported as a clean settlement.

#### Scenario: Info request pushed urgently
- **WHEN** a claim's status becomes `info_requested`
- **THEN** a message stating a reply is needed is sent once

#### Scenario: Approval carries the figures
- **WHEN** a claim is approved and the event carries claimed/paid amounts
- **THEN** the message includes them, and flags any mismatch against our own expectation

#### Scenario: Under the excess
- **WHEN** a claim comes back below the fixed excess
- **THEN** the message says it is not yet payable and the invoice is kept on file — it is not reported as a settlement or a decline

### Requirement: Inbound command dispatch reuses existing update paths
Commands SHALL be a thin adapter over the existing claim-update logic rather than duplicating it, so dashboard and Telegram cannot drift. The pure command logic SHALL be independent of the Telegram library so it is testable without constructing an Update.

#### Scenario: Telegram-supplied condition text matches dashboard behaviour
- **WHEN** Justin sends `/mark <claim_id> <condition text>`
- **THEN** the same update is applied as the dashboard's condition route, and the claim proceeds through the normal fill/draft flow if now complete

### Requirement: Mark a drafted claim reviewed
The system SHALL let Justin mark a `drafted` claim reviewed, recording a `reviewed_at` timestamp only. It SHALL NOT send the Gmail draft; status and draft remain unchanged.

#### Scenario: Marking a drafted claim reviewed
- **WHEN** `/mark <claim_id> reviewed` is sent for a claim at `drafted`
- **THEN** `reviewed_at` is set, Telegram confirms, and there is no status change and no Gmail send call

#### Scenario: Reviewed command on a claim not yet drafted
- **WHEN** the same command is sent for a claim not at `drafted`
- **THEN** it is rejected with a message explaining the claim isn't ready for review

### Requirement: Mark sent and confirm resolved
The system SHALL provide mark-sent (advances drafted→sent, batch-aware across claims sharing a draft, which starts Petcover reply tracking) and confirm-resolved (records a `confirmed_resolved` event clearing needs-action), reusing the dashboard's logic.

#### Scenario: Sent advances the whole submission
- **WHEN** mark-sent is used for one claim of a multi-claim batch draft
- **THEN** every claim sharing that draft advances to `sent`

#### Scenario: Resolved clears needs-action
- **WHEN** confirm-resolved is used after answering an info request
- **THEN** a `confirmed_resolved` event is recorded for the claim

### Requirement: Supply a vet's contact email
The system SHALL let the authorized user set or update a vet merchant's contact email, writing to the `vet_contacts` override table that invoice-request drafting reads first. This closes the otherwise un-actionable "no vet email on file" flag.

#### Scenario: First-time vet email
- **WHEN** an email is set for a merchant with no `vet_contacts` row
- **THEN** the row is created and subsequent invoice-request drafts for that merchant address it

#### Scenario: Updating an existing vet email
- **WHEN** an email is set for a merchant that already has a row
- **THEN** it is replaced, not duplicated

### Requirement: Interactive condition entry
When a claim is blocked needing a condition, the notification SHALL show the invoice line items and offer the pet's previously-used conditions as one-tap buttons, an "Other" button prompting free-text, and — when the invoice has more than one line item — a per-item split option. The message SHALL NOT tell Justin to use the dashboard.

`callback_data` carries an index into the re-queried condition list rather than the text itself, because condition text can exceed Telegram's 64-byte callback limit.

#### Scenario: Repeat condition in one tap
- **WHEN** Justin taps a past condition on a blocked claim
- **THEN** that condition is applied and the claim proceeds through the fill/draft flow

#### Scenario: New condition typed
- **WHEN** Justin taps "Other" and replies with text
- **THEN** the replied text becomes the claim's condition

### Requirement: Per-item condition split
For an invoice covering more than one condition, the system SHALL let Justin assign a condition per line item (tap a past one, type a new one, or mark it not-claimable), then group items by condition into one claim-form row each with amounts summed per condition. If the line items carry no extracted amounts, the system SHALL refuse rather than fill $0 rows.

#### Scenario: Two conditions on one invoice
- **WHEN** some items are assigned to one condition and others to a second
- **THEN** the claim form gets one row per condition, each charged the sum of its items' amounts

#### Scenario: Items have no amounts
- **WHEN** line items were extracted without per-item amounts
- **THEN** the split is refused with an explanation, and no $0 rows are filled

### Requirement: Assign a pet by tap
An unattributed claim SHALL offer a one-tap button per known pet, alongside (not replacing) the dashboard picker and invoice-based auto-assignment.

#### Scenario: Tap to assign
- **WHEN** Justin taps a pet on an unassigned claim
- **THEN** the claim's pet is set, identically to the dashboard picker

### Requirement: Reject a wrong invoice match
A matched claim whose bank charge greatly exceeds the matched invoice SHALL be flagged with a plain-language summary and a "Wrong invoice" button. Tapping it SHALL record the rejected invoice email so the matcher never re-selects it, and reset the claim to `pending_match`.

#### Scenario: Unmatch a bad match
- **WHEN** Justin taps "Wrong invoice"
- **THEN** the claim returns to `pending_match`, the rejected email is remembered, and the next match attempt skips it

### Requirement: Claim history and outstanding actions on demand
The system SHALL provide a paged view of the past year's claims and a view of everything currently waiting on Justin, both rendered as card images, with one tap-to-resolve card per outstanding actionable item.

Added after the original change. The actions view derives from the shared `claim_status.pending_actions()` so it cannot disagree with the chat agent's answer, separates blocked items (no tap can clear them), and states what it held back rather than truncating silently.

#### Scenario: Actions requested
- **WHEN** the actions view is requested
- **THEN** a summary card is sent plus one tap-to-resolve card per actionable item, with blocked items reported separately

#### Scenario: More actions than the card cap
- **WHEN** actionable items exceed the display cap
- **THEN** the count held back is stated explicitly

### Requirement: Every incoming message is acknowledged immediately
The system SHALL react to every message from the authorized user before the real handler runs, so a slow handler does not feel dead. An acknowledgement failure MUST NOT break the handler.

#### Scenario: A slow handler
- **WHEN** a message triggers a long-running handler (an LLM turn)
- **THEN** the message is acknowledged first, and the eventual reply follows

### Requirement: Tapped-message results are appended without crashing on documents
Appending a result to a tapped message SHALL edit the caption when the message carries a document and the text otherwise.

Messages carrying a PDF have no `text`, so editing text crashes on exactly the review alerts that most need feedback.

#### Scenario: Tap on a PDF review alert
- **WHEN** a button is tapped on a message that carries a document
- **THEN** the result is appended to the caption, not the text

### Requirement: No autonomous send
The system SHALL NOT expose any command that sends a Gmail claim email. Reviewing and sending remains a manual action Justin takes in Gmail.

Verified structurally: `send()` appears nowhere in `app/openclaw/` (2026-07-25). Note the OAuth token *is* capable of sending (`gmail.compose`) — see `email-ingestion` for why this guarantee is code-enforced rather than scope-enforced.

#### Scenario: No send command exists
- **WHEN** any command is sent
- **THEN** no code path calls Gmail's send endpoint
