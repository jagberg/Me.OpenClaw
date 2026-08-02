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

The caption SHALL be named explicitly on the edit rather than left to the platform's text-first fallback. Verified live 2026-08-01: the fallback succeeds on a document, but only after a rejected `editMessageText` call that is logged as `editMessage failed` — on the successful path. A log line reading as a failure every time a tap succeeds is how a real failure stops being visible, which the project's failure-visibility rule exists to prevent.

#### Scenario: Tap on a PDF review alert
- **WHEN** a button is tapped on a message that carries a document
- **THEN** the result is appended to the caption, not the text

#### Scenario: Tap on a rendered card
- **WHEN** a button is tapped on a photo card
- **THEN** the result is appended to that card's caption

#### Scenario: A successful media edit logs no failure
- **WHEN** a caption edit on a document or photo succeeds
- **THEN** no failed edit attempt precedes it and nothing resembling an error is logged, because the caption was named rather than discovered by fallback

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

### Requirement: Interactive condition entry
When a claim is blocked needing a condition, the notification SHALL show the invoice line items and offer the pet's previously-used conditions as one-tap buttons, an "Other" button prompting free-text, and — when the invoice has more than one line item — a per-item split option. The message SHALL NOT tell Justin to use the dashboard.

**The payload mechanism changes and the discipline does not.** The buttons carry a `command` action naming a registered slash command with an **index** into the re-queried condition list — never the condition text, which routinely exceeds the limit. The budget is 58 UTF-8 bytes rather than 64, because the gateway prefixes `tgcmd:`, and overflow is silent: the button is dropped from its row and a message whose only row was dropped arrives with no keyboard at all.

Restated here rather than left in the baseline because the baseline says `callback_data`, and this change adds a requirement that callback actions SHALL NOT be used for the card interface. Archiving without this MODIFIED block would put both in `openspec/specs/telegram-bot/spec.md` at once.

#### Scenario: Repeat condition in one tap
- **WHEN** Justin taps a past condition on a blocked claim
- **THEN** the registered command runs with the index, the condition is recorded from the re-queried list, and no model is involved

#### Scenario: A condition name too long for the budget
- **WHEN** a generated command string would exceed 58 UTF-8 bytes
- **THEN** the send is refused before it leaves the app, because the platform would drop the button silently and report success

## ADDED Requirements

### Requirement: Buttons are command actions, and a tap never involves a model
Every interactive button SHALL carry `action.type: "command"` naming a slash command the in-gateway plugin has registered. Callback actions SHALL NOT be used for the card interface.

Verified 2026-08-01: a `command` button dispatches through core's native command path and a plugin-registered command executes and replies. A `callback` action's value is wrapped by `buildTelegramOpaqueCallbackData` before reaching Telegram, so the raw value never survives and the namespace resolver cannot match it — the opaque form exists for a plugin's own send-and-decode round trip, not for values supplied from outside.

Consequence worth stating: this makes the entire tap path deterministic. No token is interpreted, no model runs, and the existing command surface (`/mark`, `/pet`, `/resolve`) becomes the button target rather than needing a parallel callback vocabulary.

A button's determinism depends on its command being **registered**. Every command string a button can emit SHALL be asserted registered at deploy, and any message reaching the agent that parses as one of the app's command strings SHALL be refused as an error rather than answered.

Rationale, measured live 2026-08-01: a button carrying `/ping` — a command nothing had registered — did not error and did not no-op. It was delivered to the agent as a chat turn, which replied conversationally and spent tokens. A typo, a plugin that failed one of its two silent load gates, or a command renamed on one side is therefore enough to route `/mark 7 sent` through a model as free text, which is the one path this design exists to prevent.

#### Scenario: Tap resolves a claim
- **WHEN** the user taps a tap-to-resolve button on an actions card
- **THEN** the named slash command runs through the plugin and applies the change, with no model invoked

#### Scenario: A button names an unregistered command
- **WHEN** a button's command is not registered by the plugin
- **THEN** the deploy fails naming that command, because at runtime the tap would silently become a model turn instead of an error

#### Scenario: A command string reaches the agent
- **WHEN** the agent receives a turn whose text parses as one of the app's command strings
- **THEN** it is refused and reported as a broken deterministic path, and no claim data is read or changed on the strength of it

A command string SHALL be at most **58 UTF-8 bytes**, and this SHALL be checked before send. The limit is Telegram's 64-byte `callback_data` ceiling less the platform's 6-byte `tgcmd:` prefix, and it is measured in bytes rather than characters. Buttons SHALL continue to name their target by id or index rather than by text, which keeps the longest real command near 46 bytes.

Rationale for checking rather than trusting: an over-long command is not rejected and does not produce a dead button. The platform drops the button from its row, drops the row if it is then empty, and sends a message with no keyboard — returning success with a real message id.

#### Scenario: Command string exceeds the transport limit
- **WHEN** a button's command string would exceed 58 UTF-8 bytes
- **THEN** the failure is caught before send and names the offending string, rather than the button silently vanishing from the keyboard

#### Scenario: The limit is counted in bytes
- **WHEN** a command carries a non-ASCII pet or condition name
- **THEN** the check measures its encoded byte length, not its character count

#### Scenario: Presentation payload is malformed
- **WHEN** a message's button payload does not match the platform's presentation contract
- **THEN** it is rejected before sending, because the platform discards a malformed presentation silently and still returns success with a real message id

### Requirement: The card interface is preserved feature-for-feature
Every element of the existing Telegram interface SHALL survive the transport change: Pillow-rendered claim-history and actions-summary cards sent as photos, inline keyboards on both text and photo messages, one-tap condition and pet buttons, tap-to-resolve action cards, "Wrong invoice", Confirm buttons on proposals, paging, and the PDF review alerts.

Button payloads SHALL continue to carry an index or id rather than free text. The discipline survives, but the mechanism and the number both change: the card interface uses `command` actions, not callbacks, and the budget is **58 UTF-8 bytes** — Telegram's 64 less the gateway's `tgcmd:` prefix — measured at the boundary, where 58 renders and 59 deletes the button with no error. A UI element that cannot be reproduced through the gateway SHALL be reported as a blocking gap before the old transport is retired — not quietly dropped.

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
