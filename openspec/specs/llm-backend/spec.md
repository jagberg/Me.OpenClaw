# llm-backend Specification

## Purpose
One seam for every LLM call in OpenClaw. `app/openclaw/llm.py` exposes `chat()`, `extract()` and `extract_vision()`; no other module imports a provider SDK — the sole exception is `gemini.py`, which *is* the Gemini implementation sitting behind that seam.

See ADR-0009 (provider-agnostic backend, supersedes ADR-0001) and ADR-0010 (vision-OCR fallback).

## Requirements

### Requirement: Provider-agnostic LLM interface
The system SHALL expose a single module (`llm`) providing `chat(messages, ...)`, `extract(prompt, purpose)` and `extract_vision(prompt, image_jpeg, purpose)` that all LLM callers use, independent of which provider is configured. Callers MUST NOT import a provider SDK directly.

#### Scenario: Existing extraction callers use the shared interface
- **WHEN** `vet_detection`, `invoice_matching`, or `tasks` needs an LLM extraction
- **THEN** it calls `llm.extract(prompt, purpose=...)` and receives the model's text, with no reference to any provider-specific client

#### Scenario: Chat callers use the shared interface
- **WHEN** the conversational agent needs a completion
- **THEN** it calls `llm.chat(messages, tools=...)` and receives the model's response, with no reference to any provider-specific client

### Requirement: Configurable provider and model
The provider and model SHALL be selected by environment variable, defaulting to Groq `llama-3.3-70b-versatile`. Switching to another OpenAI-compatible provider MUST require only configuration changes, not code changes.

**Backend history — the abstraction absorbing exactly what it was built for:**
- Gemini was the original single hard-wired backend (ADR-0001), abandoned when its free tier proved to be ~20 requests/day in practice. It remains selectable (`LLM_PROVIDER=gemini`) as an `extract()`-only rollback path.
- Cerebras `gpt-oss-120b` was chosen as the first default for its larger free budget, then **removed from the code entirely on 2026-07-23**: its free inference tier is sold-out for this account and every free model returns `402 payment_required` (verified live). It is no longer a selectable provider — the earlier spec text saying it "stays selectable for when capacity returns" no longer describes the code.
- Groq is the working default. OpenAI is the other configured option.

#### Scenario: Default provider
- **WHEN** no LLM provider env var is set
- **THEN** the system uses the Groq provider with model `llama-3.3-70b-versatile`

#### Scenario: Switching provider by config
- **WHEN** `LLM_PROVIDER` is set to a supported OpenAI-compatible provider and its API key is present
- **THEN** all `chat()`/`extract()` calls route to that provider without any code change

#### Scenario: Unknown provider
- **WHEN** `LLM_PROVIDER` names a provider the layer does not implement
- **THEN** the layer raises an LLM-unavailable error naming the valid choices, rather than silently falling back

#### Scenario: Missing API key
- **WHEN** the configured provider has no API key
- **THEN** `chat()`/`extract()` raise a non-silent error identifying the missing key, and the caller surfaces it (dashboard flag / chat error reply) rather than proceeding

### Requirement: Vision extraction is Gemini regardless of configured provider
`extract_vision()` SHALL route to Gemini whatever `LLM_PROVIDER` is set to, because it is the only configured backend with a vision-capable model (verified: this Groq account exposes none). A Gemini-specific unavailability MUST be re-raised as the layer's own unavailable error so callers handle one failure type.

#### Scenario: Vision OCR while running on Groq
- **WHEN** `LLM_PROVIDER=groq` and a scanned invoice needs vision OCR
- **THEN** the call is served by Gemini and the caller sees no provider-specific behaviour

### Requirement: Chat requires an OpenAI-compatible provider
`chat()` SHALL refuse to run on the legacy Gemini backend rather than degrade, since that backend implements `extract()` only.

#### Scenario: Chat attempted on the Gemini backend
- **WHEN** `LLM_PROVIDER=gemini` and `chat()` is called
- **THEN** it raises an LLM-unavailable error saying chat needs an OpenAI-compatible provider

### Requirement: Rate limiting and call logging
The LLM layer SHALL apply a client-side rate limit matched to the configured provider's free-tier limits and SHALL record every call in `llm_calls` (purpose, success, latency, error), preserving the existing observability.

#### Scenario: Rate limit respected
- **WHEN** calls arrive faster than the configured provider's per-minute limit
- **THEN** the layer queues/backs off rather than emitting requests that would be rejected

#### Scenario: Every call is logged
- **WHEN** any `chat()`, `extract()` or `extract_vision()` call completes or fails
- **THEN** a row is written to `llm_calls` with its purpose, success flag, latency, and error text (if any)

### Requirement: Non-silent failure
When the provider fails after retries, the layer SHALL raise an explicit error; callers MUST NOT swallow it into a silent no-op. Rate-limit (429) responses SHALL be retried with backoff before the error is raised.

#### Scenario: Provider unavailable
- **WHEN** the provider returns errors after the configured retries
- **THEN** the layer raises an LLM-unavailable error, and the pipeline writes a human-readable reason to the claim flag / the chat replies with a visible failure message
