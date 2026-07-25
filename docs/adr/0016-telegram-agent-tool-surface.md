# ADR-0016: The Telegram agent's tool surface — how far it reaches, and why not MCP

**Date**: 2026-07-25
**Status**: accepted
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
- **`max_iterations` went 4 → 6**, so a turn is now up to 7 requests. Against Groq's measured **12,000 tokens/minute**, a heavy turn can exhaust a minute's budget on its own. `llm._completion` retries 429s with backoff, so this degrades to a slow answer rather than a failure — but it is a real new pressure, not a free change.
- `submissions_awaiting_reply` measures waiting from `vet_claims.updated_at`, because **no sent-at column exists**. `mark_sent` stamps it and nothing else touches an unanswered submission, so it is correct in the common case; once a reply lands the last event is reported instead. A precise sent-at column would need live DDL and was not worth it for this.

## Note — a documented limit that was wrong in two places

Resolving this required settling a contradiction rather than inheriting it: `agent.py` claimed turns were kept "under the provider's 8k context cap" while `config.py` said the model had "no context cap" and "100k tokens/day". Both were wrong. Measured from the API on 2026-07-25:

| | measured |
|---|---|
| context window | 131,072 tokens |
| max completion tokens | 32,768 |
| `x-ratelimit-limit-tokens` | **12,000 per minute** |
| `x-ratelimit-limit-requests` | 1,000 per day |

There is no daily token limit. The "keep turns small" discipline was right for the wrong reason — the binding constraint is per-minute throughput, not per-request context. Both comments now carry the measured figures.
