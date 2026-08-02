# ADR 0025: The proposal gate is split by origin, and its text is composed by code

- Status: accepted
- Date: 2026-08-01
- Supersedes the gate's location in ADR-0016 (`telegram_bot._execute_action`)
- Related: ADR-0024 (two runtimes), ADR-0023 (tool inventory)

## Context

Every claim mutation is a proposal that commits only when Justin taps confirm.
That guarantee currently lives in `telegram_bot._execute_action` and is
explicitly *not* a behaviour the model is trusted to observe — ADR-0016 made the
point that a gate in a prompt is not a gate.

Moving the chat loop onto a general-purpose agent runtime forces the question of
where the gate goes, because `telegram_bot.py` is being deleted.

Two things were learned while deciding, both of which changed the shape of the
question:

**Deterministic paths do not use MCP** (Justin, 2026-08-01). A confirm tap on a
card is a `command` button → plugin → `/internal` → Python. It never touches
MCP, so a gate located "in the MCP server" would not be on that path at all.

**The native approval prompt can render a full outcome.** Reading
`approval-view-model`: an exec approval is a fixed template, but a *plugin*
approval takes `title` and `description` as free text from whoever raises it.
So it can display `assign Aari to claim #7, Echo gets $28` rather than a bare
tool name. This was the capture Justin asked for before deciding, and it
removed native-versus-bespoke as the deciding axis.

## Decision

**The gate is split by origin.**

- A confirm tap on a **card** commits behind `/internal`, on the same path as
  any other button.
- A confirm tap on a **chat-initiated** proposal commits in the MCP surface.

Justin chose the split over a single Python commit path, which was the
recommendation. Both satisfy the invariant that matters — a commit is never the
return value of a tool the model called — which is why this was a decision and
not a deduction.

**The confirmation text is composed by code from the claim record about to
change** — id, pet, merchant, date, amount, read back from the row. Never from
the model's description of what it intends. This applies identically to both
paths and is the substantive safeguard.

**Harness refusals move with the commit.** A message naming more than one pet on
file never yields a single-pet assignment; a per-item condition split with no
extracted amounts is refused rather than filling `$0` rows. Both are enforced in
the MCP surface, not mirrored there, because that surface now commits.

## Consequences

**Two entry points reach the commit, not two components.** This distinction was
got wrong when the choice was put to Justin, and the correction is recorded
because he decided on the looser description. The MCP surface *is* the Python
app's — same process, same modules — so both paths commit inside Python and can
call the same commit function. The risk is drift within one codebase, not
keeping two services in step. That makes the chosen option safer than it was
presented, so the decision stands unchanged.

**The gate must be tested per entry point.** A single test of "the" gate no
longer covers the system: a card tap exercises only one of the two.

**Free-text approval prompts move the risk rather than removing it.** If the
model composed the description, a model that resolved the wrong claim would
describe the wrong claim convincingly — approval becomes a rubber stamp with
better typography. Code-composed text is what prevents that, and it is the one
requirement here that cannot be relaxed.

**Prompt-level enforcement is weaker than before, not stronger.** ADR-0016's
boundaries were held by a tool registry plus prompt discipline. On a
general-purpose runtime the prompt is not a boundary at all, which is why the
refusals are code and the inventory is enumerated (ADR-0023).

**Recorded limitation.** The two-pets refusal keys on pet names appearing in the
message, so a phrasing that names two pets while meaning one ("that one is
Aari's, not Echo's") is refused too. The refusal explains itself and Justin can
restate. A wrong claim cannot be restated once sent, so the asymmetry is
deliberate.

**Evidence this needed to be code.** Replaying *"This is actually split between
echo and Aari. Aari cost was $35 out of this"* against the ASSIGN PET card on
2026-07-27 — no API error, the split tool present in the schema — the model
proposed assigning Aari *and* Echo. The prompt rule lost on live data.

## Amendment (2026-08-02) — the MCP half of the split has no mechanism, and the decision needs re-taking

**The Decision above says a chat-initiated confirm "commits in the MCP surface".
Nothing in the gateway can deliver that.** Found while starting section 3 of
`openclaw-telegram-cutover`, by reading the product's shipped code rather than
its prose — the habit this repo adopted on 2026-08-01 after five wrong
architectural conclusions in one session.

For an MCP server to gate its own commit behind a human tap, it must ask the
client a question mid-call. MCP's name for that is **elicitation**. The gateway
does not offer it:

- `dist/agent-bundle-mcp-runtime-*.js` constructs `new Client({name:
  "openclaw-bundle-mcp", version: "0.0.0"}, {jsonSchemaValidator, listChanged})`
  — **no `capabilities` in the options object.**
- The bundled SDK reads `this._capabilities = options?.capabilities ?? {}`
  (`@modelcontextprotocol/sdk/dist/esm/client/index.js:107`) and sends exactly
  that on `initialize` (`:297`). So the client declares `{}`.
- The same SDK refuses to register a handler for a capability it did not
  declare: `assertRequestHandlerCapability` throws *"Client does not support
  elicitation capability"* (`:421-425`).
- The string `elicit` does not appear anywhere in the gateway's agent MCP
  runtime (`grep -c` = 0).

The gateway *does* have a rich approval system — `plugin-sdk/approval-runtime.js`
exports `buildPluginApprovalRequestMessage`, `resolveApprovalApprovers` and the
timeout constants — which is what the Context above was reading when it recorded
that "a *plugin* approval takes `title` and `description` as free text". That
reading was correct and remains correct. The error was carrying it across to the
MCP surface: **approvals are a plugin capability, and the claims MCP server is
not a plugin.**

**Not verified live.** This is read off the shipped client, the bundled SDK and
their absence of any elicitation code — three agreeing points, but not an
attempted elicitation that came back refused. The live attempt needs a real
agent turn, and Groq's daily budget was exhausted the same day (`Limit 100000,
Used 96708`). Worth doing before the cutover; it would only confirm.

### What this does to the decision

Justin chose split-by-origin over a **single Python commit path**, which was the
recommendation. The option he chose does not exist. The option he declined is
now the only feasible one, so the outcome is forced — but the reasoning behind
his choice ("the chat flow stays self-contained") is exactly what is lost, and
that is his call to accept, not an implementation detail to absorb quietly.

The feasible shape, using only mechanisms slice 1 already proved live:

- A chat proposal returns text plus a **confirm button**, built by
  `gateway_client.build_buttons` and validated against the platform's own
  normalizer.
- The tap is a `command` button → plugin → `/internal` → the same commit
  function a card tap reaches.
- The invariant that mattered is untouched: **a commit is still never the return
  value of a tool the model called.** So is the substantive safeguard — the
  confirmation text is composed by code from the claim row about to change.

What is genuinely lost is the "two entry points" property, and with it the
consequence recorded above that *"the gate SHALL be tested on this path
independently"*. One commit path is easier to hold correct, which was the
argument for the rejected option in the first place.

Two costs this creates, neither of them blocking:

- A confirm button must carry the proposal's identity inside **58 UTF-8 bytes**,
  and `confirm` becomes a sixth entry in `BUTTON_COMMANDS` and in the plugin's
  `COMMANDS` — both now asserted equal by
  `test_the_plugin_registers_exactly_the_commands_a_button_may_emit`.
- A proposal must survive between the MCP call and the tap, which are separate
  requests in separate runtimes. That needs a durable store, not the per-turn
  `proposals` list `telegram_bot` passes around today.

**Status: this ADR's Decision is superseded in part and awaits Justin.** Nothing
in section 3 that depends on where the commit lands has been built. The parts
that do not depend on it — the `propose_*` refusals, the claim-id requirement,
and the proof that a proposal changes nothing — are unaffected either way.
