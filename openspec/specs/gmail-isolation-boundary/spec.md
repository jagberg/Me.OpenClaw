# gmail-isolation-boundary Specification

## Purpose
The Gmail credential holds `gmail.compose` and is therefore *capable* of sending.
"Never send email" is enforced only by the absence of `send()` in the code that
holds that credential — a code-shaped guarantee, which holds exactly as long as
that code is the only code able to reach the mailbox. Introducing a second runtime
that offers file, shell and browser tools in principle is the one part of the
gateway swap that can regress a hard rule, which is why this is a capability
rather than a design note.

**Amendment (2026-08-11, ADR-0030).** "The absence of `send()`" is no longer
absolute: `invoice_matching.send_invoice_request` is one named, narrow exception
(vet invoice-request emails only, Justin's explicit override), guarded by an
automated test (`app/tests/test_core.py`,
`test_nothing_but_the_one_named_exception_can_send_mail_and_no_tool_offers_to`)
that asserts every *other* file in the package still has zero Gmail send call
sites. The requirements below — credential isolation, no agent tool reach, query
construction in Python — are unaffected; only this rationale paragraph's
absoluteness needed correcting.

This capability covers the boundary as it stands with both runtimes running: the
gateway holds no Gmail credential and no Gmail-scoped Google key, the agent holds
no tool that could reach the credential store or a webmail origin, and every
Gmail query is still constructed in `invoice_matching`.

**Scope note (2026-08-02).** Four further requirements — mail reachable only
through named sweeps, drafts-only re-verified structurally after the swap, sweeps
idempotent under gateway redelivery, and the vision-OCR cap holding against the
gateway's retries — describe pressure sources that do not exist yet: the agent has
seven read tools and no sweeps. They are specified in
`openspec/changes/openclaw-telegram-cutover/`.

Asserted at deploy by `scripts/gateway_preflight.py` (`check_gmail_isolation`,
`check_boundary_plugins_disabled`, `check_media_outbox_narrow`) against the
*running* gateway, because every item here is configuration and fails silently.

## Requirements

### Requirement: Gmail credentials never leave the Python process
The Gmail OAuth token, client secrets and every Gmail API call SHALL live exclusively in the Python process. The gateway SHALL hold no Gmail credential of its own, and SHALL NOT be configured with any Google provider key that carries a Gmail scope.

Rationale, and why this is the gating concern of the whole change: the existing token holds `gmail.compose` and is therefore *capable* of sending. "Never send email" is enforced today only by the absence of `send()` in `app/openclaw/`. That guarantee is code-shaped, so it holds only while the code with the credential is the only code that can reach it.

#### Scenario: Gateway configuration inspected
- **WHEN** the gateway's configuration and secrets are inspected
- **THEN** no Gmail token, no OAuth client secret and no Gmail-scoped credential is present

#### Scenario: Token refresh
- **WHEN** the OAuth token expires and is re-authorized
- **THEN** the flow runs against the Python app's credential store only, and the gateway is unaffected

### Requirement: No agent tool can reach the credential store
The agent SHALL NOT be granted any tool that can read or write `app/data/`, execute shell commands, or browse to a webmail origin. The absence of these tools SHALL be a configured property of the agent, verifiable by inspecting its inventory.

Rationale: the gateway runtime offers file, shell and browser tools in principle. Any one of them turns a walled-off credential into a reachable one, and a browser reaching a logged-in mail session bypasses the token question entirely.

#### Scenario: Agent inventory inspected
- **WHEN** the agent's tool list is enumerated
- **THEN** it contains no filesystem read/write, no shell/Bash, and no browser tool

#### Scenario: Agent asked to read a file
- **WHEN** the user asks the agent to open the database, the token file or a source file
- **THEN** no tool exists to do it and the agent says so

#### Scenario: Agent asked to open webmail
- **WHEN** the user asks the agent to log into Gmail and look
- **THEN** it has no browser tool and cannot reach any mail origin

### Requirement: Query construction stays in Python
All Gmail search construction — the merchant narrow/wide queries, the spouse fallback, and the `-from:me` and SENT-label guards — SHALL remain in `invoice_matching`. The gateway SHALL have no influence over which messages are fetched.

Rationale: these guards were derived from real mailbox failures. A query assembled anywhere else re-opens matching bugs that were closed against live data.

#### Scenario: A claim is matched
- **WHEN** the pipeline searches for a claim's invoice
- **THEN** the queries are those built by `invoice_matching`, with the existing guards applied
