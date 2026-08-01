## MODIFIED Requirements

### Requirement: Single authorized user, identified by Telegram username
The system SHALL authorize inbound events by comparing the sender's Telegram username, as reported by the gateway, against the configured `TELEGRAM_USERNAME`, and SHALL ignore any event from a different or missing username, logging the rejection.

The compare MUST be case-insensitive: Telegram usernames are case-insensitive but the API reports display casing (e.g. "Jagberg"), so an exact compare wrongly rejects the real user.

The check SHALL remain the app's own and SHALL NOT be delegated to the gateway's sender-trust model. The gateway's DM-pairing behaviour, which exists to let untrusted senders reach an agent, SHALL be configured off or restricted to the single authorized username, and the app SHALL still reject a mismatch even if the gateway admitted the sender.

#### Scenario: Command from an unauthorized username
- **WHEN** an event arrives from a user whose username is not the configured one (or who has no username)
- **THEN** the command is not executed, no claim state changes, and the rejection is logged

#### Scenario: Display casing differs
- **WHEN** the API reports the authorized username with different capitalisation
- **THEN** it is still authorized

#### Scenario: Gateway admits a stranger
- **WHEN** the gateway delivers an event from a sender it paired but who is not the configured username
- **THEN** the app rejects it, executes nothing, and logs the rejection

### Requirement: Self-service chat registration via /start
The system SHALL let the authorized user register the chat ID for outbound pushes by sending `/start`, persisting that chat ID from the gateway-delivered event. Outbound notifications SHALL be skipped and the gap logged visibly — never silently dropped — if no chat ID is registered.

The app SHALL keep its own record of the chat ID rather than relying on the gateway's session state, because unattended pipeline notifications originate in Python with no inbound event to reply to and therefore need an addressable target of their own.

#### Scenario: First-time registration
- **WHEN** the authorized user sends `/start` and no chat ID is registered
- **THEN** the chat ID from that event is persisted and a confirmation is sent back

#### Scenario: Notification attempted before registration
- **WHEN** a claim needs a notification but no chat ID is registered
- **THEN** no send is attempted and the gap is logged visibly, consistent with the project's failure-visibility rule

### Requirement: Inbound command dispatch reuses existing update paths
Commands SHALL be a thin adapter over the existing claim-update logic rather than duplicating it, so dashboard and Telegram cannot drift. The pure command logic SHALL be independent of the transport — neither the Telegram library nor the gateway's event shape — so it is testable without constructing a gateway event.

#### Scenario: Telegram-supplied condition text matches dashboard behaviour
- **WHEN** Justin sends `/mark <claim_id> <condition text>`
- **THEN** the same update is applied as the dashboard's condition route, and the claim proceeds through the normal fill/draft flow if now complete

#### Scenario: Command logic tested without the gateway
- **WHEN** the command logic is exercised by the smoke suite
- **THEN** it runs with no gateway present and no transport object constructed

### Requirement: Every incoming message is acknowledged immediately
The system SHALL react to every message from the authorized user before the real handler runs, so a slow handler does not feel dead. The acknowledgement SHALL be emitted through the gateway's reaction action. An acknowledgement failure MUST NOT break the handler.

#### Scenario: A slow handler
- **WHEN** a message triggers a long-running handler (an LLM turn)
- **THEN** the message is acknowledged first, and the eventual reply follows

#### Scenario: Reaction action fails
- **WHEN** the gateway rejects or drops the reaction
- **THEN** the handler still runs to completion and the failure is logged

### Requirement: Tapped-message results are appended without crashing on documents
Appending a result to a tapped message SHALL edit the caption when the message carries a document or photo and the text otherwise, using the gateway's message-edit action.

Messages carrying a PDF have no `text`, so editing text crashes on exactly the review alerts that most need feedback. The same split applies to rendered card images, which are photos and likewise carry a caption rather than text.

#### Scenario: Tap on a PDF review alert
- **WHEN** a button is tapped on a message that carries a document
- **THEN** the result is appended to the caption, not the text

#### Scenario: Tap on a rendered card
- **WHEN** a button is tapped on a photo card
- **THEN** the result is appended to that card's caption

#### Scenario: Caption editing unsupported by the action
- **WHEN** the gateway's edit action cannot target a caption
- **THEN** the result is delivered as a reply to the card rather than lost, and the shortfall is recorded

### Requirement: An edited message is handled, not dropped
An edit SHALL be handled on the same path as a new message, whatever shape the gateway delivers it in, and the message log SHALL record it with a truthful kind and summary rather than an empty `other` row.

Rationale: on 2026-07-27 Justin edited his message to add the pet's share. The handler raised `'NoneType' object has no attribute 'text'`, the update was logged as kind `other` with an empty summary, and the correction he had just made was invisible in the log as well as unprocessed. The underlying cause was reading a transport-specific field rather than the effective message; the requirement outlives the transport.

#### Scenario: Authorized user edits a text message
- **WHEN** an edit event arrives from the authorized user
- **THEN** it is handled on the same path as a new text message and the log row names it as an edit with its text

#### Scenario: Edit acknowledged
- **WHEN** an edited message is received
- **THEN** the acknowledgement path runs without raising, as it does for a new message

### Requirement: No autonomous send
The system SHALL NOT expose any command, tool, or agent capability that sends a Gmail claim email. Reviewing and sending remains a manual action Justin takes in Gmail.

The guarantee SHALL be verified across both runtimes: `send()` absent from `app/openclaw/`, and no send capability in the gateway agent's tool inventory. Note the OAuth token *is* capable of sending (`gmail.compose`) — see `gmail-isolation-boundary` for why this guarantee is code-enforced rather than scope-enforced, and why the gateway is deliberately given no Gmail credential.

#### Scenario: No send command exists
- **WHEN** any command is sent
- **THEN** no code path calls Gmail's send endpoint

#### Scenario: No send tool exists
- **WHEN** the agent's tool inventory is enumerated
- **THEN** it contains no tool that sends mail

## ADDED Requirements

### Requirement: The card interface is preserved feature-for-feature
Every element of the existing Telegram interface SHALL survive the transport change: Pillow-rendered claim-history and actions-summary cards sent as photos, inline keyboards on both text and photo messages, one-tap condition and pet buttons, tap-to-resolve action cards, "Wrong invoice", Confirm buttons on proposals, paging, and the PDF review alerts.

Callback payloads SHALL continue to carry an index or id rather than free text, preserving the existing 64-byte discipline. A UI element that cannot be reproduced through the gateway SHALL be reported as a blocking gap before the old transport is retired — not quietly dropped.

#### Scenario: Actions view requested after the swap
- **WHEN** the actions view is requested
- **THEN** the summary card and per-item tap-to-resolve cards are delivered with working buttons, as before

#### Scenario: Condition chosen in one tap
- **WHEN** Justin taps a past condition on a blocked claim
- **THEN** the callback reaches the app, the condition is applied, and the claim proceeds through the fill/draft flow

#### Scenario: Buttons on a photo card are unsupported
- **WHEN** inline buttons cannot be attached to a photo through the gateway
- **THEN** the gap is reported as blocking and the old transport is not retired until it is resolved

### Requirement: The durable message log remains the app's own
`telegram_messages` SHALL continue to record every inbound and outbound message with its raw payload and the app version, and `record_inbound` SHALL still write before the handler runs so that a crash mid-handler leaves the row unprocessed and replayable. The gateway's own logging SHALL NOT be treated as a substitute.

Rationale: the log is three things the gateway does not promise — the RL dataset, the audit trail for "did my tap register?", and the replay queue (ADR-0014).

#### Scenario: Crash mid-handler
- **WHEN** the app crashes while handling a gateway-delivered event
- **THEN** the row remains unprocessed and is re-run by the replay queue at startup

#### Scenario: Duplicate delivery from the gateway
- **WHEN** the gateway redelivers an event the app already processed
- **THEN** the handler's at-least-once guarantees hold and no duplicate mutation is committed
