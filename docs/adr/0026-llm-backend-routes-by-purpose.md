# ADR-0026: The LLM backend routes by purpose, not by a single global provider

**Date**: 2026-08-02
**Status**: proposed
**Deciders**: Justin

Amends the "one provider at a time" premise of ADR-0009. Does not supersede it —
provider-agnosticism and the OpenAI-compatible interface stand, and Groq stays
the default. What changes is that "the provider" becomes "the provider *for this
purpose*". ADR-0017's daily-budget walk and ADR-0010's vision cap are unaffected
in intent, but 0017's chain gains a provider dimension.

## Context

`config.LLM_PROVIDER` selects one provider for the whole process. Three things
made that untenable on 2026-08-02, all of them observed rather than predicted:

**One budget served every purpose, and chat drained it.** Groq's daily token
budget is per model, and measurement turns consumed `Limit 100000, Used 96708`.
Extraction and chat were on the same model, so conversational work starved the
pipeline. The two have nothing in common except a config key.

**Vision is already routed separately, but by hardcode.** `extract_vision()`
does not consult `_PROVIDERS` at all — it calls Gemini directly, because Gemini
was the only vision-capable backend configured. So the split this ADR describes
partly exists; it is just expressed as a special case in code rather than as
configuration.

**Where vision goes is a boundary problem, not a cost one.** Google's terms for
the unpaid Gemini quota, retrieved 2026-08-02, say Google uses submitted content
"to provide, improve, and develop" its models, that "human reviewers may read,
annotate, and process your API input and output", and "Do not submit sensitive,
confidential" data. `extract_vision()` sends scanned vet invoices there — name,
address, pet names, itemised amounts. Verified 2026-08-02, no vision model is
served by Groq at all (15 models, no `scout`, no `maverick`, no `-vl`), so there
is no in-provider alternative to move to.

A fourth force is structural rather than observed. `_FALLBACK_MODELS` is
`dict[provider] -> tuple[model]` and `_client` is a module global bound to one
`base_url`, so the fallback chain can only walk models **within** one provider.
The 2026-08-02 eval flagged this as single-point redundancy (axis 5c): a
provider-level 403 takes chat and extraction down together. Adding a second
provider to config does not fix it; the data structure has to change.

## Decision

**Provider and model are selected per call purpose, not per process.** `purpose`
— already an argument on every public function and already persisted to
`llm_calls` — becomes the routing key as well as the logging key. Fallback chain
entries become `(provider, model)` pairs so a chain may cross providers, and the
single `_client` becomes a cache keyed by base URL.

`config.LLM_PROVIDER` remains, as the default for any purpose without an explicit
mapping. No call site changes.

## Alternatives Considered

### Alternative 1: Leave it, and just change `LLM_PROVIDER` when a budget runs out
- **Pros**: zero code. Honest about how small this deployment is.
- **Cons**: manual, and the swap is global — moving extraction to a new provider
  also moves vision, which is the one that must not move to a training provider.
  Does nothing about the eval's single-point finding.
- **Why not**: the failure it must answer is unattended. The pipeline tick runs
  on cron with nobody watching; "Justin edits `.env`" is not a recovery path for
  a 3am extraction.

### Alternative 2: Route everything through OpenRouter and let it pick
- **Pros**: one integration, one key, 17 free models (verified 2026-08-02),
  logging opt-in and off by default, and only 2 of 80 providers train.
- **Cons**: model diversity without **account** diversity. Its rate limit is
  requests-per-day and **account-wide**, so extraction and vision would compete
  for the same bucket — reproducing exactly the failure this ADR exists to fix,
  one layer up. Its 429 body says `rate_limit_exceeded` and never `"tokens per
  day"`, so `_is_daily_budget_exhausted` would not match and the chain would
  **silently never walk**.
- **Why not**: it solves quota exhaustion and not isolation, and isolation is
  the property that was actually lost. OpenRouter remains a good *candidate
  provider* within the routing table — this rejects it as the whole answer, not
  as a participant.

### Alternative 3: Split `llm.py` into separate modules per purpose
- **Pros**: no shared state, no routing table, each purpose obviously independent.
- **Cons**: three copies of retry, backoff, rate limiting, `llm_calls` logging
  and error classification. Those are the parts with hard-won behaviour in them
  — the `reasoning`-field whitelist in `_assistant_turn` exists because echoing
  a model's own output field killed a live turn.
- **Why not**: it duplicates the code that must not drift to separate the
  configuration that must. A dict does the same job.

### Alternative 4: Local inference (Ollama) for everything
- **Pros**: no quota, no third party, perfect privacy — which answers the vision
  concern outright.
- **Cons**: measured on this machine 2026-08-02 — `GTX 1650 Ti Max-Q, 4096 MiB`.
  4 GB fits roughly a 3–4B model at Q4; an 8B Q4 spills to CPU. A turn is ~4.9k
  tokens against a 15-tool schema with up to four rounds.
- **Why not**: that is the class this project already rejected once. `qwen3.6-27b`
  was excluded from the fallback chain at 62 seconds as *"not a fallback — it's a
  hang with a reply at the end."* Revisit at 12–16 GB VRAM, not before.

## Consequences

### Positive
- A purpose's budget can be exhausted without taking the others down. This is the
  2026-08-02 failure, addressed directly.
- Vision can leave the unpaid Gemini quota without moving extraction with it.
- The fallback chain can cross providers, which is what the eval asked for.
- No call site changes — `purpose` is already threaded through all five.
- Chat leaves this table entirely at the gateway cutover, since the gateway
  configures its own model. The remaining table has two rows, not three.

### Negative
- Two or three provider accounts and keys to hold rather than one, and each is a
  separate thing that can expire, get rate-limited, or change its terms.
- Configuration can now be wrong in a new way: a purpose mapped to a provider
  that cannot serve it. Vision routed to a text-only model fails at call time,
  not at startup.
- `llm_calls` becomes harder to read as one budget. It was already two-place
  accounting once the gateway logs its own turns; this makes it three.

### Risks

- **Error classification is Groq-shaped and would silently stop working.**
  `_is_daily_budget_exhausted` matches `"tokens per day"` / `"(tpd)"`. A provider
  whose 429 says anything else produces a chain that never walks — and never says
  so. The eval flagged classification as too coarse (axis 5i) while it was still
  latent; purpose routing makes it load-bearing. **Mitigation**: per-provider
  classification, and a test asserting each configured provider's real 429 body
  is recognised. Not a shared regex.

- **`_last_model_used` is a module global and already racy.** A pipeline tick and
  a chat turn can overlap in one process. Today the consequence is a stale
  reading; with purpose routing it becomes a *wrong* one, and it is how Justin is
  told he got a fallback rather than the primary. **Mitigation**: return the model
  alongside the result rather than reading it off a global.

- **Vision quality is unproven on every candidate.** Wrong OCR means a wrong claim
  amount sent to an insurer — the one purpose where a quality failure is expensive
  rather than annoying. **Mitigation**: no vision provider is adopted until it is
  compared against `gemini-2.5-flash` on a real scanned invoice from the corpus,
  not a benchmark. ADR-0010's 3-attempt cap stays regardless.

- **A purpose mapping is configuration, so no test in the suite can see it.** This
  is the same class as the gateway config the preflight exists for. **Mitigation**:
  assert the mapping's shape in the hermetic suite (every purpose resolves to a
  known provider; the vision purpose resolves to a vision-capable one), and treat
  the provider's actual capability as a deploy-time check.

## Status note

**Proposed, not accepted.** The routing shape is settled; the vision provider is
not, and it is gated on two untested things recorded in
`docs/research/2026-08-02-free-llm-providers.md`: whether this account can obtain
a Cerebras key (its free tier was sold out for this account on 2026-07-23), and
whether any candidate's OCR holds up against a real invoice. Accepting this ADR
before those are answered would record a decision whose central choice is still
open. Tracked in `openspec/BACKLOG.md`.
