# Tasks

App half first — it has the live failure history. The gateway half needs a deploy, so the two runtimes disagree in between; that is expected, not a fault.

No test may spend a token. The suite is hermetic (LLM keys force-blanked) and stays that way: daily exhaustion is tested against synthesised 429 bodies taken from the real captured responses in `_is_daily_budget_exhausted`'s docstring.

## 1. Client cache

- [ ] 1.1 Replace the `_client` module global with a dict keyed by **base URL** (not provider id — two ids can share an endpoint, and the client is a function of URL + key)
- [ ] 1.2 `_resolve()` takes a provider argument instead of always reading `config.LLM_PROVIDER`, defaulting to it
- [ ] 1.3 Confirm two providers' clients can coexist in one process without either being rebuilt

## 2. Chain shape

- [ ] 2.1 `_FALLBACK_MODELS` entries become `(provider, model)` pairs in one ordered list, same-provider hops first, cross-provider tail last — one structure, because the order *is* the policy
- [ ] 2.2 `_completion` walks the pairs, fetching the right client per candidate
- [ ] 2.3 The primary is not duplicated in the chain when it also appears in the tail
- [ ] 2.4 Exhausting every provider raises `LLMUnavailableError` naming the exhaustion — never an empty string that reads like an answer

## 3. Reported model identity

- [ ] 3.1 **Check whether anything outside `llm.py` reads `_last_model_used`** before touching it — grep, do not assume
- [ ] 3.2 `_completion` returns the model that answered rather than setting a module global
- [ ] 3.3 The reported identity names the **provider** as well as the model
- [ ] 3.4 Remove the global in this change rather than leaving both; if an external reader exists, update it here
- [ ] 3.5 Confirm the ADR-0017 downgrade disclosure still reaches the user, and reads sensibly for a provider switch

## 4. Per-provider 429 classification

- [ ] 4.1 Make the classifier per-provider rather than a union of substrings. **Correct the BACKLOG entry while here** — it says the classifier is "Groq-shaped", but `perday` already catches Gemini's `GenerateRequestsPerDayPerProjectPerModel-FreeTier`, so the entry overstates the work
- [ ] 4.2 An unrecognised 429 body is treated as **per-minute**, deliberately — mistaking per-minute for daily burns another provider's budget for nothing, while the reverse costs only a wait
- [ ] 4.3 Log an unrecognised body so the gap is visible rather than absorbed
- [ ] 4.4 Tests for both providers' real body shapes, and for the unrecognised case

## 5. App half verification

- [ ] 5.1 A synthesised Gemini per-day 429 walks the chain into Groq and returns an answer
- [ ] 5.2 A per-minute 429 does **not** advance to another provider
- [ ] 5.3 Both suites: `tests/test_core.py` **and** `tests/test_telegram.py`
- [ ] 5.4 `ruff format` then `ruff check` clean
- [ ] 5.5 One live call through the real client from inside the app container, per ADR-0028 — the `openai` SDK, never `urllib`

## 6. Gateway half (needs a deploy)

- [ ] 6.1 Add Groq entries after the Gemini ones in `agents.defaults.model.fallbacks` in `scripts/gateway_seed.sh`
- [ ] 6.2 Confirm **every model named in the chain is declared** in `models.providers.<p>.models` — the gateway cannot fail over to a model its provider entry never mentions, and that trap has already cost one deploy
- [ ] 6.3 Deploy from the deploy worktree with `./scripts/deploy.ps1` (which now polls for health rather than sleeping 15s)
- [ ] 6.4 Confirm the live chain from `/health` and the gateway log, not from the seed script's intent
- [ ] 6.5 One live cross-provider turn through Node `fetch` from the gateway container

## 7. Record

- [ ] 7.1 ADR amending ADR-0017 — per-model insurance stays the first resort and stays correct; this is the floor beneath it. Note that 2026-08-06 is its motivating incident
- [ ] 7.2 Update `docs/failure-modes.md`, which has listed single-provider redundancy as a standing gap since 2026-07-28
- [ ] 7.3 Close the BACKLOG entry "The model fallback chain cannot survive a provider-level block" — it is already half-corrected by ADR-0028; this closes the other half
- [ ] 7.4 State plainly in the ADR what this does **not** buy: exhausting both providers in one day is still possible. This converts "dead for a day" into "degraded for a day"
- [ ] 7.5 Sync delta specs into `openspec/specs/` before archiving
