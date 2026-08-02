## ADDED Requirements

<!--
The three requirements that need a real tap on a real button, which needs the
gateway polling. The five read-surface requirements shipped and archived with
`openclaw-gateway-core` (slice 1) and are already in `openspec/specs/`.

Splitting this capability across two slices was the deliberate cost recorded in
that change's task 8.11: holding it whole would have dragged the read tools —
exercisable through `openclaw agent` with no channel bound — into the risky day
for no reason.
-->

### Requirement: Reads commit nothing; mutations are proposals the harness gates
Read tools SHALL return current state directly. Every mutating capability SHALL be exposed only as a `propose_*` tool that records a pending action and returns a confirmation to be presented with a confirm button. The commit SHALL happen on the confirm callback path inside the Python app, never as a return value of a tool the model called.

The gate SHALL be a property of code, not of the agent's configuration or prompt. This preserves today's guarantee, which lives in `telegram_bot._execute_action` and is explicitly *not* a behaviour the model is trusted to observe.

**RESOLVED by Justin, 2026-08-01: split by origin.** A confirm tap on a **card** is a `command` button → plugin → `/internal`, committing in Python and never touching MCP. A confirm tap on a **chat-initiated** proposal commits inside the MCP server. Both satisfy the invariant that a commit is never a tool return value; the split was chosen so the chat flow stays self-contained.

**The split is smaller than "two components" suggests, and this correction matters because the decision was taken on the looser description.** The MCP server *is* the Python app's MCP surface — the same process, the same modules. So both paths commit inside Python; what differs is the entry point, `/internal` over HTTP versus an MCP confirm call. Both can and SHALL call the same commit function, which makes the divergence risk a matter of discipline in one codebase rather than of keeping two services in step.

What still follows: the harness refusals below are enforced here and not merely mirrored here, and the gate SHALL be tested on this path **independently** of the `/internal` path. A single test of "the" gate no longer covers the system, because two entry points can reach the commit and only one of them is exercised by a card tap.

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

### Requirement: Claim identity is always available to a tool
Every mutating tool SHALL accept an explicit claim id, and the current claim resolved from a replied-to card SHALL be supplied to the turn. No tool SHALL exist that can only be targeted by pet, reference, or merchant.

Rationale: with no way to name the claim under discussion, the model fabricated argument values from the schema's own description strings (live, 2026-07-27).

#### Scenario: Reply to a claim card
- **WHEN** the user replies to a card naming claim #N and requests an action with no other target
- **THEN** the tool is called with id N

#### Scenario: No id resolvable
- **WHEN** neither the message nor the replied-to message names a claim and the request is ambiguous
- **THEN** the agent asks for clarification and commits nothing
