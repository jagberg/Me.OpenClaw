## ADDED Requirements

<!--
Four of this capability's seven requirements. Each describes a system that does
not exist until the gateway drives the conversation: mail-touching sweeps in the
agent's inventory, the pressure sources that could breach the vision cap, and
the structural re-verification whose test (task 10.3) is still open. The other
three shipped and archived with `openclaw-gateway-core` (slice 1).

One of these four is a correction to a correction, recorded rather than quietly
applied. Task 8.11's eval fix assigned THREE requirements here; "Vision OCR
attempt cap is preserved" is the fourth. Its only scenario is a rematch sweep
requested repeatedly, and no sweep tool exists in slice 1's seven-tool read
inventory — nor does gateway cron drive anything yet. Both pressure sources the
requirement names are slice-2 constructs, so by 8.11's own rule the requirement
cannot be slice 1. The cap itself (ADR-0010) is enforced in Python today and is
unaffected either way; what moves is the assertion that it survives the new
pressure, which cannot be tested until that pressure exists.
-->

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
