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

### Requirement: An in-gateway plugin owns the app's command surface
A plugin running inside the gateway SHALL register the app's slash commands (`/mark`, `/pet`, `/resolve` and the rest) via the plugin API, and its command handlers SHALL do no claims work themselves — each forwards to the app's `/internal` endpoints.

Rationale: a button carrying `action.type: "command"` invokes a **native** slash command through core's command path. The app's commands are not native to the gateway, so something must register them; that is the plugin's job and the reason it exists. It does not own outbound rendering.

#### Scenario: A button tap runs a claim command
- **WHEN** the user taps a button whose action is the command `/mark 7 sent`
- **THEN** the plugin's registered handler runs, calls `/internal`, and the existing claim logic applies the change — with no model involved at any point

#### Scenario: The plugin carries no domain logic
- **WHEN** the plugin's source is inspected
- **THEN** it contains no claim rules, no status transitions and no Gmail access — only registration and forwarding

#### Scenario: Registration is proven, not assumed
- **WHEN** the deploy completes
- **THEN** a registered command is invoked end to end and must respond; the plugin listing's own report of its commands SHALL NOT be accepted as evidence, because that data comes from a persisted registry that goes stale silently

### Requirement: Agent turn size stays within the configured model's limits
The size of a single agent turn SHALL be measured against a declared ceiling before the system is considered deployable, and the deploy SHALL fail when it exceeds the configured model's per-request limit.

The ceiling SHALL be asserted against the platform's **itemised** report of what composes a turn, not against a single total. A total hides which component grew, and a component regressing while the total stays under budget is the failure this is meant to catch.

Measured 2026-08-01 on a stock gateway with a fresh session key: 22,810 prompt tokens for a one-word message, composed of 31,972 chars of tool schemas, 33,774 chars of system prompt (14,341 of it injected workspace markdown files), and 4,206 chars of skills. Disabling 44 of 45 plugins does not reduce it, because plugins are not what fills the turn. The turn is therefore mostly content the deployment chooses — a tool allowlist, the workspace files it ships, and the skills it enables — and a provider whose per-minute limit sits below the result cannot serve the agent at all.

#### Scenario: Turn exceeds the model's limit
- **WHEN** a measured turn is larger than the configured model's per-request or per-minute ceiling
- **THEN** the deploy fails and names both numbers, rather than deferring the failure to the first real message

#### Scenario: One component regresses
- **WHEN** the tool schemas, injected workspace files or enabled skills grow beyond their declared shares
- **THEN** the deploy fails naming that component, even if the overall total is still within budget

#### Scenario: Measurement uses a clean session
- **WHEN** turn size is measured
- **THEN** a fresh session key is used, because an existing session's accumulated history is counted in the request and produces a reading that measures conversation rather than surface

### Requirement: The agent's workspace files are shipped, versioned, and carry no enforcement
The markdown files the gateway injects into every turn — `IDENTITY.md`, `USER.md`, `SOUL.md`, `AGENTS.md` and the rest — SHALL be authored in the repository, deployed into the agent workspace, and versioned with the application. `BOOTSTRAP.md` SHALL be absent and automatic re-seeding SHALL be disabled, so an upgrade cannot restore the template versions.

These files are prompt content. No guarantee that must hold SHALL depend on them: the harness refusals, the proposal gate and the no-send rule live in code. Their content is limited to matters whose worst failure is awkwardness — tone, how the user is addressed, the `#id` convention, and supplied context.

Rationale: left with the seeded templates, the stock agent opened by interviewing the user about its own name, species and "vibe" across three consecutive messages before it would answer anything, and in the same conversation asserted it had checked email in a runtime holding no mail credential. The first is why the files must be shipped complete; the second is why nothing enforceable may be written into them.

#### Scenario: A fresh workspace is deployed
- **WHEN** the gateway starts with an empty agent workspace
- **THEN** the shipped files are present, no bootstrap interview occurs, and the first message is answered on its merits

#### Scenario: An upgrade re-seeds the templates
- **WHEN** a platform upgrade would rewrite the workspace files
- **THEN** re-seeding is disabled and the shipped versions survive, or the deploy fails

### Requirement: The app reaches Telegram through gateway actions, not the Bot API
Unattended outbound messages — claim notifications, cards, PDF alerts, nudges — SHALL be emitted by calling the gateway's send/edit actions, and SHALL NOT call the Telegram Bot API directly.

#### Scenario: Pipeline tick notifies a blocked claim
- **WHEN** a tick finds a claim needing attention
- **THEN** the notification is sent via a gateway action, with no direct Bot API call from Python

#### Scenario: Gateway is down when a notification is due
- **WHEN** a send is attempted and the gateway is unreachable
- **THEN** the failure is recorded with a human-readable reason and surfaced, never swallowed, consistent with the project's failure-visibility rule

### Requirement: Configuration that can silently regress is asserted at deploy
The deploy SHALL assert, against the running gateway, that: the boundary plugins (`browser`, `file-transfer`, and any other granting filesystem, shell or browser reach) are disabled; `channels.telegram.dmPolicy` is `allowlist` with a non-empty `allowFrom`; `commands.ownerAllowFrom` is non-empty; the gateway holds no Gmail credential and no mount reaching `app/data`; and the media outbox directory is narrow.

Rationale: every item here was found silently wrong during the 2026-08-01 spike. Each is configuration rather than code, so no test in the app's suite can see it, and each fails without an error — an unset `dmPolicy` hands unknown senders a live pairing code; an upgrade can re-enable `browser` with no signal.

#### Scenario: A boundary plugin is re-enabled by an upgrade
- **WHEN** the gateway is upgraded and `browser` returns to the enabled set
- **THEN** the deploy fails naming the plugin, rather than starting successfully

#### Scenario: Access policy left at its default
- **WHEN** `dmPolicy` is `pairing` rather than `allowlist`
- **THEN** the deploy fails, because unknown senders would receive a pairing code and instructions to obtain approval

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
