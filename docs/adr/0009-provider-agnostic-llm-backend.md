# ADR-0009: Provider-agnostic LLM backend (Groq default), superseding 0001

**Date**: 2026-07-19
**Status**: accepted (supersedes ADR-0001)
**Deciders**: Justin

## Context

ADR-0001 hard-wired Gemini 2.5 Flash (AI Studio free tier) as the sole LLM backend. That free tier is now exhausted (~20 requests/day observed), which stalls the extraction pipeline (`vet_detection`, `invoice_matching`, `tasks`). Separately, we're adding a conversational Telegram agent that needs an LLM with real free-tier headroom and tool-calling. Both needs share one root cause: a single hard-wired provider with no swap path and no fallback.

## Decision

Introduce `app/openclaw/llm.py` as the single LLM seam — `chat(messages, tools, tool_impls, ...)` and `extract(prompt, purpose)`. No other module imports a provider SDK directly. Provider and model are chosen by env var (`LLM_PROVIDER`, `LLM_MODEL`); Groq `llama-3.3-70b-versatile` (free tier) is the default. Cerebras, Groq and OpenAI all speak the OpenAI `/chat/completions` shape, so one `openai` client with a configurable `base_url` covers them — switching is a config change, not a code change. Gemini stays selectable (`LLM_PROVIDER=gemini`, `extract()` only) as a rollback path. The reused `_RateLimiter` and `_log_call` preserve the existing rate-limit + `llm_calls` observability, and `LLMUnavailableError` keeps failures non-silent.

**Default was originally Cerebras `gpt-oss-120b`** (1M tokens/day, larger free budget), but Cerebras' free inference tier turned out to be **sold-out for this account** — every free model (gpt-oss-120b, zai-glm-4.7, gemma-4-31b) returns `402 payment_required`, and the billing page shows all inference plans "Temporarily Sold Out" (2026-07). Groq is the verified-working default; Cerebras remains selectable for if/when capacity returns. This is exactly the single-provider-failure the abstraction was built to absorb: the switch was one env var + one default.

## Alternatives Considered

- **Groq (Llama 3.3 70B) default** — free, no context cap, but 100k tokens/day vs Cerebras' 1M. Kept as the primary documented fallback.
- **gpt-4o-mini** — cheap (~$0.65/mo) and uncapped, but paid from the first call. Kept as the paid option.
- **LangChain / LiteLLM** — a heavy dependency for what is `base_url` + `model` + a rate limiter. Rejected: one file does it.

## Consequences

### Positive
- $0/mo on Groq's free tier (100k tokens/day, no context cap) — well above Gemini's ~20 requests/day, plus all extraction.
- Provider swap is one env var; resilient to a single provider being down or out of quota.
- Extraction behaviour unchanged — only the transport moved.

### Negative / Risks
- The agent passes compact summaries, not raw email dumps (enforced in `agent.py`) — Groq has no context cap, but this keeps quota use low and survives a swap back to a context-capped provider like Cerebras.
- Free-tier providers may train on submitted text — same posture accepted in ADR-0001 for household-admin content.
- Free-tier keys can hit quota/billing walls (observed live: Cerebras returned `402 payment_required`); the swap path (Groq/OpenAI) is the mitigation.

## Amendment (2026-07-25) — "no context cap" was wrong; the quota mitigation is now automatic

Two claims in this ADR need correcting against measurement. The decision itself stands.

**1. "No context cap" is wrong.** This ADR asserts it three times (Alternatives, Consequences/Positive, and Negative/Risks). Measured from the Groq API on 2026-07-25, `llama-3.3-70b-versatile` has a **131,072-token context window** — large, but a cap. The related "100k tokens/day" figure here was **right**, and is the binding constraint; see ADR-0016's amendment for how nearly that got "corrected" away by measuring rate-limit headers, which never mention the daily token limit at all.

The reasoning built on the wrong claim survives anyway: this ADR's Negative/Risks note says compact summaries "keep quota use low and survive a swap back to a context-capped provider". The first half is the real reason, and it's the daily budget it protects — not a per-minute or per-request ceiling.

**2. The stated quota mitigation is superseded.** This ADR recorded: *"Free-tier keys can hit quota/billing walls … the swap path (Groq/OpenAI) is the mitigation."* That proved insufficient in practice. When `llama-3.3-70b-versatile` exhausted its daily budget, the chat agent was dead — the swap path is manual, requiring an env var and a restart, which is useless to someone holding a phone. And the failure wasn't at provider granularity: Groq was healthy, one *model's* budget was gone.

**Superseded by ADR-0017**, which adds automatic per-model fallback within the provider (Groq's TPD is per model). The manual provider swap remains available and remains correct for an actual provider outage — which is what this ADR was reasoning about. It simply wasn't the failure that occurred.

## Notes

Classification and Petcover reference extraction remain regex/keyword on purpose (quota discipline) — chat and extraction are the only LLM users.

---

## Addendum — 2026-08-01: the gateway takes chat; Groq survives a scare

The conversational loop moves to the OpenClaw gateway (ADR-0024), which does its
own model resolution. This ADR's decision is unchanged for `extract()` and
`extract_vision()`; what follows is where the boundary now falls.

**Groq stays the default, but nearly did not.** A stock gateway turn measured
22,810 prompt tokens against Groq free tier's 12,000 tokens per minute — a
per-request impossibility, not exhaustion. It was recorded for most of a day as
a hard blocker, on the strength of a total read off a `413` error. Itemised, the
turn was three-quarters content the deployment chooses; trimmed, it is 3,865.
See ADR-0023. The lesson is the method, not the number: a total tells you that
you are over a limit and never what to cut.

**What the gateway covers:** chat turn model selection, and failover between
configured candidates.

**What `llm.py` keeps:** `extract()` and `extract_vision()`, including ADR-0017's
per-model daily walk. These are the calls that actually exhaust a daily budget —
batch extraction over a mailbox — and the gateway never sees them.

**The gap, accepted:** the gateway classifies every quota failure as
`rate_limit` and treats it as *transient*, re-probing the same model during
cooldown (`shouldAllowCooldownProbeForReason`). Groq's ceiling is per day, per
model, so waiting cannot help and only moving to another model's budget does.
Chat therefore spends three futile retries per exhausted-day turn before moving
on. Mitigated by configuring a multi-model chain so `next=none` becomes
`next=<model B>`; not eliminated. This is a regression against ADR-0017 for the
chat path only, recorded rather than left to be discovered.
