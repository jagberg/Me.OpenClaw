## Why

The Telegram bot only understands a fixed set of slash commands (`/mark`, `/pet`, `/sent`…), so any question about the claims or emails means opening the dashboard or SSHing the DB. Justin wants to just *ask* — "which claims are blocked?", "did Petcover reply about Bella?" — and act on the answer in the same thread. Separately, the Gemini free tier is exhausted (~20 requests/day in practice), which blocks both the existing extraction pipeline and any new chat feature. Both problems share one root: the LLM backend is a single hard-wired provider with no headroom and no fallback.

## What Changes

- Add a **provider-agnostic LLM layer** (`llm.chat()`) alongside the existing `gemini.extract()`. Default backend becomes **Cerebras free tier** (`gpt-oss-120b`, 1M tokens/day, OpenAI-compatible). Provider selected by env var so swapping to Groq/OpenAI/Anthropic later is config-only.
- Repoint existing extraction call sites (`vet_detection`, `invoice_matching`, `tasks`) at the new layer so they stop depending on the drained Gemini quota. Behavior unchanged; only the transport moves.
- Add **free-form chat** to the Telegram bot: any non-command message from the authorized user is answered by an LLM agent that can read claims, claim status, and matched emails, and can **perform mutations** (mark sent, set condition, assign pet, mark resolved) — each mutation gated behind a **confirm button** before it commits.
- Existing slash commands and inline buttons stay as-is (fast paths); chat is additive.
- **BREAKING (internal only):** ADR-0001 ("Gemini-only backend") is superseded by a new ADR recording the provider-abstraction + Cerebras-default decision.

## Capabilities

### New Capabilities
- `llm-backend`: Provider-agnostic LLM access — a single `chat()`/`extract()` interface, configurable provider/model, per-provider rate limiting, call logging, and a non-silent failure path. Cerebras is the v1 default.
- `conversational-agent`: Free-form Telegram chat that answers questions about claims, claim status, and matched emails, and executes claim mutations through a confirm-before-commit flow honoring the project's hard rules (never send email, never guess required fields).

### Modified Capabilities
<!-- No main-spec requirements change; extraction call sites are repointed at the new layer without altering their spec-level behavior. -->

## Impact

- **New code:** `app/openclaw/llm.py` (provider layer), chat/agent handling in `telegram_bot.py`, a read/act tool surface over existing `claim_forms`/`claim_status`/`db` functions.
- **Modified code:** `vet_detection.py`, `invoice_matching.py`, `tasks.py` switch from `gemini.extract` to the new layer; `config.py` gains provider/model/key vars; `.env.example` documents them.
- **Dependencies:** add an OpenAI-compatible client (Cerebras/Groq/OpenAI all speak it); `google-genai` stays only if Gemini is kept as a selectable provider.
- **Docs:** new ADR superseding 0001; note in CLAUDE.md that classification/references stay regex/keyword (unchanged) — only chat + extraction use the LLM.
- **Cost/quota:** $0/mo, ~400 chat turns/day ceiling vs current ~20; no credit card.
- **Data:** free-tier providers may train on submitted text — same posture already accepted for household-admin content in ADR-0001; no more sensitive data is exposed by chat than the pipeline already sends.
