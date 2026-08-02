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
