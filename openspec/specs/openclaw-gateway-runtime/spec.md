# openclaw-gateway-runtime Specification

## Purpose
The OpenClaw gateway runs as a second runtime alongside the Python app. It owns
the agent session, model resolution and the plugin surface; the Python app owns
claims logic, Gmail, the database and the dashboard. This capability covers what
must hold while both runtimes run: who registers the app's commands, how large an
agent turn may be, what the agent's workspace contains, which configuration the
deploy must assert because no test can see it, how versions are stamped when
there are two of them, and that bringing the pair up stays one command.

**Scope note (2026-08-02).** Four further requirements — the gateway owning
channel transport, the app reaching Telegram through gateway actions, independent
degradation, and gateway cron — are not yet true. They are specified in
`openspec/changes/openclaw-telegram-cutover/` and become part of this capability
when that change archives. Today the Python app still holds the bot token.

Implemented across `docker-compose.yml` (two services, fixed subnet),
`scripts/deploy.ps1`, `scripts/gateway_seed.sh`, `scripts/gateway_preflight.py`,
`app/gateway-plugin/` and `app/gateway-workspace/`. See ADR-0023, ADR-0024,
ADR-0025 and `docs/gateway-deploy.md`.
## Requirements
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

#### Scenario: The proof is observable from the app side
- **WHEN** the end-to-end check runs
- **THEN** the app records a positive marker carrying an identifier the plugin minted, so a working path and an absent one cannot look alike — a check that logs only failures cannot distinguish success from silence

### Requirement: Agent turn size stays within the configured model's limits
The size of a single agent turn SHALL be measured against a declared ceiling before the system is considered deployable, and the deploy SHALL fail when it exceeds the configured model's per-request limit.

The ceiling SHALL be asserted against the platform's **itemised** report of what composes a turn, not against a single total. A total hides which component grew, and a component regressing while the total stays under budget is the failure this is meant to catch.

Measured 2026-08-01 with a fresh session key. A **stock** gateway turn is 22,810 prompt tokens for a one-word message — 31,972 chars of tool schemas, 33,774 chars of system prompt (14,341 of it injected workspace markdown), 4,206 chars of skills. The **configured** turn, with the shipped workspace files and a tool allowlist, is **5,355**. Disabling 44 of 45 plugins changes neither, because plugins are not what fills a turn.

The turn is therefore mostly content the deployment chooses, and the ceiling is met by configuration rather than by provider selection. What does not move is the core system prompt, 18,536 chars (~4.6k tokens) — the irreducible floor beneath any budget set here.

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

### Requirement: Configuration that can silently regress is asserted at deploy
The deploy SHALL assert, against the running gateway, that: the boundary plugins (`browser`, `file-transfer`, and any other granting filesystem, shell or browser reach) are disabled; `channels.telegram.dmPolicy` is `allowlist` with a non-empty `allowFrom`; `commands.ownerAllowFrom` is non-empty; the gateway holds no Gmail credential and no mount reaching `app/data`; and the media outbox directory is narrow.

Rationale: every item here was found silently wrong during the 2026-08-01 spike. Each is configuration rather than code, so no test in the app's suite can see it, and each fails without an error — an unset `dmPolicy` hands unknown senders a live pairing code; an upgrade can re-enable `browser` with no signal.

#### Scenario: A boundary plugin is re-enabled by an upgrade
- **WHEN** the gateway is upgraded and `browser` returns to the enabled set
- **THEN** the deploy fails naming the plugin, rather than starting successfully

#### Scenario: Access policy left at its default
- **WHEN** `dmPolicy` is `pairing` rather than `allowlist`
- **THEN** the deploy fails, because unknown senders would receive a pairing code and instructions to obtain approval

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

### Requirement: The in-gateway plugin forwards an inbound document without interpreting it

When a document arrives on a channel the gateway owns, the in-gateway plugin SHALL forward
the file's bytes and its original filename to the app's `/internal` surface and render
whatever the app returns. It SHALL NOT parse the file, SHALL NOT decide whether the file is
acceptable, and SHALL NOT decide who is allowed to send one — those are the app's
decisions, next to the data and the rules.

This extends the boundary the plugin's command handlers already keep. A file format
understood in two runtimes is the same defect as a claim rule understood in two runtimes:
the copies drift, and drift here means a transaction imported wrong or not at all.

A forwarded document MUST NOT reach the agent as a chat turn. An attachment that falls
through to the model costs tokens, produces a plausible-sounding answer about a file
nothing imported, and is indistinguishable to the user from the file having worked.

#### Scenario: A document arrives on an owned channel

- **WHEN** the user sends a file attachment to the bot
- **THEN** the plugin forwards its bytes and filename to the app over the shared-secret
  internal surface, and replies with the app's own answer

#### Scenario: The plugin carries no format knowledge

- **WHEN** the plugin's source is inspected
- **THEN** it contains no CSV parsing, no column layout, no transaction rules and no
  authorization decision — only detection that a file is present, the forward, and the
  rendering of the reply

#### Scenario: The app is unreachable when a document arrives

- **WHEN** the forward to the app fails
- **THEN** the user is told the file did not reach the app and why; the failure is never
  reported as an accepted upload, and never left silent

#### Scenario: A forwarded document does not become a model turn

- **WHEN** a document is forwarded and handled by the app
- **THEN** the agent is not invoked for that message, and no model tokens are spent on it

#### Scenario: The inbound path is asserted at deploy

- **WHEN** the deploy's preflight runs
- **THEN** it asserts the inbound-document path is live, on the same principle as the
  registered-command assertion — a path that silently stopped working must not be
  indistinguishable from a period in which no file was sent

