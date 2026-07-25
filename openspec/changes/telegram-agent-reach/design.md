## Context

`agent.py` is OpenClaw's only *pull* interface. Everything else pushes: the pipeline notifies, the dashboard displays, Telegram cards offer taps. The agent is the one place Justin can ask an arbitrary question — and it is the narrowest surface in the system: 8 tools, no dates, no mailbox reach, no assistant side.

Widening it means deciding *how far*, and four of those calls are non-obvious enough to record.

## Decision 1 — the mailbox rule is narrowed, not deleted

The current prompt says, absolutely:

> You CANNOT read Justin's mailbox, search his email, or see what he has sent. […] Never imply you looked at his email.

That rule is not defensive boilerplate. It was added because the agent **fabricated the action** — told Justin it had checked his sent mail when it had no such capability. For a single-user assistant that is the worst available failure: an answer he cannot distinguish from a real one.

This change gives the agent two named sweeps over mail, so the rule as written becomes false. The temptation is to delete it. **We narrow it instead**, to the shape that is actually true:

- cannot browse, search, or read arbitrary mail — unchanged, still absolute;
- *can* run two named sweeps (`rematch_claims`, `poll_petcover_now`) and report what they found;
- must present these as specific checks, never as "I looked through your email".

Recorded as a deliberate change of mind rather than an edit, because the original reasoning stays valid — the capability moved, the risk did not. A regression test asserts the narrowed rule is present so it can't quietly drift back.

**Trade-off accepted**: a prompt rule with a carve-out is weaker than an absolute one, and the model may still over-claim at the margin. Mitigated by the sweeps' reply text stating their own scope ("checked N pending claims for <vet>"), so the *evidence* in the answer is scoped even if the prose overreaches.

## Decision 2 — sweeps act directly; they are not proposal-gated

Every other mutation the agent can reach is a proposal awaiting a Confirm tap. These two are not, for three reasons:

1. **The pipeline already does exactly this, unattended, every 15 minutes.** `match_claim` and `poll_petcover_status` are the tick's own calls. Gating on-demand invocation while the scheduler runs it unsupervised would be theatre.
2. **Neither can send anything.** Gmail access is read + drafts only, structurally (hard rule).
3. **A bad match is reversible** through the existing `unmatch` / ❌ Wrong invoice path. A bad *proposal* gate, by contrast, would make the tool useless for its actual purpose — "go through the vet's emails" is a sweep over several claims, and one Confirm tap per claim is not an answer.

### Replay safety (this is the part that could bite)

ADR-0014 made update handling **at-least-once**: `message_log.replay_pending` re-runs any update whose handler didn't finish. So a chat turn that triggers a sweep can execute that sweep **twice**.

Both are idempotent, and that is *why* direct action is acceptable rather than a happy accident:

- `rematch_claims` only ever considers `status = 'pending_match'` claims. A claim the first run matched is no longer in that set.
- `poll_petcover_now` skips anything `gmail_ingest._already_processed` marked, and status events are append-only with their own dedupe.

**This is asserted, not assumed**: a test runs each sweep twice and requires the second to be a no-op. If either turns out not to be idempotent, that tool becomes proposal-gated instead — the test is the gate on the decision, not decoration.

## Decision 3 — no force-reprocess of already-seen Petcover mail

"Go through the Petcover emails" could mean "re-read ones you've already read". Rejected.

Replaying a seen email against the append-only event log risks re-applying a status transition — and status is the thing this whole service exists to track correctly. The `_already_processed` guard is load-bearing, not incidental.

What Justin actually wants from that phrasing is served two other ways: `poll_petcover_now` catches anything genuinely unprocessed (including mail that arrived while something was broken), and `claim_detail` shows what was already recorded. The sweep's reply states this limit explicitly, so "nothing new" is never mistaken for "nothing there".

**Trade-off accepted**: if a reply was mis-parsed at ingest time, chat can't force a re-read. That's a deliberate manual/dev operation, not something the agent should reach.

## Decision 4 — no code, docs, or spec reading in the container

Justin asked to be able to "ask about the system". Scoped explicitly to **claim-level why**: flags, status events, and the recorded settlement figures — which is what `claim_detail` delivers.

Not extended to reading the repo. Two reasons: a 70B free-tier model reading Python will explain it confidently and wrongly, which is worse than declining; and mounting the repo puts secrets-adjacent paths within reach of the one component driven by free-text input. "What does the pipeline do on a tick?" stays a question for a dev session, where the answer comes with the code in front of it.

## Decision 5 — MCP is rejected for this, and the reason matters for later

MCP earns its keep across a **process or vendor boundary**, when something outside the codebase needs to call a tool. There is no such boundary here: the agent's tools are Python functions in the same package, imported directly. `_fn(...)` + `_build_impls(...)` already *is* a tool registry with JSON Schema attached.

An MCP layer would add a server process, a second serialization hop, and duplicated schemas — for zero new capability, while spending free-tier tokens on a fatter tool list.

Worth noting a specific regression it would cause: OpenClaw holds a direct `googleapiclient` Gmail seam. Swapping to a Gmail MCP server would lose `full_message_text`'s PDF-text extraction — settlement dollar breakdowns exist *only* in the attachment — and lose the drafts-only guardrail that makes `send()` structurally unreachable.

**Where it would make sense, later**: if Claude Code, Claude Desktop, or claude.ai should ever query OpenClaw. Justin considered and declined that destination for this change. Because such a server would read the *same* `TOOLS` + `_build_impls` registry, it stays roughly a 50-line facade — so the obligation this change accepts is to keep that registry clean and boundary-shaped, not to build the facade. Recorded so the question isn't re-litigated from scratch.

## Decision 6 — baseline specs were synced before this change was written

`conversational-agent` had no baseline in `openspec/specs/`: the change that introduced it (`conversational-telegram-agent`, 33/33 complete) was never archived, so its delta never landed. This change would have had nothing to diff against.

Resolved first, as its own commit: both deltas synced and the change archived. Two things surfaced while doing it, both recorded in the baselines rather than silently corrected:

- the original "name claims by pet + Petcover reference, **not** internal claim ids" requirement had been **reversed** in shipped code (`cc867e3`) — Justin acts by id;
- the `llm-backend` delta still described Cerebras as selectable, though it was removed from the code entirely on 2026-07-23.

The archived proposal keeps its original Cerebras-default text. It is the record of what was proposed at the time; rewriting it would destroy the trail.

**Known gap, deliberately out of scope**: five more changes describing shipped code remain un-archived with open tasks (`telegram-claim-actions` 9, `openclaw-personal-assistant` 2, `vet-claim-automation` 2, `unified-visit-claim-view` 1, `fix-email-matching-gaps` 4). None block this work. Flagged here so the backlog is visible and not mistaken for completeness.

## Constraint — the tool schema is not free

The full tool schema ships in **every** request, on a Groq free-tier budget, and this change takes 8 tools to ~15. Consequences accepted:

- every tool description stays one line;
- a test guards the serialized `TOOLS` size, so the next addition has to be deliberate;
- `agent.handle_message` passes `max_iterations=6` (the default 4 is tight for sweep → read → answer).

**Unresolved contradiction to settle by measurement, not by picking one**: `agent.py`'s docstring says turns are kept "under the provider's 8k context cap"; `config.py` says llama-3.3-70b has "no context cap" but 100k tokens/day. Both are load-bearing claims about the same model and they cannot both be right. Measure the real limit and record it in one place; do not inherit the discrepancy into a change that makes every request bigger.
