## ADDED Requirements

### Requirement: The claims domain is exposed as a bounded MCP tool inventory
The Python app SHALL expose its capabilities to the gateway agent as an MCP server with an explicit, enumerated tool inventory. A capability absent from the inventory SHALL be unreachable by the agent, not merely discouraged by prompt text.

Rationale: today's boundaries (ADR-0016) are held by a tool registry in `agent.py` plus prompt discipline. Once a general-purpose agent runtime holds the loop, prompt discipline is no longer a boundary — the inventory is.

#### Scenario: A capability with no tool
- **WHEN** the user asks for an operation the inventory does not implement
- **THEN** the agent cannot perform it, and no adjacent tool is used as a substitute

#### Scenario: Inventory is enumerable
- **WHEN** the tool surface is inspected
- **THEN** every tool the agent can call is listed, with no wildcard or dynamically-registered addition

### Requirement: Reads commit nothing; mutations are proposals the harness gates
Read tools SHALL return current state directly. Every mutating capability SHALL be exposed only as a `propose_*` tool that records a pending action and returns a confirmation to be presented with a confirm button. The commit SHALL happen on the confirm callback path inside the Python app, never as a return value of a tool the model called.

The gate SHALL be a property of the MCP server, not of the agent's configuration or prompt. This preserves today's guarantee, which lives in `telegram_bot._execute_action` and is explicitly *not* a behaviour the model is trusted to observe.

#### Scenario: Mutation requested
- **WHEN** the agent calls a `propose_*` tool
- **THEN** a pending action is recorded, a confirmation naming the claim and action is returned, and no claim data has changed

#### Scenario: Confirmation never tapped
- **WHEN** a proposal is presented and no confirm callback arrives
- **THEN** nothing is committed

#### Scenario: Model asserts completion prematurely
- **WHEN** the model's reply describes a proposed mutation as done
- **THEN** the data is nonetheless unchanged, because the commit path was never entered

### Requirement: Harness-enforced refusals survive the move
Refusals currently enforced by the harness rather than the prompt SHALL be enforced inside the MCP server. Specifically: a message naming more than one pet on file SHALL NOT yield a single-pet assignment proposal, and a per-item condition split SHALL be refused when line items carry no amounts rather than filling $0 rows.

#### Scenario: Two pets named
- **WHEN** the user's message names two pets on file and the model calls the single-pet assignment proposal
- **THEN** the server refuses, queues nothing, and returns direction to propose a per-pet split with each share

#### Scenario: Split with no per-item amounts
- **WHEN** a per-item condition split is proposed for an invoice whose items have no extracted amounts
- **THEN** the server refuses with an explanation and no $0 rows are produced

### Requirement: Outstanding work has exactly one derivation
Any tool answering what is outstanding, blocked, or waiting SHALL call the shared `claim_status.pending_actions()` derivation. The inventory SHALL NOT contain a second tool from which the agent could assemble its own list.

#### Scenario: Asked what is outstanding
- **WHEN** the agent answers what is waiting on the user
- **THEN** the entries come from the shared derivation, and agree with the `/actions` cards item for item

### Requirement: Claim identity is always available to a tool
Every mutating tool SHALL accept an explicit claim id, and the current claim resolved from a replied-to card SHALL be supplied to the turn. No tool SHALL exist that can only be targeted by pet, reference, or merchant.

Rationale: with no way to name the claim under discussion, the model fabricated argument values from the schema's own description strings (live, 2026-07-27).

#### Scenario: Reply to a claim card
- **WHEN** the user replies to a card naming claim #N and requests an action with no other target
- **THEN** the tool is called with id N

#### Scenario: No id resolvable
- **WHEN** neither the message nor the replied-to message names a claim and the request is ambiguous
- **THEN** the agent asks for clarification and commits nothing

### Requirement: Secrets and implementation internals are outside the inventory
The inventory SHALL contain no tool returning `.env` contents, API keys, bank credentials, database files, or source code. Claim-level "why" SHALL be answered by the claim-detail tool; questions about the implementation SHALL be declined for want of a tool.

#### Scenario: Asked for secrets
- **WHEN** the user asks for API keys, bank details or `.env` contents
- **THEN** no tool can return them and the agent declines
