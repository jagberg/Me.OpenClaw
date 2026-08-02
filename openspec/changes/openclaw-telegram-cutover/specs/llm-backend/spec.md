## MODIFIED Requirements

### Requirement: Provider-agnostic LLM interface
The system SHALL expose a single module (`llm`) providing `extract(prompt, purpose)` and `extract_vision(prompt, image_jpeg, purpose)` that all extraction callers use, independent of which provider is configured. Callers MUST NOT import a provider SDK directly.

Conversational completions are no longer the app's concern: the gateway agent holds the chat loop and resolves its own model. `llm` therefore covers the extraction paths only — `vet_detection`, `invoice_matching`, `tasks` — and remains the sole seam for them.

#### Scenario: Existing extraction callers use the shared interface
- **WHEN** `vet_detection`, `invoice_matching`, or `tasks` needs an LLM extraction
- **THEN** it calls `llm.extract(prompt, purpose=...)` and receives the model's text, with no reference to any provider-specific client

#### Scenario: No provider SDK leaks into a caller
- **WHEN** any module other than `llm` and `gemini` is inspected
- **THEN** it imports no provider SDK

### Requirement: An exhausted daily budget falls through to another model
Groq's daily token limit is per model, so when one model's budget is spent the extraction path SHALL try the next model in a configured chain rather than failing the request. Daily exhaustion gets **one attempt per model** — retrying cannot free a daily cap — and MUST be distinguished from the per-minute limit, which keeps its backoff-and-retry against the same model. When every model's budget is spent the failure SHALL say so plainly, including that the window is rolling.

This requirement SHALL remain implemented in `llm.py` for the extraction paths and SHALL NOT be assumed to be inherited from the gateway. The gateway's documented fallback triggers on rate-limit responses only; a per-day token cap is a distinct condition it does not treat as failover. Before relying on the gateway for chat-side resilience, a real Groq daily-exhaustion response SHALL be captured and checked against what the gateway actually classifies as retryable; if it does not switch models, the gap SHALL be recorded rather than assumed benign.

The reply SHALL disclose which model answered whenever it is not the configured primary: a quietly weaker answer is the invisible failure the hard rules forbid. Every model in the chain SHALL be verified end-to-end against the real tool schema before being relied on. See ADR-0017.

#### Scenario: Primary model's daily budget is spent
- **WHEN** an extraction fails with a per-day token limit for the configured model
- **THEN** the layer retries on the next model in the chain, and that model's answer is returned with a note naming it

#### Scenario: Per-minute limit, not per-day
- **WHEN** a request fails on the per-minute token limit
- **THEN** the layer backs off and retries the SAME model, and does not switch models

#### Scenario: Every budget spent
- **WHEN** no model in the chain has daily budget left
- **THEN** an LLM-unavailable error names the exhausted budgets and states that the window is rolling

#### Scenario: Gateway's classification is verified, not assumed
- **WHEN** the gateway's model chain is configured for the chat agent
- **THEN** a captured daily-exhaustion response is checked against its failover classification, and any failure to switch models is recorded as a known gap

### Requirement: Rate limiting and call logging
The extraction path SHALL apply a client-side rate limit matched to the configured provider's free-tier limits and SHALL record every call in `llm_calls` (purpose, success, latency, error).

Chat-turn calls made by the gateway will not appear in `llm_calls`. This is an accepted observability split, and it SHALL be explicit: the single-table view of every LLM call the project has today does not survive the swap. Where the gateway keeps its own call records, they SHALL be locatable, and the split SHALL be documented rather than discovered when a token-spend question cannot be answered.

#### Scenario: Rate limit respected
- **WHEN** extraction calls arrive faster than the configured provider's per-minute limit
- **THEN** the layer queues/backs off rather than emitting requests that would be rejected

#### Scenario: Every extraction call is logged
- **WHEN** any `extract()` or `extract_vision()` call completes or fails
- **THEN** a row is written to `llm_calls` with its purpose, success flag, latency, and error text (if any)

#### Scenario: Accounting for total spend
- **WHEN** total LLM spend is questioned
- **THEN** both `llm_calls` and the gateway's own records are named as the two places to look

## REMOVED Requirements

### Requirement: Chat requires an OpenAI-compatible provider
**Reason**: `llm.chat()` had exactly one caller (`agent.py:679`), and the agent's tool loop moves to the gateway. With no chat caller left in Python, a requirement about which provider `chat()` may run on has nothing to constrain.

**Migration**: Chat provider selection is configured on the gateway agent (`agents.defaults.model` with an ordered fallback chain). `LLM_PROVIDER=gemini` remains an `extract()`-only rollback path, which was always its real role — the removed requirement existed to stop `chat()` degrading onto it.

### Requirement: Only input-valid fields are replayed into the tool loop
**Reason**: This constrained `llm.chat()`'s replay of an assistant tool-call turn. The loop it protected no longer exists in this codebase.

**Migration**: The hazard transfers rather than disappears — reasoning-capable models still emit output-only fields (`reasoning`) that the API rejects on input, which killed live turns once the fallback chain could reach such a model. The gateway now owns the replay, so this SHALL be verified against a reasoning-capable model in its chain before that model is relied on, and any recurrence recorded as a gateway defect rather than re-fixed here.
