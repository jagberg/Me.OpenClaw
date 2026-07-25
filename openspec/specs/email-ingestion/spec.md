# email-ingestion Specification

## Purpose
Poll Gmail, turn qualifying mail into candidate tasks, never process the same message twice. `gmail_ingest.poll_once` + `gmail_client`. The assistant half of OpenClaw; the claims service uses the same `gmail_client` seam for its own searches.

See ADR-0004 (polling over push/watch).

## Requirements

### Requirement: Gmail access is read plus drafts, and the no-send guarantee is code-enforced
The system SHALL authenticate to Gmail via OAuth. Requested scopes are `gmail.readonly` and `gmail.compose`. The system SHALL NEVER call `send()` on the Gmail API — drafts only, for Justin to review and send himself.

**This supersedes the original requirement, and weakens it — recorded rather than quietly replaced.**

The original spec (2026-07, greenfield) required read-only scope and said the system "SHALL NOT request or use any write/send/modify scope". That was true when the only job was reading mail for tasks. It stopped being true when claim drafting shipped: drafts need `gmail.compose`.

The important nuance is that `gmail.compose` **does** grant send capability. So:

- **before**: sending was impossible because the token could not do it — enforced by Google;
- **now**: sending is possible for the token and simply never invoked — enforced by our own code.

The guarantee is therefore behavioural, not structural. What backs it: `send()` appears nowhere in `app/openclaw/` (verified 2026-07-25), it is a hard rule in `CLAUDE.md`, and it is the project's most-repeated constraint. A narrower alternative (`gmail.addons.current.action.compose`, or drafts via a separate restricted identity) was never evaluated — that is an **unrecorded gap**, not a decision.

#### Scenario: Initial OAuth consent
- **WHEN** the service is authorized for the first time (`scripts/gmail_auth.py`)
- **THEN** consent requests `gmail.readonly` + `gmail.compose`, and the resulting refresh token is stored locally under `app/data/`

#### Scenario: A draft is created
- **WHEN** the claims service prepares a submission or invoice request
- **THEN** it uses `drafts().create`/`update` and never `send()`

#### Scenario: Token expiry
- **WHEN** the stored token expires (Google testing-app apps expire refresh tokens every 7 days)
- **THEN** the failure is visible and recoverable by re-running `scripts/gmail_auth.py`, which needs interactive browser consent

### Requirement: Poll for new messages periodically
The system SHALL poll the Gmail account for new messages at a configurable interval rather than relying on push notifications.

#### Scenario: Polling picks up a new message
- **WHEN** a new email arrives in the connected Gmail account
- **THEN** the system detects it within one polling interval without requiring any inbound webhook

### Requirement: Dedupe already-processed messages
The system SHALL track which Gmail message IDs have already been processed and SHALL NOT surface the same message as a candidate task more than once.

This ledger is load-bearing beyond task capture: Petcover reply polling reuses it, and re-reading a seen reply would risk re-applying a status against the append-only event log.

#### Scenario: Same message seen across multiple polls
- **WHEN** a previously processed message is returned again by a subsequent poll
- **THEN** the system does not create a duplicate candidate task for it

#### Scenario: Processing fails part-way
- **WHEN** a message is fetched but processing raises before completion
- **THEN** it is left unmarked so the next poll retries it, rather than being silently dropped

### Requirement: Surface candidate task to task-capture
The system SHALL extract a candidate task description from a qualifying email and hand it to the task-capture pipeline for storage, with the source message ID.

Noise (automated notifications, bulk mail) is filtered before capture rather than becoming tasks.

#### Scenario: Actionable email produces a candidate task
- **WHEN** a polled email contains actionable content
- **THEN** the system passes a candidate task description and the source message ID to task-capture

#### Scenario: Noise is not captured
- **WHEN** a polled message is recognised as automated/bulk noise
- **THEN** no candidate task is created, and the message is marked processed so it isn't reconsidered
