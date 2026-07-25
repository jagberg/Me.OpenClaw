## ADDED Requirements

### Requirement: Provider-agnostic LLM interface

The system SHALL expose a single module (`llm`) providing `chat(messages, ...)` and `extract(prompt, purpose)` functions that all LLM callers use, independent of which provider is configured. Callers MUST NOT import a provider SDK directly.

#### Scenario: Existing extraction callers use the shared interface

- **WHEN** `vet_detection`, `invoice_matching`, or `tasks` needs an LLM extraction
- **THEN** it calls `llm.extract(prompt, purpose=...)` and receives the model's text, with no reference to any provider-specific client

#### Scenario: Chat callers use the shared interface

- **WHEN** the conversational agent needs a completion
- **THEN** it calls `llm.chat(messages, tools=...)` and receives the model's response, with no reference to any provider-specific client

### Requirement: Configurable provider and model

The provider and model SHALL be selected by environment variable, defaulting to Groq `llama-3.3-70b-versatile`. Switching provider (Groq, Cerebras, OpenAI, or the legacy Gemini) MUST require only configuration changes, not code changes, for any OpenAI-compatible provider.

> Note: the default was originally Cerebras `gpt-oss-120b` (larger free budget), but Cerebras' free inference tier is sold-out for this account — every free model returns `402 payment_required` (verified live, tasks.md 5.1). Groq is the working default; Cerebras stays selectable for when capacity returns. This is the single-provider-failure the abstraction was built to absorb.

#### Scenario: Default provider

- **WHEN** no LLM provider env var is set
- **THEN** the system uses the Groq provider with model `llama-3.3-70b-versatile`

#### Scenario: Switching provider by config

- **WHEN** `LLM_PROVIDER` is set to a supported OpenAI-compatible provider and its API key is present
- **THEN** all `chat()`/`extract()` calls route to that provider without any code change

#### Scenario: Missing API key

- **WHEN** the configured provider has no API key
- **THEN** `chat()`/`extract()` raise a non-silent error identifying the missing key, and the caller surfaces it (dashboard flag / chat error reply) rather than proceeding

### Requirement: Rate limiting and call logging

The LLM layer SHALL apply a client-side rate limit matched to the configured provider's free-tier limits and SHALL record every call in `llm_calls` (purpose, success, latency, error), preserving the existing observability.

#### Scenario: Rate limit respected

- **WHEN** calls arrive faster than the configured provider's per-minute limit
- **THEN** the layer queues/backs off rather than emitting requests that would be rejected

#### Scenario: Every call is logged

- **WHEN** any `chat()` or `extract()` call completes or fails
- **THEN** a row is written to `llm_calls` with its purpose, success flag, latency, and error text (if any)

### Requirement: Non-silent failure

When the provider fails after retries, the layer SHALL raise an explicit error; callers MUST NOT swallow it into a silent no-op.

#### Scenario: Provider unavailable

- **WHEN** the provider returns errors after the configured retries
- **THEN** the layer raises an LLM-unavailable error, and the pipeline writes a human-readable reason to the claim flag / the chat replies with a visible failure message
