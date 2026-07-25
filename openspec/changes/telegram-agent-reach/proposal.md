## Why

Justin can ask the Telegram agent things today, but the useful questions all fail — and they fail on *reach*, not on the model. Three he actually asked for, each traced to a specific gap:

| Question | Why it can't be answered |
|---|---|
| "Go through my emails from a specific vet or Petcover and see if they can be processed or change a status" | The agent has **no mailbox reach at all**. Its prompt hard-forbids it and offers only `reconcile_sent_invoice_requests`. The two sweeps that would answer this — `invoice_matching.match_claim` and `pipeline.poll_petcover_status` — run only on the 15-minute tick: never on demand, never scoped to one vet. |
| "What claim emails were sent that you can verify and check for a response" | Nothing answers it. `reconcile_sent_invoice_requests` covers *invoice-request* drafts only; for claim submissions no read pairs "sent" with "has a reply come back". `dashboard_lists()` covers only the event slice. |
| "What actions do I have for July 2025 transactions" | `pending_actions()` and `agent._find_claims()` have **no date filter**, and `system_prompt()` never states today's date — so every relative-date question is guesswork. |

Separately, the assistant half of OpenClaw (`tasks.py`, `reminders.py`) has **zero** Telegram surface — no command, no tool, and no list function anywhere in the codebase. It is reachable only by reading the DB directly.

## What Changes

- **Date awareness**: inject today's UTC date into the system prompt; `pending_actions` and `query_claims` take optional `since`/`until`, plus `merchant` on the latter (`_find_claims` already accepts it — it was simply never wired to a tool argument).
- **Scoped on-demand mailbox sweeps**: `rematch_claims(merchant|claim_id)` runs the existing `match_claim` over `pending_match` claims; `poll_petcover_now()` runs the existing Petcover poll. Both **act directly** rather than proposing, and both are safe under ADR-0014's at-least-once replay because each is idempotent (see design). `pipeline.poll_petcover_status()` gains a summary return so the agent reports what changed instead of "done".
- **The mailbox rule is narrowed, not deleted**: the agent still cannot browse or search mail; it can run these two named sweeps and must say so plainly. This modifies a requirement that exists because of an observed live failure.
- **`submissions_awaiting_reply()`**: new `claim_status` read, one row per Submission (grouped by `draft_id`), carrying days-waiting and the newest status event.
- **`claim_detail(claim_id)`**: one claim in full — invoice line items, claimable subtotal, flag, and every status event *with its recorded dollar figures*. Answers "why did #21 flag?", which today's `claim_history` cannot (event types and subjects only, and keyed by pet/reference rather than id).
- **Assistant side onto Telegram**: `list_tasks`, `propose_create_task`, `propose_close_task`. Requires generalizing `telegram_bot._register_action` from its `action:claim_id` token — tasks have no claim id.
- **Deliberately excluded**: force-reprocessing already-seen Petcover mail, and any code/docs/spec reading inside the container. Both are reasoned about in design.md.

## Capabilities

### Modified Capabilities
- `conversational-agent`: gains date-scoped queries, two named mailbox sweeps (with the never-imply-mailbox-access requirement narrowed accordingly), submission-reply and claim-detail reads, and task capture/closure under the existing confirm-before-commit gate.

### New Capabilities
- `task-telegram-surface`: reading and mutating the assistant-side `tasks` table from Telegram, under the same proposal gate claims mutations use.

## Impact

- **Modified code**: `agent.py` (tool registry, `_build_impls`, `system_prompt`, prompt rules — the bulk), `claim_status.py` (`submissions_awaiting_reply`, `claim_detail`), `pipeline.py` (`poll_petcover_status` returns a summary), `telegram_bot.py` (opaque action tokens, task branches in `_execute_action`).
- **No schema change.** Every read is over existing tables.
- **Token budget**: the tool schema ships in *every* request on a Groq free-tier budget and this takes 8 tools to ~15. Descriptions stay one line, and a test guards the serialized schema size. `agent.py`'s docstring claims an 8k context cap while `config.py` says the model has none — contradictory, so it gets measured and the answer recorded rather than inherited.
- **Docs**: ADR-0016 (tool-surface boundary + why MCP is rejected), `CONTEXT.md` (no assistant-side vocabulary exists), `app/openclaw/CLAUDE.md` (its `agent.py` row asserts no mailbox access, which this change makes wrong).
- **Cost**: $0 — same free-tier backend, more tools.
