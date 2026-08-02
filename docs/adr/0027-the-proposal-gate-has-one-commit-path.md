# ADR 0027: The proposal gate has one commit path, reached from two entry points

- Status: accepted
- Date: 2026-08-02
- Deciders: Justin
- Supersedes the *gate location* half of ADR-0025. Everything else in 0025
  stands: code-composed confirmation text, harness-enforced refusals, and the
  invariant that a commit is never a tool return value.

## Context

ADR-0025 split the gate by origin — a card tap commits behind `/internal`, a
chat-initiated proposal commits inside the MCP surface. Justin chose that over
the recommended single Python commit path, so the chat flow would stay
self-contained.

**The MCP half has no mechanism.** For an MCP server to gate its own commit it
must ask the client for a decision mid-call, which MCP calls *elicitation*. The
gateway does not offer it. Read from its shipped code on 2026-08-02, three
agreeing points, recorded in full in ADR-0025's amendment of that date:

- the client is constructed with no `capabilities` in its options;
- the bundled SDK therefore sends `capabilities: {}` on `initialize`, and its
  `assertRequestHandlerCapability` throws *"Client does not support elicitation
  capability"* for any such handler;
- the string `elicit` appears nowhere in the gateway's agent MCP runtime.

The gateway *does* have a full approval system, and ADR-0025's Context read it
correctly: a **plugin** approval takes free-text `title` and `description`. The
error was carrying that one surface across. Approvals are a plugin capability;
the claims MCP server is not a plugin.

So the option chosen did not exist, and the option declined was the only one
left. That made the outcome forced but not the *decision* — the reason for
choosing (a self-contained chat flow) is what gets given up, and Justin was
asked rather than told. He confirmed on 2026-08-02: proceed with one commit
path.

## Decision

**One commit function, two entry points.**

`proposals.commit` is the only code that applies a proposed mutation. It is
reachable from a confirm tap and from nothing else:

- a **card** tap → `command` button → plugin → `/internal` → `commit`;
- a **chat** proposal → `propose_*` writes a `pending_proposals` row and sends a
  card carrying `/confirm <id>` → the same plugin → `/internal` → `commit`.

`proposals.execute` holds the action switch, moved verbatim out of
`telegram_bot._execute_action`, which now delegates to it. That module is deleted
at the cutover and its behaviour must not go with it.

A proposal is **durable**. The MCP call and the tap are separate requests in
separate runtimes, so `telegram_bot`'s in-process `_pending_actions` dict cannot
span the gap; a restart in between would silently turn a proposal into a tap
that does nothing. `confirmed_at` makes a tap single-use, because Telegram
redelivers and a double mark-sent is a second Petcover submission for one set of
invoices.

## Alternatives Considered

### Alternative 1: raise the confirmation as a plugin approval
- **Pros**: keeps ADR-0025's intent — the chat flow stays self-contained, and
  the gateway renders a native approval rather than another card.
- **Cons**: the approval must be raised by the plugin, while the proposal is
  built in the app, so it needs a callback from Python into the gateway
  (`createOperatorApprovalsGatewayClient`) — a third inter-runtime path, none of
  it exercised here, on top of a feature this project has never used.
- **Why not**: it buys presentation. The gate's substance is that the text is
  composed by code and the commit is not a tool return value, and a card with a
  command button already delivers both using mechanisms slice 1 proved live.
  Worth revisiting if the two-message flow annoys in practice.

### Alternative 2: let the MCP tool commit, and rely on the prompt to confirm first
- **Pros**: no store, no button, no second message.
- **Cons**: the gate becomes a behaviour the model is trusted to observe, which
  ADR-0016 named as not a gate at all, and 2026-07-27 demonstrated on live data.
- **Why not**: it is the failure this whole line of work exists to prevent.

### Alternative 3: keep proposals in memory, keyed by session
- **Pros**: no schema, no migration.
- **Cons**: a gateway or app restart between the proposal and the tap makes the
  button silently inert — the worst shape available, because the tap looks like
  it worked.
- **Why not**: "a morning of taps changed nothing and left no evidence of why"
  is already in this repo's history (ADR-0014).

## Consequences

### Positive
- One writer for confirmed mutations, guarded mechanically: the suite asserts no
  MCP implementation reaches `commit` or `execute`.
- The card path and the chat path cannot drift, because there is nothing to keep
  in step.
- A proposal survives a restart, and its whole lifecycle is on disk — which is
  the audit trail for "did my tap register?" that taps have historically lacked.

### Negative
- **A chat proposal now costs two messages**: the agent's reply and the Confirm
  card. ADR-0025's split existed partly to avoid exactly this.
- `confirm` is a sixth `BUTTON_COMMANDS` entry and a sixth plugin command. An
  unregistered one is not an error — it reaches the agent as a chat turn and
  spends tokens — so the equality is asserted in the suite and at deploy.
- `pending_proposals` accumulates. Nothing prunes it yet; it is small and the
  history is worth having, but it is a row per proposal forever.

### Risks
- **The Confirm card can fail to send while the proposal is recorded.** Then a
  pending row exists with no way to confirm it. Handled loudly rather than
  silently: the tool's own return text tells the model the button did not
  arrive, and the failure is logged at ERROR. It is still two states where there
  was one.
- **`latest_inbound_text()` is a heuristic.** The two-pets refusal reads the most
  recent inbound text row rather than the message that actually triggered the
  turn. With one user in one chat these coincide; under concurrent messages they
  need not. Taking the text from a tool argument was the alternative, and it is
  worse — a model that paraphrased the message would paraphrase the second pet
  name away and take the refusal with it. Revisit if the gateway ever supplies
  the triggering message to the tool call.
- **Not verified live.** Neither the elicitation refusal that forced this nor the
  confirm round trip has been exercised against the running gateway: the first
  needs an agent turn and Groq's daily budget was exhausted the same day, the
  second needs the plugin's `/internal/command/confirm` route, which section 4
  builds. Both are hermetically tested and neither is proven end to end.
