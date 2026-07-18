## ADDED Requirements

### Requirement: Single authorized user, identified by Telegram username
The system SHALL authorize inbound commands by comparing the sender's Telegram username against the configured `TELEGRAM_USERNAME` (`jagberg`), and SHALL ignore (not act on, optionally reply with a rejection) any command from a different or missing username.

#### Scenario: Command from an unauthorized username
- **WHEN** a Telegram update arrives from a user whose username is not `jagberg` (or has no username set)
- **THEN** the command is not executed and no claim state changes

#### Scenario: Command from the authorized username
- **WHEN** a Telegram update arrives from the user `jagberg`
- **THEN** the command is parsed and dispatched normally

### Requirement: Self-service chat registration via /start
The system SHALL let the authorized user register the chat ID Telegram will accept outbound pushes on, by sending `/start`. On a matching-username `/start`, the system SHALL persist the resulting chat ID for use by future outbound notifications. Outbound notifications SHALL be skipped (and the gap surfaced, not silently dropped) if no chat ID has been registered yet.

#### Scenario: First-time registration
- **WHEN** the user `jagberg` sends `/start` and no chat ID is yet registered
- **THEN** the chat ID from that update is persisted and a confirmation is sent back

#### Scenario: Notification attempted before registration
- **WHEN** a claim needs a notification but no chat ID has been registered
- **THEN** no send is attempted and the gap is logged visibly, consistent with the existing Gemini/Gmail failure-visibility pattern

### Requirement: Outbound notification on claim state change
The system SHALL send a Telegram message to Justin when a claim transitions into a state that needs his attention (`matched` with a missing required field, or `drafted`), and SHALL NOT re-send a notification for a claim still sitting in the same state it was last notified for.

#### Scenario: Claim newly stuck at matched, missing condition
- **WHEN** a claim advances from `pending_match` to `matched` and lacks condition text
- **THEN** a Telegram message identifying the claim and the missing field is sent once, and the claim's last-notified state is recorded

#### Scenario: Claim newly drafted
- **WHEN** a claim advances to `drafted`
- **THEN** a Telegram message with the claim details and a link to the Gmail draft is sent once

#### Scenario: Pipeline tick with no state change
- **WHEN** a pipeline run finds a claim still at `matched` with the same missing field as last notified
- **THEN** no duplicate notification is sent

### Requirement: Inbound command dispatch reuses existing update paths
The system SHALL implement Telegram commands as a thin adapter over the existing claim-update logic (condition text, pet assignment, immediate pipeline advance) rather than duplicating that logic, so dashboard and Telegram stay consistent.

#### Scenario: Telegram-supplied condition text matches dashboard behavior
- **WHEN** Justin sends `/mark <claim_id> <condition text>` in Telegram
- **THEN** the same update is applied as the dashboard's `POST /claims/{id}/condition` route, and the claim proceeds through the normal fill/draft flow if now complete

### Requirement: Mark a drafted claim reviewed
The system SHALL let Justin mark a `drafted` claim as reviewed from Telegram, recording a reviewed timestamp only. This SHALL NOT trigger sending the Gmail draft — the claim's status and draft remain unchanged; only a `reviewed_at` value is set.

#### Scenario: Justin marks a drafted claim reviewed
- **WHEN** Justin sends `/mark <claim_id> reviewed` for a claim at `drafted`
- **THEN** the claim's `reviewed_at` is set and Telegram confirms, with no change to claim status and no Gmail send call

#### Scenario: Reviewed command on a claim not yet drafted
- **WHEN** Justin sends `/mark <claim_id> reviewed` for a claim not at `drafted`
- **THEN** the command is rejected with a message explaining the claim isn't ready for review yet

### Requirement: Supply a vet's contact email via Telegram
The system SHALL let the authorized user set or update a vet merchant's contact email via `/vetemail <merchant name> <email>`, writing to the `vet_contacts` override table that invoice-request drafting reads first. This closes the previously un-actionable "no vet email on file" flag.

#### Scenario: First-time vet email
- **WHEN** Justin sends `/vetemail <merchant> <email>` for a merchant with no `vet_contacts` row
- **THEN** the row is created and subsequent invoice-request drafts for that merchant address it

#### Scenario: Updating an existing vet email
- **WHEN** Justin sends `/vetemail` for a merchant that already has a row
- **THEN** the email is replaced, not duplicated

### Requirement: Batched claims notify once, self-contained
Claims sharing one Gmail draft (a batch submission) SHALL be summarized in a single Telegram message, not one per claim. Because a specific Gmail draft cannot be deep-linked on mobile, the message SHALL carry the claim details (pet, per-item date/service/amount, total) so it can be reviewed without opening the draft, plus a best-effort Drafts subject-search link.

#### Scenario: Three claims in one draft
- **WHEN** three drafted claims share one `draft_id`
- **THEN** a single message lists all three with amounts and a combined total, sent once

### Requirement: Notification on Petcover lifecycle status changes
The system SHALL notify on claims entering Petcover lifecycle states: urgent tone for `info_requested` and `suspended` (Justin must act), informational for `acknowledged`, `settled` (with claimed-vs-paid amounts when available from the settlement event), and `declined` — with the same once-per-state dedup as the matched/drafted notifications.

#### Scenario: Info request pushed urgently
- **WHEN** a claim's status becomes `info_requested`
- **THEN** a Telegram message stating a reply is needed is sent once

#### Scenario: Settlement includes reconciliation figures
- **WHEN** a claim settles and the settlement event carries claimed/paid amounts
- **THEN** the Telegram message includes both figures

### Requirement: Mark sent and confirm resolved via Telegram
The system SHALL provide `/sent <claim_id>` (advances drafted→sent, batch-aware across claims sharing one draft, which starts Petcover reply tracking) and `/resolved <claim_id>` (records a `confirmed_resolved` event clearing the needs-action state), reusing the same logic as the dashboard routes.

#### Scenario: Sent advances the whole submission
- **WHEN** Justin sends `/sent` for one claim of a multi-claim batch draft
- **THEN** every claim sharing that draft advances to `sent`

#### Scenario: Resolved clears needs-action
- **WHEN** Justin sends `/resolved <claim_id>` after answering an info request
- **THEN** a `confirmed_resolved` event is recorded for the claim

### Requirement: No autonomous send via Telegram
The system SHALL NOT expose any Telegram command that sends the Gmail claim email. Reviewing and sending remains a manual action Justin takes from the Gmail draft link.

#### Scenario: No send command exists
- **WHEN** Justin sends any Telegram command
- **THEN** no code path calls Gmail's send endpoint for a claim email
