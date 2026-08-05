# ADR-0017: Automatic per-model fallback when a daily token budget is spent

**Date**: 2026-07-25
**Status**: accepted
**Deciders**: Justin

## Context

Justin sent a normal question to the Telegram agent and got a rate-limit error. `llama-3.3-70b-versatile`'s free-tier budget of **100,000 tokens/day** was gone — largely spent by that morning's live verification — and the chat agent was simply dead until the rolling window decayed. Nothing degraded; it stopped.

ADR-0009 built the provider-agnostic seam precisely to absorb single-provider failure, and its recorded mitigation for quota walls was "the swap path (Groq/OpenAI) is the mitigation". Two problems with that in this case:

- the swap is **manual** — an env var and a restart, which is no use to someone holding a phone;
- the failure wasn't at provider granularity at all. Groq was healthy. **One model's** daily budget was exhausted.

The relevant discovery: Groq's TPD limit is **per model** — the 429 names it, *"Rate limit reached for model `llama-3.3-70b-versatile` … on tokens per day"*. So an exhausted daily budget is survivable by moving models within the same provider, using the same key.

This is distinct from the per-minute token limit (12,000 TPM), where every model shares nothing and waiting genuinely is the only cure.

## Decision

`llm._completion` walks a chain of models, falling through **only** on daily-budget exhaustion:

```
llama-3.3-70b-versatile  (primary)
  → openai/gpt-oss-120b
  → openai/gpt-oss-20b
  → llama-3.1-8b-instant
```

Four separate daily budgets, one provider, one key.

1. **TPD gets exactly one attempt per model.** Retrying cannot free a daily cap, so `_try_model` re-raises immediately rather than burning its retry budget. TPM keeps its existing backoff-and-retry against the same model, because there waiting *does* help. `_is_daily_budget_exhausted` distinguishes them by the 429 body — the only place TPD is named.

2. **Ordered by capability**, on the principle that a weaker answer beats no answer for a household assistant. There is no scenario where Justin prefers an error to a slightly worse reply.

3. **The degradation is disclosed.** A fallback reply is prefixed with which model produced it. A quietly weaker answer is exactly the invisible failure the project's hard rules forbid, and he should be able to weigh a downgraded reply.

4. **Every link was probed end-to-end before being trusted**, against the real 15-tool schema, not assumed from a model card. All picked the right tool with correct arguments and completed the loop:

   | model | full turn |
   |---|---|
   | `openai/gpt-oss-120b` | 0.9s |
   | `openai/gpt-oss-20b` | 1.3s |
   | `llama-3.1-8b-instant` | 2.1s |

5. **`qwen/qwen3.6-27b` is deliberately excluded.** It answers correctly but took **62 seconds**. That is not a fallback — it is a hang with a reply at the end.

## Alternatives considered

- **Wait for the rolling window.** Zero code, and what happened by default. Rejected: the agent is unusable for an unpredictable stretch, and the window is opaque — Justin has no way to know whether it's 7 minutes or 40.
- **Switch provider to OpenAI on exhaustion** (the ADR-0009 mitigation, automated). Rejected as the *first* line: it starts costing money for what a second free bucket solves, and it changes provider for a problem that isn't the provider's. Still available manually via `LLM_PROVIDER`.
- **Reduce tokens per turn instead** (the tool schema is ~1.5k of every ~2.6k-token request). Not rejected — it attacks the cause rather than the symptom and would help every turn, not just the exhausted case. Deferred as the larger change; recorded here because it remains the better long-term answer.
- **Raise the limit (Groq Dev Tier).** Costs money for a single-user household tool. Not pursued.

## Consequences

### Positive
- Roughly 4× the daily headroom, at $0 and with no config change.
- The failure mode moved from "dead" to "degraded and labelled".
- Every link is verified, so the chain's behaviour under real exhaustion is known rather than hoped for.

### Negative / Risks
- **Answer quality varies by which model is holding the turn.** Mitigated by disclosure, not eliminated. An 8B model answering a claims question is meaningfully weaker than a 70B one.
- **Small models honour prompt rules less reliably.** Observed immediately: `gpt-oss-20b` emitted `**bold**` despite an explicit instruction not to. Prompt discipline is best-effort below the primary.
- **A mid-turn switch means one conversation is continued by a different model.** Accepted; no problem observed, but the tool-call/tool-result pairing is now the only thing holding a turn together across a model boundary.
- **Latency was made a selection criterion**, which is a judgement call: 62s is excluded, 2.1s is fine, and nothing defines where the line sits. Revisit if a link starts running slow rather than failing.
- The chain is Groq-specific (`_FALLBACK_MODELS` is keyed by provider). Another provider gets no fallback until someone adds its models.

## Two bugs this decision exposed

Both found by Justin using the feature, not by the test suite — worth recording because the suite could not have caught either.

1. **Reasoning-field replay** (`199cc20`). `gpt-oss-120b` returns a `reasoning` field with its tool call, and `chat()`'s loop appended `message.model_dump(exclude_none=True)` — the whole message — back into the conversation. The next request died on `messages[2].reasoning: reasoning is not supported with this model`. Latent since the tool loop was written; **only reachable once this ADR's chain could route to a reasoning-capable model**. Fixed with a `role`/`content`/`tool_calls` whitelist rather than a `reasoning` blacklist, so the next output-only field a model invents doesn't reproduce it.

2. **Markdown in a plain-text channel** (`b304325`). `gpt-oss-120b` answers with pipe tables by default, and `_handle_chat` sends replies with no `parse_mode`, so they would have arrived on his phone as raw pipes. The prompt now requires short plain-text lines.

Generalisable lesson: adding a fallback path adds every *behavioural* difference of the fallback models, not just their capacity. Capacity was the reason for the change; formatting and response-shape differences came along uninvited.

---

## Amendment (2026-08-01) — untouched by the gateway's token work, and that is the point

The agent's per-turn size was cut from 22,810 tokens to 3,865 (ADR-0023). That
addresses Groq's **per-minute** ceiling of 12,000 TPM. It does nothing whatever
about the **per-day, per-model** budget of 100,000 tokens that this ADR exists
to walk.

Stated explicitly because "the token problem is solved" will otherwise be read
as covering both. Two independent limits, two independent mechanisms:

| Limit | Window | Cure | Owner |
|---|---|---|---|
| 12,000 TPM | per minute | make the request smaller | tool allowlist (ADR-0023) |
| 100,000 tokens | per day, per model | move to another model's budget | this ADR's walk |

The gateway does not distinguish them. Its failover has a single `rate_limit`
classification, treated as transient and re-probed during cooldown — correct for
a per-minute limit, useless for a per-day one. So this ADR's walk stays in
`llm.py` for extraction and vision, which are the calls that exhaust a day. See
the addendum to ADR-0009 for what the chat path gives up.

---

## Amendment (2026-08-04) — the gateway side now has the walk too, and it was needed the same day

ADR-0009's 2026-08-01 amendment recorded this as an accepted gap: the gateway
classifies every quota failure as `rate_limit`, treats it as transient, and
re-probes the same model, so *"chat therefore spends three futile retries per
exhausted-day turn before moving on. Mitigated by configuring a multi-model chain
so `next=none` becomes `next=<model B>`; not eliminated."*

The mitigation was never actually configured. It is now, and the trigger was a
failing deploy rather than a review: a day of probes and deploys spent
`GenerateRequestsPerDayPerProjectPerModel-FreeTier` for `gemini-2.5-flash`, and
`scripts/gateway_preflight.py` failed `model serves a turn` with the gateway
reporting only `FailoverError: API rate limit reached`. With one model in the
provider entry there was nowhere to go — **the gateway cannot fail over to a model
its provider entry never declares**, so a `fallbacks` list alone would not have
been enough.

`scripts/gateway_seed.sh` now declares all four models on the provider and sets
`agents.defaults.model.fallbacks` to the three probed links in capability order.
The next deploy passed with `model serves a turn — gemini-3.6-flash answered`,
which is this ADR's behaviour, on the gateway, verified by an exhaustion nobody
staged.

**Two independent chains, deliberately.** `llm._FALLBACK_MODELS` serves
`extract()` and `extract_vision()`; the gateway's config serves chat turns. They
are not shared and cannot be: one is Python config, the other is the product's.
`test_chat_has_a_gemini_backend_and_the_agents_primary_is_the_reachable_provider`
asserts the gateway's list against `llm._FALLBACK_MODELS` so the two cannot drift
apart silently, and additionally that each fallback is declared on the provider —
the failure mode above.

**What is still true and still unfixed.** The gateway will keep wasting its
transient-retry budget on the exhausted model before moving, because its single
`rate_limit` classification cannot see the difference between per-minute and
per-day. Only the provider's own quota detail can: a per-day 429 carries a
`quotaId` containing `PerDay`. The app distinguishes them
(`llm._is_daily_budget_exhausted`, extended the same day to read Gemini's
spelling); the gateway does not, and that is a product limit rather than a
configuration one.
