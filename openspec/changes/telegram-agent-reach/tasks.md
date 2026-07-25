# Tasks

## 0. Baseline sync (prerequisite — done)

- [x] 0.1 Sync `conversational-agent` + `llm-backend` deltas into `openspec/specs/`, recording the reversed claim-id requirement and Cerebras' removal
- [x] 0.2 `git mv conversational-telegram-agent` → `openspec/changes/archive/2026-07-25-conversational-telegram-agent`
- [x] 0.3 `openspec validate --all` green (14 passed)

## 1. Date awareness

- [x] 1.1 `agent.system_prompt()` injects today's UTC date
- [x] 1.2 `_find_claims` gains a transaction-date range filter alongside the existing ones
- [x] 1.3 `query_claims` tool exposes `since` / `until` / `merchant`
- [x] 1.4 `pending_actions` tool takes `since` / `until`, filtering in the wrapper — `claim_status.pending_actions()` itself unchanged
- [x] 1.5 Reply text names the range it applied, and says so when the range is empty

## 2. Scoped mailbox sweeps

- [x] 2.1 `pipeline.poll_petcover_status()` returns a `{checked, events, claims_changed}` summary instead of `None` (existing callers ignore it)
- [x] 2.2 `rematch_claims(merchant=None, claim_id=None)` over `pipeline._pending_claims()` + `invoice_matching.match_claim`, reporting per-claim outcomes by id
- [x] 2.3 `poll_petcover_now()` over the summary from 2.1, reply stating the unprocessed-mail-only scope
- [x] 2.4 Narrow the prompt's mailbox rule to the true shape (cannot browse/search; can run these named sweeps and must scope-state them)

## 3. New reads

- [x] 3.1 `claim_status.submissions_awaiting_reply()` — one row per `draft_id`, days waiting, newest event
- [x] 3.2 `claim_status.claim_detail(claim_id)` — txn, line items, claimable, flag, events with recorded figures
- [x] 3.3 Expose both as tools

## 4. Assistant side onto Telegram

- [x] 4.1 `list_tasks(status=None)` read + tool
- [x] 4.2 Generalize `telegram_bot._register_action` to a claim-independent token
- [x] 4.3 `propose_create_task` / `propose_close_task` + their `_execute_action` branches
- [x] 4.4 Prompt guidance: task ids always shown; never invent an outcome

## 5. Budget + loop

- [x] 5.1 ~~`handle_message` passes `max_iterations=6`~~ — raised, then put BACK to 4: a live 429 exposed a 100k tokens/**day** cap that header-only measurement missed (ADR-0016 amendment). Extra iterations are charged against <40 requests/day.
- [x] 5.2 Measured: 131,072 context / 32,768 completion / 12,000 tokens-per-min / 1,000 req-per-day / **100,000 tokens-per-DAY**. `agent.py`'s "8k cap" was wrong; `config.py`'s "100k/day" was right. The TPD limit is absent from rate-limit headers and appears only in the 429 body — measuring headers alone first produced a confident, wrong "no daily token limit exists"
- [x] 5.3 Keep every new tool description to one line

## 6. Tests (`app/tests/test_core.py`)

- [x] 6.1 July-2025-scoped `pending_actions` includes a July txn, excludes August
- [x] 6.2 `query_claims` date range + merchant filter
- [x] 6.3 `rematch_claims` reports per claim and leaves non-`pending_match` claims untouched
- [x] 6.4 `poll_petcover_now` summary shape, `poll_petcover_status` stubbed
- [x] 6.5 **Replay safety: each sweep run twice, second run a no-op** — if either fails, that tool becomes proposal-gated instead
- [x] 6.6 `submissions_awaiting_reply` returns one row per `draft_id`, not per claim
- [x] 6.7 `claim_detail` surfaces the recorded settlement figures and the flag
- [x] 6.8 `propose_create_task` queues only — `tasks` table unchanged until `_execute_action` runs
- [x] 6.9 `system_prompt()` carries today's date **and** still carries the real pet list
- [x] 6.10 Regression: narrowed mailbox rule present, not deleted
- [x] 6.11 Serialized `TOOLS` schema under a byte ceiling

## 7. Docs

- [x] 7.1 ADR-0016 — tool-surface boundary, direct-acting sweeps under replay, MCP rejection (0014/0015 taken)
- [x] 7.2 `CONTEXT.md` — assistant-side vocabulary (Task, Follow-up) is absent entirely
- [x] 7.3 `app/openclaw/CLAUDE.md` — its `agent.py` row asserts no mailbox access, which this change makes wrong

## 8. Deploy + live verification

- [x] 8.1 Full suite green (104 before this change)
- [x] 8.2 Deploy via `./scripts/deploy.ps1` from the `Me.OpenClaw-telegram-claimquery` worktree — not bare compose (leaves `APP_VERSION` unknown, mistags every `telegram_messages` row)
- [x] 8.3 Live: "what actions do I have for July 2025 transactions"
- [x] 8.4 Live: "what did I send that's awaiting a reply"
- [x] 8.5 Live: scoped "go through the Petcover mail" — confirm the reply states its own scope
- [ ] 8.6 Live: capture a task from chat, confirm the tap writes it and the untapped case doesn't
- [ ] 8.7 Record what was *actually verified live* here, not merely what was coded

## 9. Live verification results (2026-07-25, real DB + real Gmail + real Groq)

Deployed `e180b92+feat/telegram-agent-reach` via `deploy.ps1`; `/health` reported
`polling_alive: true`, queue 0.

**Verified working against real data:**
- Date scoping — July 2025 correctly reports the range empty; Aug 2025 returns exactly #21 and #20; Dec 2025 returns #16 and #17. The model resolved "July 2025" to the right range unaided.
- `submissions_awaiting_reply` — 5 real submissions, batches correctly collapsed to one entry each (#8+#22, #6+#7).
- `claim_detail(21)` — produced the answer that was previously impossible: flag plus `claimed_amount=$35.00 paid_amount=$22.75 fixed_excess_stated=$0.00 age_contribution_stated=$12.25`. Also re-surfaces the known #21 data-quality gap (our $44.75 vs Petcover's stated $35.00 claimed).
- `rematch_claims(merchant="bankstown")` — real Gmail search, scoped to exactly the 2 pending Bankstown claims, left #17 (a different vet) alone. **Run twice: identical result, no state change** — the idempotency the direct-acting decision rests on, confirmed live and not just in a stub.
- `poll_petcover_now` — run twice, no new events, and the reply carried its own scope disclaimer rather than implying Petcover had never written.

**Three real bugs the live pass caught that tests had not:**
1. `"what claim emails were sent that you can verify and check for a response"` routed to the **vet** invoice-request sweep and answered "nothing to verify" while 5 submissions sat awaiting Petcover. Both tool descriptions said "sent"; neither said to whom. Fixed + regression test (`841e3a4`).
2. `"last reply: unclassified"` read as though Petcover had answered something meaningful, when an unclassified event is a reply we couldn't parse and never sets status (ADR-0008). Fixed (`c915ead`).
3. `"remember I need to call the vet…"` died with a raw Groq 400 dump — the model emitted `<function=list_tasks,{...}</function>` and `_is_rate_limited` only matched 429, so it never retried. Now retried, with a message that doesn't claim an outage. Also called `list_tasks` for "remember X", so the prompt now maps remember/don't-let-me-forget to `propose_create_task`. Fixed + test (`e180b92`).

Post-fix re-test: all three questions answer correctly (see 8.3–8.5).

**NOT verified live — and why:**
- [x] 9.1 Task capture end-to-end (8.6) — **verified**, and verified under genuinely exhausted-budget conditions, which made it a stronger test than intended. "remember I need to call the vet about Aari's next arthritis check" → model chose `propose_create_task` (the prompt rule added after the first failure works), proposal carried no `claim_id`, `tasks` stayed at 44 rows while proposing, the simulated Confirm tap wrote `#123 "call the vet about Aari's next arthritis check"`, then the verification row was deleted. Turn took 1.6s.

- [x] 9.3 **Daily-budget fallback** (`0acad21`) — Justin hit the rate limit in real Telegram use because live verification had spent llama-3.3-70b's 100k TPD. Groq's TPD is per-model, so the chain now falls through to `openai/gpt-oss-120b` then `llama-3.1-8b-instant`. Confirmed live against the actually-exhausted primary: warning logged, answered on gpt-oss-120b in 1.6s, and the reply carried `⚠️ llama-3.3-70b-versatile is out of daily tokens — answered with openai/gpt-oss-120b`. TPD gets one attempt per model (retrying can't free a daily cap); TPM keeps its backoff-and-retry.
- [ ] 9.2 The Telegram transport itself. Everything above ran through `agent.handle_message` in the container — real model, real tools, real DB — but not through a Telegram message. Justin sending the three questions from his phone is the last mile.

- [x] 9.4 **Two bugs the fallback itself exposed**, both found by Justin using it, not by tests:
  - `199cc20` — `messages[2].reasoning: reasoning is not supported with this model`. The tool loop replayed `message.model_dump(exclude_none=True)` back into the conversation, so gpt-oss-120b's `reasoning` field poisoned the next request and killed the turn. Latent since the loop was written; only reachable once the chain could route to a reasoning-capable model. Fixed with a role/content/tool_calls **whitelist**, not a `reasoning` blacklist — the next output-only field would otherwise reproduce it exactly. Verified live: a 2-round turn on gpt-oss-120b now completes (59.7s) and answers #21 correctly with all four figures.
  - `b304325` — gpt-oss-120b answers with markdown pipe tables by default, and `_handle_chat` sends plain text with no `parse_mode`, so they'd arrive on his phone as raw pipes. Prompt now requires short plain-text lines. Verified live: 0 pipes, 0 bold markers.
