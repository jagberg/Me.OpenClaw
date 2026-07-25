## Context

OpenClaw is a single-user local FastAPI app. Today the LLM is Gemini 2.5 Flash, hard-wired in `gemini.py` and called from three sites (`vet_detection`, `invoice_matching`, `tasks`) via `gemini.extract(prompt, purpose)`. ADR-0001 chose Gemini-only for v1. The free tier is now exhausted (~20 req/day observed), stalling the pipeline, and there is no chat surface — the Telegram bot in `telegram_bot.py` is a fixed set of slash commands with a clean split between pure handlers and thin async adapters, plus inline-button callbacks and a `_pending_condition` free-text flow.

Two coupled needs: (1) a backend with real free-tier headroom and a swap path, (2) free-form chat that can read claims/emails and act on them. Constraints from CLAUDE.md are binding: never send email (drafts only), never guess required fields, failures must be visible, and don't add LLM calls where regex/keywords already work.

## Goals / Non-Goals

**Goals:**
- One `llm` module all callers share; provider/model chosen by env var; Cerebras `gpt-oss-120b` default.
- Repoint the three extraction sites with zero behavior change.
- Free-form Telegram chat that answers questions about claims/status/emails and performs the existing mutations behind a confirm button.
- Preserve existing observability (`llm_calls`) and the non-silent-failure posture.

**Non-Goals:**
- No change to classification or reference extraction — those stay regex/keyword (CLAUDE.md), no LLM added.
- No multi-provider routing/escalation logic (single active provider at a time).
- No new persistent chat-history store; conversation is per-turn/short-lived in memory.
- No RAG/embeddings/vector store — data is small; summaries in the prompt suffice.
- No web dashboard changes.

## Decisions

### D1: Provider layer via the OpenAI-compatible chat-completions API

Add `app/openclaw/llm.py` exposing `chat(messages, tools=None, purpose=...)` and `extract(prompt, purpose=...)`. Cerebras, Groq, and OpenAI all speak the OpenAI `/chat/completions` shape, so one `openai` SDK client pointed at a configurable `base_url` covers all three; provider = (base_url, model, api_key, rate limit) resolved from env. `extract()` is a thin wrapper over `chat()` with a single user message, keeping the existing call sites a one-line change.

- **Why not keep `gemini.py` and add branches:** Gemini's SDK is not OpenAI-shaped; branching per provider in every caller is the coupling we're removing. Keep `gemini.py` only as an optional provider adapter behind the same interface (or drop it — see D6).
- **Why not LangChain/LiteLLM:** a new heavy dependency for what is `base_url` + `model` + a rate limiter. Ladder rung: an already-viable one-file solution beats a framework.

### D2: Cerebras default, config-driven swap

`gpt-oss-120b` on Cerebras: 1M tokens/day, ~400 chat turns/day, 8k context cap, 5 req/min — all fine given small emails (user-confirmed). `LLM_PROVIDER` / `LLM_MODEL` / per-provider `*_API_KEY` env vars select the backend. Rate limiter reuses the existing `_RateLimiter` sliding-window class from `gemini.py`, seeded from the provider's per-minute limit.

- **Alternatives:** Groq 70B (100k tok/day, no context cap) as the documented fallback if Cerebras's 8k cap ever bites; OpenAI gpt-4o-mini (~$0.65/mo) as the paid no-cap option. Both reachable by env only.

### D3: Chat entry point reuses the existing text handler

`on_text_reply` already receives non-command text but currently only services `_pending_condition`. Extend it: if a pending flow owns the message, keep today's behavior; otherwise hand the text to the agent. This honors the spec's "existing typed-reply flows still win" without a second `MessageHandler`.

### D4: Agentic loop with a small, explicit tool surface

`chat()` runs a bounded tool-calling loop (cap ~4 iterations). Tools are thin wrappers over existing functions, split read vs act:
- **Read (auto):** list/summarize claims by status, get one claim (pet + Petcover ref, flag, date, items), find matched email/status events for a pet or ref. These return **compact summaries**, never raw full-email dumps, to stay under the 8k cap and the daily token budget.
- **Act (deferred):** mark sent, set condition, assign pet, mark resolved. An act tool does NOT execute; it returns a "proposed action" the bot renders as a confirmation message with an inline confirm button (same `InlineKeyboardMarkup` pattern as `mark_sent_button`). The mutation runs in `on_callback` when tapped, calling the existing `claim_forms`/`claim_status` functions — the same code paths the slash commands use.

- **Why confirm-before-commit in the harness, not the model:** the guarantee must not depend on the LLM behaving. The act tool physically cannot mutate; only a human tap does. This is how "never send email / never guess fields" stays enforced regardless of model output.

### D5: Data exposure boundary

Tools select explicit columns; none read `.env`, secrets table, or bank fields. System prompt states the hard rules and the identify-by-pet/Petcover-ref convention (ADR/CLAUDE.md). Claims are referred to by pet name + Petcover reference, not internal ids, matching existing bot behavior.

### D6: `gemini.py` disposition

Keep `gemini.py` importable as a legacy provider selectable by `LLM_PROVIDER=gemini` (its SDK adapter behind the same `chat/extract` interface), but default off. Tests currently monkeypatch `gemini.extract`; they will be repointed at `llm.extract`.

## Risks / Trade-offs

- **8k context cap on Cerebras** → summaries-not-dumps is a hard design rule (D4); if a future need exceeds it, flip `LLM_PROVIDER=groq` (no cap) via env — no code change.
- **Chat adds LLM calls, the opposite of the quota goal** → mitigated by the 20× headroom (400 vs 20/day), bounded tool loop, cached system prompt (free on these providers), and keeping classification/references off the LLM entirely.
- **Model performs a wrong/unrequested mutation** → cannot commit without a human tap (D4); ambiguous targets ask for clarification and commit nothing.
- **Provider outage** → non-silent error surfaces to chat / claim flag; documented Groq + OpenAI fallbacks are one env var away.
- **Prompt-injection via email content read into chat** → act tools are confirm-gated, so injected "instructions" still can't mutate silently; read tools only return data the user already owns.
- **In-memory pending-confirm state lost on restart** → same accepted trade-off as `_pending_condition` today; user re-issues the request.

## Migration Plan

1. Add `openai` to `requirements.txt`; add provider/model/key vars to `config.py` and `.env.example`.
2. Add `llm.py`; move the `_RateLimiter` there (or import it). Provide `chat()`/`extract()`.
3. Repoint `vet_detection`, `invoice_matching`, `tasks` from `gemini.extract` → `llm.extract`; repoint tests' monkeypatch.
4. Add read/act tools + agent loop; wire chat into `on_text_reply` and confirm callbacks into `on_callback`.
5. Set `CEREBRAS_API_KEY`; smoke-test extraction parity against the real DB and a few chat turns (read + one confirmed mutation) before declaring done.
6. Write the ADR superseding 0001.

**Rollback:** set `LLM_PROVIDER=gemini` (if quota returns) or `groq`; the chat handler is additive, so reverting the text-handler branch restores command-only behavior without touching the pipeline.

## Open Questions

- Exact Cerebras per-minute rate-limit value to seed the limiter (5 RPM per current docs — confirm against the live key).
- Whether to persist a short rolling chat context per chat_id or stay strictly single-turn (default: single-turn to cap tokens; revisit if follow-up questions feel broken).
