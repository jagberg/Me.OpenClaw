# ADR-0016: The Telegram agent's tool surface — how far it reaches, and why not MCP

**Date**: 2026-07-25
**Status**: accepted; gate location superseded by ADR-0025, tool surface by ADR-0023 (2026-08-01)
**Deciders**: Justin

## Context

`agent.py` is OpenClaw's only *pull* interface. Everything else pushes: the pipeline notifies, the dashboard displays, Telegram cards offer taps. The agent is the one place Justin can ask an arbitrary question.

It was also the narrowest surface in the system — 8 tools, no date awareness, no mailbox reach, no assistant side — and the questions he actually asked all failed on that narrowness rather than on the model:

- *"Go through my emails from a specific vet or Petcover and see if they can be processed"* — the two sweeps that answer this (`invoice_matching.match_claim`, `pipeline.poll_petcover_status`) ran only on the 15-minute tick, never on demand, never scoped to one vet.
- *"What claim emails were sent that you can verify and check for a response"* — nothing paired "sent" with "did a reply come back".
- *"What actions do I have for July 2025 transactions"* — no date filter anywhere, and the prompt never stated today's date.

Widening it means deciding *how far*. Four of those boundaries are non-obvious.

## Decision

### 1. The no-mailbox-access rule is narrowed, not deleted

The prompt said, absolutely: *"You CANNOT read Justin's mailbox, search his email, or see what he has sent […] Never imply you looked at his email."*

That was not defensive boilerplate. It was added because the agent **fabricated the action** — told Justin it had checked his sent mail when it had no such capability. For a single-user assistant that is the worst available failure: an answer indistinguishable from a real one.

Three named sweeps now exist, so the rule as written became false. It is narrowed rather than removed:

- cannot browse, search, or read arbitrary mail — unchanged, still absolute;
- *may* run `reconcile_sent_invoice_requests`, `rematch_claims`, `poll_petcover_now` and report what each found;
- must state what the sweep actually covered ("I re-checked the 3 unmatched Bondi Vet claims"), never "I looked through your email".

A regression test asserts the narrowed wording, because the previous test matched a loose `"cannot read"` and started passing off an unrelated line the moment the rule was reworded.

### 2. Sweeps act directly; they are not proposal-gated

Every other agent mutation is a proposal awaiting a Confirm tap. These two are not:

1. the pipeline already makes these exact calls unattended every 15 minutes — gating on-demand invocation while the scheduler runs it unsupervised would be theatre;
2. neither can send anything (Gmail is read + drafts only, structurally);
3. a bad match is reversible via the existing ❌ Wrong invoice path — and per-claim confirmation would defeat the purpose, since "go through the vet's emails" is inherently a sweep over several claims.

**Replay safety is the load-bearing part.** ADR-0014 made update handling at-least-once: `message_log.replay_pending` re-runs any update whose handler didn't finish, so a chat turn can execute a sweep **twice**. Both are idempotent — `rematch_claims` only considers `pending_match` claims, and `poll_petcover_now` skips anything `gmail_ingest._already_processed` marked. That is *why* direct action is acceptable rather than a happy accident, and a test runs each sweep twice and requires the second to be a no-op. If either ever stops being idempotent, that tool becomes proposal-gated.

### 3. No force-reprocess of already-seen Petcover mail

Replaying a seen email against the append-only event log risks re-applying a status transition, and status is what this service exists to track correctly. The `_already_processed` guard is load-bearing.

Consequence that must stay visible: "nothing new" is not "Petcover hasn't replied". The tool's own reply text disclaims the stronger reading explicitly, and a test asserts it — otherwise a quiet poll reads as evidence of insurer silence.

### 4. No code, docs, or spec reading in the container

"Ask about the system" is scoped to **claim-level why**: flags, status events, and the recorded settlement figures, delivered by `claim_detail`. Not extended to reading the repo, for two reasons: a 70B free-tier model reading Python explains it confidently and wrongly, which is worse than declining; and mounting the repo puts secrets-adjacent paths within reach of the one component driven by free-text input.

### 5. MCP is rejected for this, and the reason matters for later

MCP earns its keep across a **process or vendor boundary**, when something outside the codebase needs to call a tool. There is no such boundary here: the tools are Python functions in the same package, imported directly, and `_fn(...)` + `_build_impls(...)` already *is* a tool registry with JSON Schema attached. An MCP layer would add a server process, a second serialization hop, and duplicated schemas for zero new capability.

A specific regression it would cause: OpenClaw holds a direct `googleapiclient` Gmail seam. Swapping to a Gmail MCP server would lose `full_message_text`'s PDF-text extraction — settlement dollar breakdowns exist *only* in the attachment — and lose the drafts-only guardrail that makes `send()` structurally unreachable.

**Where it would make sense**: if Claude Code, Claude Desktop, or claude.ai should ever query OpenClaw. Justin considered and declined that destination. Because such a server would read the *same* registry, it stays roughly a 50-line facade — so the obligation accepted here is to keep that registry clean and boundary-shaped, not to build the facade.

## Consequences

### Positive
- The three questions that motivated this are answerable, each from data that already existed.
- The assistant side (`tasks`, `reminders`) has a surface for the first time; it was previously reachable only by querying the DB.
- `claim_detail` makes "why is #N like this" answerable from the flag plus the figures the reply actually carried — previously impossible, since `claim_history` gave event types only and was keyed by pet/reference rather than id.
- `poll_petcover_status()` now returns a summary, so any caller can report what changed instead of "done".

### Negative / Risks
- **A prompt rule with a carve-out is weaker than an absolute one.** The model may still over-claim at the margin. Mitigated by the sweeps' reply text carrying their own scope, so the *evidence* in the answer is bounded even if the prose overreaches.
- **The tool schema is not free.** 8 tools → 15, and the whole schema ships in every request. Measured 5.9 KB; a test caps it at 9 KB so the next addition is deliberate.
- **`max_iterations` was raised 4 → 6 and then put back to 4** — see the amendment below. The daily token budget, not per-turn depth, is the scarce resource.
- `submissions_awaiting_reply` measures waiting from `vet_claims.updated_at`, because **no sent-at column exists**. `mark_sent` stamps it and nothing else touches an unanswered submission, so it is correct in the common case; once a reply lands the last event is reported instead. A precise sent-at column would need live DDL and was not worth it for this.

## Note — a documented limit that was wrong in two places

Resolving this required settling a contradiction rather than inheriting it: `agent.py` claimed turns were kept "under the provider's 8k context cap" while `config.py` said the model had "no context cap" and "100k tokens/day". Measured from the API on 2026-07-25:

| | measured |
|---|---|
| context window | 131,072 tokens |
| max completion tokens | 32,768 |
| tokens per minute | 12,000 |
| requests per day | 1,000 |
| **tokens per day** | **100,000** |

`agent.py`'s "8k context cap" was simply wrong. `config.py`'s "100k tokens/day" was right — see the amendment for how nearly that got "corrected" away.

The "keep turns small" discipline was right, but not for the stated reason: the binding constraint is the daily token budget, not per-request context.

## Amendment (2026-07-25) — the daily cap that headers don't report

The first version of this ADR stated flatly that **"There is no daily token limit."** That was wrong, and wrong in the more dangerous direction: it "corrected" a `config.py` comment that had been right all along.

The error came from *how* it was measured. `x-ratelimit-limit-*` response headers report tokens-per-minute and requests-per-day, and nothing else — so header inspection alone produces a confident, complete-looking picture with the daily token limit missing from it. The limit surfaced the same hour, from the body of a real 429 during live verification:

> Rate limit reached … on tokens per day (TPD): Limit 100000, Used 98676, Requested 2638. Please try again in 18m55s.

Two consequences:

1. **`max_iterations` goes back to 4.** It had been raised to 6 for headroom on the reasoning that only a 12,000/minute limit applied, where extra iterations cost latency at worst. Against 100,000 tokens/**day** at ~2,600 tokens a request — the tool schema ships every time — the day's whole budget is under 40 requests, and one turn can spend several. 4 rounds still cover the deepest real path (sweep → read → answer) with a spare. Not a reversal of the decision, a correction of the arithmetic it rested on.
2. **The tool-schema size guard matters more than it looked.** At ~1.5k of those 2.6k tokens, the schema is the largest fixed cost of every request, and 8 → 15 tools raised the price of *every* chat turn, not just the ones using the new tools.

Method note worth keeping: this is the second time in this change that measuring the obvious surface gave a wrong-but-plausible answer. Headers are a partial view of a provider's limits; the errors it throws are the complete one.

---

## Amendment (2026-08-01) — the gate's location is superseded by 0025; the reasoning is not

**Superseded in part by:** ADR-0025 (gate location), ADR-0023 (tool inventory).

This ADR placed the proposal gate in `telegram_bot._execute_action` and made the
point that a gate in a prompt is not a gate. **That point stands and is now
carried further.** What changes is only *where* the code sits, because
`telegram_bot.py` is deleted by the gateway swap (ADR-0024).

- **Gate location:** split by origin. A card's confirm tap commits behind
  `/internal`; a chat-initiated proposal commits in the MCP surface. Both inside
  Python. ADR-0025.
- **Tool surface:** this ADR's registry in `agent.py` becomes an enumerated MCP
  inventory plus a `tools.allow` allowlist on the gateway. The change is not
  cosmetic — on a general-purpose agent runtime, prompt discipline is not a
  boundary at all, so the inventory has to be one. ADR-0023.
- **The three named sweeps** (`reconcile_sent_invoice_requests`,
  `rematch_claims`, `poll_petcover_now`) and the rule that the agent must state
  their scope rather than imply it read the mailbox: **unchanged**. Reinforced,
  in fact — the platform's stock agent was observed on 2026-08-01 claiming it had
  checked mail in a runtime holding no mail credential, which is exactly the
  failure this ADR named and exactly why enforcement is the inventory rather
  than the prompt.

Recorded here rather than only in ADR-0025 so that a reader arriving at this ADR
first is not led to a location that no longer exists.
