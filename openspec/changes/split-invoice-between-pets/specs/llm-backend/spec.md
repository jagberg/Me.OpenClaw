## MODIFIED Requirements

### Requirement: Non-silent failure
When the provider fails after retries, the layer SHALL raise an explicit error; callers MUST NOT swallow it into a silent no-op. Rate-limit (429) responses SHALL be retried with backoff before the error is raised.

Retries SHALL NOT be limited to 429. Any transport-level or transient HTTP failure (connection/timeout, 403 access-denied from the provider edge, 5xx) SHALL be retried with the same backoff, because a single such response is not evidence the provider is down. Only failures a retry cannot change SHALL fail fast: a per-day token cap (which switches model instead) and a request-shape 400 that is not a malformed tool call (retrying it burns budget to reproduce our own bug).

Rationale: on 2026-07-27 a single `403 Access denied. Please check your network settings.` ended a chat turn on its first attempt — `llm_calls` holds exactly one row for it — because the retry classifier recognised only 429 and malformed tool calls. The next identical request, seconds later, succeeded.

#### Scenario: Provider unavailable
- **WHEN** the provider returns errors after the configured retries
- **THEN** the layer raises an LLM-unavailable error, and the pipeline writes a human-readable reason to the claim flag / the chat replies with a visible failure message

#### Scenario: One-off 403 from the provider edge
- **WHEN** a request fails with a 403 or other transient HTTP/transport error
- **THEN** it is retried with backoff up to the configured retry count, and a subsequent success is returned normally

#### Scenario: Request-shape error is not retried
- **WHEN** a request fails with a 400 that is not a malformed tool call
- **THEN** it fails immediately with the provider's message, spending no further tokens

#### Scenario: Every attempt is logged
- **WHEN** a request is retried
- **THEN** each attempt writes its own `llm_calls` row, so the log shows how many attempts a failure actually got
