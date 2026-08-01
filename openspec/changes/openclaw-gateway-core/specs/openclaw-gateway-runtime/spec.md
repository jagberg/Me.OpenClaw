## ADDED Requirements

### Requirement: The gateway owns channel transport; the app owns the domain
The OpenClaw gateway SHALL be the only process holding the Telegram bot token and the only process polling Telegram. The Python app SHALL NOT run its own updater. Claims logic, Gmail access, the database and the dashboard SHALL remain in the Python process.

Two processes cannot long-poll one bot token — Telegram answers the second poller with `409 Conflict` — so this is a mutual-exclusion requirement, not a stylistic one.

#### Scenario: Gateway is the sole poller
- **WHEN** both runtimes are up
- **THEN** exactly one process is polling the bot token, and the Python app has no `Application`/updater instance

#### Scenario: Domain logic is not moved
- **WHEN** the swap is complete
- **THEN** `pipeline`, `claim_status`, `invoice_matching`, `claim_forms`, `vet_detection` and `gmail_client` are unchanged in behaviour and still owned by Python

### Requirement: The app reaches Telegram through gateway actions, not the Bot API
Unattended outbound messages — claim notifications, cards, PDF alerts, nudges — SHALL be emitted by calling the gateway's send/edit actions, and SHALL NOT call the Telegram Bot API directly.

#### Scenario: Pipeline tick notifies a blocked claim
- **WHEN** a tick finds a claim needing attention
- **THEN** the notification is sent via a gateway action, with no direct Bot API call from Python

#### Scenario: Gateway is down when a notification is due
- **WHEN** a send is attempted and the gateway is unreachable
- **THEN** the failure is recorded with a human-readable reason and surfaced, never swallowed, consistent with the project's failure-visibility rule

### Requirement: Each runtime degrades visibly and independently
Neither runtime's absence SHALL cause the other to fail silently. If the gateway is down, the pipeline SHALL continue matching, drafting and recording state, queueing what it could not deliver. If the Python app is down, the gateway SHALL report the tool surface as unavailable rather than answering from the model's own knowledge.

#### Scenario: Python app down, user asks a question
- **WHEN** the MCP server is unreachable and the user asks about a claim
- **THEN** the reply states the claims service is unavailable, and no claim facts are asserted

#### Scenario: Gateway down, tick still runs
- **WHEN** the gateway is down for a full tick interval
- **THEN** claim state still advances and the undelivered notifications remain pending rather than being marked notified

### Requirement: Scheduled work runs from gateway cron with single-fire guarantees
The 15-minute pipeline tick, the Gmail ingest job and the daily stale-action nudge SHALL be registered as gateway cron entries invoking the app. A tick SHALL NOT overlap itself, and a missed window SHALL NOT cause two ticks to run concurrently on recovery.

#### Scenario: A tick outruns its interval
- **WHEN** one tick is still running when the next is due
- **THEN** the next invocation is skipped or queued, and two `pipeline.run_once` calls never run concurrently against the same database

#### Scenario: Gateway restarts between ticks
- **WHEN** the gateway restarts
- **THEN** cron entries survive the restart without manual re-registration

### Requirement: Version stamping survives the second runtime
Every row written to `telegram_messages` SHALL continue to carry the Python app's version. The gateway's own version SHALL be recorded separately and SHALL NOT overwrite or masquerade as `app_version`.

Rationale: `app_version` exists so the message log is a usable dataset keyed to the code that produced it. Two runtimes mean two versions; conflating them makes the dataset lie.

#### Scenario: Message logged while both runtimes are current
- **WHEN** any Telegram message is recorded
- **THEN** the row carries the Python app version, and the gateway version is available without displacing it

#### Scenario: Only the gateway was redeployed
- **WHEN** the gateway is updated and the Python app is not
- **THEN** `app_version` is unchanged and the gateway version reflects the new build

### Requirement: Deploy remains a single documented command
Bringing up the system SHALL remain one documented operation covering both runtimes, and SHALL report the health of each. A partial start SHALL be reported as a failure rather than a success.

#### Scenario: Deploy with one runtime failing
- **WHEN** the Python container starts but the gateway daemon does not
- **THEN** the deploy reports failure and names which runtime is down
