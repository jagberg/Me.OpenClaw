## ADDED Requirements

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

### Requirement: Mail is reachable only through named sweeps with stated scope
Mail-touching capability SHALL be exposed as the existing named sweeps only — `reconcile_sent_invoice_requests`, `rematch_claims`, `poll_petcover_now` — each with a fixed, non-parameterised scope. The agent SHALL NOT be able to supply a Gmail query string, label, sender, date range or result limit. When reporting a sweep the agent SHALL state that it ran that specific check, and SHALL NOT imply it read, searched or browsed the mailbox.

#### Scenario: Asked to search the mailbox
- **WHEN** the user asks the agent to look through his email for something
- **THEN** the agent states it cannot browse or search the mailbox, offers the named sweep that exists, and never implies it looked

#### Scenario: Sweep reported
- **WHEN** a sweep runs and finds nothing
- **THEN** the reply names the sweep and its scope, rather than reporting that the mailbox contains nothing

#### Scenario: Arbitrary query attempted
- **WHEN** the model attempts to pass a search expression to a sweep tool
- **THEN** the tool schema accepts no such argument and the scope is unchanged

### Requirement: Query construction stays in Python
All Gmail search construction — the merchant narrow/wide queries, the spouse fallback, and the `-from:me` and SENT-label guards — SHALL remain in `invoice_matching`. The gateway SHALL have no influence over which messages are fetched.

Rationale: these guards were derived from real mailbox failures. A query assembled anywhere else re-opens matching bugs that were closed against live data.

#### Scenario: A claim is matched
- **WHEN** the pipeline searches for a claim's invoice
- **THEN** the queries are those built by `invoice_matching`, with the existing guards applied

### Requirement: Drafts-only stays code-enforced after the swap
No code path in either runtime SHALL call Gmail's send endpoint. The guarantee SHALL be re-verified structurally once the gateway is in place, covering both the Python source and the agent's tool inventory.

#### Scenario: Structural check
- **WHEN** the codebase and agent inventory are checked for a send path
- **THEN** `send()` appears nowhere in `app/openclaw/` and no tool exposes sending

#### Scenario: User asks the agent to send a claim
- **WHEN** the user tells the agent to send the Petcover email
- **THEN** the agent refuses, states drafts-only, and points at the Gmail draft for manual sending

### Requirement: Sweeps stay idempotent under gateway redelivery
Each sweep SHALL remain safe to run more than once for the same trigger, because both the durable message log's replay and the gateway's own delivery are at-least-once. A sweep SHALL NOT create a duplicate draft, duplicate status event, or second invoice request on a repeat run.

#### Scenario: The same sweep trigger is delivered twice
- **WHEN** a sweep runs twice for one user request
- **THEN** the second run produces no duplicate draft, event or request

### Requirement: Vision OCR attempt cap is preserved
The per-email vision-OCR attempt cap SHALL remain enforced in Python. Neither the gateway's retry behaviour nor a repeated agent request SHALL cause an email to exceed it.

Rationale: vision calls are the project's most expensive LLM path and the cap (ADR-0010) is why a scanned invoice cannot silently drain the daily budget.

#### Scenario: Repeated match requests on an image-only invoice
- **WHEN** a rematch sweep is requested repeatedly for a claim whose invoice is image-only
- **THEN** vision attempts for that email stop at the cap and the claim is flagged rather than retried indefinitely
