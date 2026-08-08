## Why

On 2026-08-06 typed chat answered "API rate limit reached" for most of a day. Taps kept working — they never reach a model — so nothing looked broken. The cause was not a bug: the gateway heartbeat spent Gemini's free-tier daily quota, and **every link in the fallback chain was a Gemini model**, so there was nowhere to fall through to.

ADR-0017's chain is *per-model* insurance. It was designed for one model's daily budget running out while a sibling model's budget survives. It cannot help when the exhaustion is provider-wide, and `docs/failure-modes.md` has listed that as a standing gap since 2026-07-28.

Two things changed that make this buildable now rather than later:

- **ADR-0028** established that Groq is reachable from both runtimes with the clients they actually use. The "second provider" this needs already exists, already has a key, and needs no new account.
- The heartbeat is disabled, so the immediate bleed is stopped — which means this is prevention, not firefighting, and can be done carefully.

The heartbeat fix removed *one* consumer of the quota. It did not make the quota survivable. Any future exhaustion — a busy day, a re-enabled heartbeat, a chatty week — reproduces 2026-08-06 exactly.

## What Changes

- **`llm._client` becomes a dict keyed by base URL.** This is the load-bearing change: a single module-global client is what pins the process to one provider, and it is why adding a provider to config does not currently buy anything.
- **`_FALLBACK_MODELS` entries become `(provider, model)` pairs** rather than bare model names, so a chain can cross a provider boundary.
- **The chain gains a cross-provider tail.** When every model of the primary provider is exhausted, the next candidate is another provider's model rather than the end of the list.
- **`_last_model_used` stops being a module global** and is returned through the call path. With routing across providers it goes from merely stale to actively wrong, and it is how Justin learns he got a downgrade.
- **The gateway's chain gets the same treatment** — `agents.defaults.model.fallbacks` in `scripts/gateway_seed.sh` is four Gemini models today, which is the same single-provider shape on the other runtime.
- **Disclosure is preserved across the provider boundary.** ADR-0017 requires the downgrade be visible; a cross-provider downgrade is a larger change in behaviour than a cross-model one, so it must not become quieter.

**Not a config change to the primary.** `LLM_PROVIDER` stays `gemini`. This adds a floor, it does not move the default.

## Capabilities

### New Capabilities

None. This deepens an existing capability rather than adding one.

### Modified Capabilities

- `llm-backend`: the fallback chain SHALL be able to cross providers, not only models within one provider, and the model actually used SHALL be reported accurately when it does.

## Impact

- **Code**: `app/openclaw/llm.py` — `_client`, `_resolve`, `_FALLBACK_MODELS`, `_completion`, `_try_model`, `_last_model_used`. Roughly 40 lines; **no call-site changes**, since `purpose` is already an argument at all five call sites.
- **Config**: `scripts/gateway_seed.sh` for the gateway's own chain. Requires a deploy to take effect.
- **ADR**: 0017 is amended, not superseded — its per-model reasoning stays correct and stays the first resort. Worth its own ADR because it changes what the word "fallback" means in 0017.
- **Not touched**: `extract_vision`, which is Gemini-only because Groq serves no vision model at all (verified 2026-08-02). The privacy exposure recorded against the unpaid Gemini quota in ADR-0026 is unchanged by this and stays open.
- **Relationship to ADR-0026**: that ADR routes by *purpose* and is still `proposed`, gated on a vision provider. This change is orthogonal — it makes any single purpose survivable — and does not resolve or block it.
