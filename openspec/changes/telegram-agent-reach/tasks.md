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

- [x] 5.1 `handle_message` passes `max_iterations=6`
- [x] 5.2 **Measure** the real context/token limit and resolve the `agent.py` "8k cap" vs `config.py` "no context cap" contradiction — record the answer in one place, don't inherit it
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
- [ ] 8.2 Deploy via `./scripts/deploy.ps1` from the `Me.OpenClaw-telegram-claimquery` worktree — not bare compose (leaves `APP_VERSION` unknown, mistags every `telegram_messages` row)
- [ ] 8.3 Live: "what actions do I have for July 2025 transactions"
- [ ] 8.4 Live: "what did I send that's awaiting a reply"
- [ ] 8.5 Live: scoped "go through the Petcover mail" — confirm the reply states its own scope
- [ ] 8.6 Live: capture a task from chat, confirm the tap writes it and the untapped case doesn't
- [ ] 8.7 Record what was *actually verified live* here, not merely what was coded
