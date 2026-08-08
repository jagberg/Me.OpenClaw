## Context

`llm.py` today, read rather than recalled (2026-08-08):

- `_PROVIDERS` maps a provider id to `(base_url, default_model, api_key)`.
- `_FALLBACK_MODELS` is keyed by provider and holds **bare model names**: `groq` → three Groq models, `gemini` → three Gemini models.
- `_client` is **a single module global**, built once from `config.LLM_PROVIDER`.
- `_completion` builds `chain = [model, *_FALLBACK_MODELS.get(config.LLM_PROVIDER, ())]` and walks it with **one** client.
- `_last_model_used` is a module global read back by `chat()`'s return.

So the shape is: one provider per process, and a chain that cannot leave it. That is exactly ADR-0017's design — per-model insurance — and exactly what 2026-08-06 defeated.

**One correction to the backlog entry that motivated this.** It says `_is_daily_budget_exhausted` "matches `tokens per day`/`(tpd)`, which is Groq-shaped; any other provider's body produces a chain that never walks". That is now out of date: the function also matches `perday`, which catches Gemini's `GenerateRequestsPerDayPerProjectPerModel-FreeTier`, and its docstring documents both providers from real captured responses. The classifier is *already* two-provider. It still needs to become explicitly per-provider rather than a union of substrings, but the work is smaller than the entry implies and the entry should be corrected rather than trusted.

## Goals / Non-Goals

**Goals:**

- A single call survives one provider's daily exhaustion.
- The same on the gateway side, whose chain is four Gemini models today.
- The downgrade stays visible, and names the provider.

**Non-Goals:**

- **Moving the primary.** `LLM_PROVIDER` stays `gemini`. This adds a floor.
- **Vision.** `extract_vision` stays Gemini-only; Groq serves no vision model at all. The ADR-0026 privacy exposure is untouched and stays open — this change must not be read as having addressed it.
- **Routing by purpose.** ADR-0026's concern, still `proposed`, orthogonal to this.
- **Retry/backoff policy.** Unchanged; only the *chain* changes.

## Decisions

**1. Key the client cache by base URL, not by provider id.**
Two provider ids could share a base URL (a re-pointed `groq` entry, an OpenAI-compatible proxy), and the client is a function of URL + key, not of the label. Alternative — a dict keyed by provider id — is simpler to read but wrong the first time two ids share an endpoint.

**2. `_FALLBACK_MODELS` becomes an ordered list of `(provider, model)` pairs, same-provider entries first.**
Alternative considered: keep it keyed by provider and append a separate `_CROSS_PROVIDER_TAIL`. Rejected — two structures for one ordered walk means two places to get the order wrong, and the order *is* the policy: cheap same-provider hops before an expensive provider switch.

**3. Return the model identity instead of reading a global.**
`_last_model_used` is already wrong under concurrency; crossing providers makes it wrong in a way a user would notice, because the disclosure is how Justin learns he got a weaker model. `_completion` returns `(message, used)` and `chat()` threads it. The global may stay temporarily as a deprecated alias if anything outside `llm.py` reads it — **check before removing**, and remove it in the same change rather than leaving both.

**4. Per-provider 429 classification, with the unrecognised case defaulting to "wait".**
If a body matches no known per-day pattern, treat it as per-minute. The asymmetry is deliberate: mistaking a per-minute limit for a daily one burns another provider's budget for nothing, while mistaking a daily one for per-minute costs a wait and is recoverable. Log the unrecognised body so the gap is visible rather than absorbed.

**5. The gateway gets the same chain, but through config, not code.**
`agents.defaults.model.fallbacks` in `gateway_seed.sh` gains Groq entries after the Gemini ones, and `models.providers.groq` is already declared there. Note the constraint that bit before: **the gateway cannot fail over to a model its provider entry never mentions**, so any model named in the chain must also be declared in the provider's `models` array.

**6. Verify against the real clients, not `urllib`.**
ADR-0028's rule. Any probe in this work uses the `openai` SDK from the app container and Node `fetch` from the gateway container.

## Risks / Trade-offs

- **A cross-provider hop changes answer quality silently.** → Disclosure is a requirement, not a nicety; the spec makes the provider part of the reported identity.
- **Exhausting the *second* provider too, in one bad day.** → Real, and not solved here. The chain gets longer, not infinite. Worth stating plainly: this converts "dead for a day" into "degraded for a day", which is the achievable win.
- **Testing daily exhaustion is awkward** — it needs a spent quota. → Test the classifier and the chain-walk against synthesised 429 bodies from the real captured responses in `_is_daily_budget_exhausted`'s docstring, rather than waiting for a real outage. The suite is hermetic and must stay so; no test may spend a token.
- **The gateway half needs a deploy to take effect**, so it lands separately from the app half and the two will disagree in between. → Sequence the app half first; it is the one with the live failure history.

## Migration Plan

1. App half: client cache, chain shape, returned identity, classifier. Tests against synthesised bodies.
2. Both suites, `ruff format` then `check`.
3. Gateway half: `gateway_seed.sh` chain + confirm every named model is declared in the provider entry.
4. Deploy, and confirm from `/health` and the gateway log that the chain is what was intended.
5. ADR amending 0017.

Rollback: revert the commit. No data or schema is touched, and no config default moves.

## Open Questions

1. **Does anything outside `llm.py` read `_last_model_used`?** Must be checked before it is removed, not assumed.
2. **Which Groq model belongs at the head of the cross-provider tail?** `llama-3.3-70b-versatile` is the declared default and the one probed live on 2026-08-07, so it is the obvious first choice — but no quality comparison against Gemini's lite models has been run, and the chain's order is a quality decision as much as an availability one.
3. Should the gateway and the app share the chain definition? They are separately configured today, and unifying them is a larger change than this one.
