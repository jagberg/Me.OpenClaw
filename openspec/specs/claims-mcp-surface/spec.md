# claims-mcp-surface Specification

## Purpose
The claims domain reaches the gateway's agent as an MCP server with an explicit,
enumerated tool inventory — not as prompt text describing what the agent may do.
A capability absent from the inventory is unreachable, which is the difference
between a boundary and a instruction. This capability covers the read surface:
what the inventory is, the token budget that bounds it, that deterministic paths
never route through it, that outstanding work has one derivation, and that
nothing in it returns a secret.

**Scope note (2026-08-02).** Three further requirements — the proposal gate, the
harness-enforced refusals it carries, and mutating-tool claim identity — need a
real tap on a real button, which needs the gateway polling. They are specified in
`openspec/changes/openclaw-telegram-cutover/`. Until then the inventory is
read-only: `mcp_server.py` selects its implementations by name from `TOOL_NAMES`,
so a `propose_*` tool existing in `agent.py` is not reachable through MCP.

Implemented in `app/openclaw/mcp_server.py` (JSON-RPC over `POST /mcp`), reached
by the gateway through `mcp.servers.claims`. See ADR-0023 for the allowlist and
its token measurements.

## Requirements

### Requirement: MCP serves conversation only; deterministic paths never use it
The MCP inventory SHALL be reachable only from the conversational agent. A button tap, a slash command, or an unattended pipeline notification SHALL NOT pass through MCP, a tool call, or a model.

Decided by Justin, 2026-08-01: no MCP for deterministic calls. A tap is a `command` action → plugin-registered command → `/internal` → existing claim logic. Beyond correctness, this is a cost control: every tool schema is transmitted on every chat turn, so a deterministic path routed through MCP would be charged the whole inventory for work that needs no model.

#### Scenario: A tap never reaches MCP
- **WHEN** the user taps a claim card button
- **THEN** the command path handles it end to end, and no MCP tool is invoked and no model runs

#### Scenario: Chat reaches the domain through MCP
- **WHEN** the user asks a question in free text
- **THEN** the agent answers using the MCP read tools

### Requirement: The claims domain is exposed as a bounded MCP tool inventory
The Python app SHALL expose its capabilities to the gateway agent as an MCP server with an explicit, enumerated tool inventory. A capability absent from the inventory SHALL be unreachable by the agent, not merely discouraged by prompt text.

Rationale: today's boundaries (ADR-0016) are held by a tool registry in `agent.py` plus prompt discipline. Once a general-purpose agent runtime holds the loop, prompt discipline is no longer a boundary — the inventory is.

#### Scenario: A capability with no tool
- **WHEN** the user asks for an operation the inventory does not implement
- **THEN** the agent cannot perform it, and no adjacent tool is used as a substitute

#### Scenario: Inventory is enumerable
- **WHEN** the tool surface is inspected
- **THEN** every tool the agent can call is listed, with no wildcard or dynamically-registered addition

### Requirement: The tool inventory has a declared budget
The number of tools SHALL be bounded by a declared maximum, and exceeding it SHALL fail the test suite rather than silently increasing the cost of every conversational turn.

The maximum SHALL be derived from the configured provider's per-request limit less the measured cost of everything else in a turn, not chosen by judgement.

Rationale: tool schemas ship on every request, so inventory size is a recurring per-turn cost. Measured 2026-08-01: a trimmed turn with no claims tools is 5,355 tokens against Groq free tier's 12,000 per minute, leaving roughly 6,600 tokens for the entire claims inventory. That is the budget. For scale, the platform's own stock inventory of 32 tools was 31,972 chars — about five times the headroom available — so the constraint is real and a plausible-looking inventory can breach it.

#### Scenario: A tool is added beyond the budget
- **WHEN** a new tool takes the inventory past its declared maximum
- **THEN** the suite fails, naming the budget and the current count

### Requirement: Outstanding work has exactly one derivation
Any tool answering what is outstanding, blocked, or waiting SHALL call the shared `claim_status.pending_actions()` derivation. The inventory SHALL NOT contain a second tool from which the agent could assemble its own list.

#### Scenario: Asked what is outstanding
- **WHEN** the agent answers what is waiting on the user
- **THEN** the entries come from the shared derivation, and agree with the `/actions` cards item for item

### Requirement: Secrets and implementation internals are outside the inventory
The inventory SHALL contain no tool returning `.env` contents, API keys, bank credentials, database files, or source code. Claim-level "why" SHALL be answered by the claim-detail tool; questions about the implementation SHALL be declined for want of a tool.

#### Scenario: Asked for secrets
- **WHEN** the user asks for API keys, bank details or `.env` contents
- **THEN** no tool can return them and the agent declines
