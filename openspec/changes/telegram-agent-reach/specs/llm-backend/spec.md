## ADDED Requirements

### Requirement: An exhausted daily budget falls through to another model
Groq's daily token limit is per model, so when one model's budget is spent the layer SHALL try the next model in a configured chain rather than failing the request. Daily exhaustion gets **one attempt per model** — retrying cannot free a daily cap — and MUST be distinguished from the per-minute limit, which keeps its backoff-and-retry against the same model. When every model's budget is spent the failure SHALL say so plainly, including that the window is rolling.

The reply SHALL disclose which model answered whenever it is not the configured primary: a quietly weaker answer is the invisible failure the hard rules forbid. Every model in the chain SHALL be verified end-to-end against the real tool schema before being relied on. See ADR-0017, which supersedes ADR-0009's manual-swap mitigation for quota walls.

#### Scenario: Primary model's daily budget is spent
- **WHEN** a request fails with a per-day token limit for the configured model
- **THEN** the layer retries on the next model in the chain, and that model's answer is returned with a note naming it

#### Scenario: Per-minute limit, not per-day
- **WHEN** a request fails on the per-minute token limit
- **THEN** the layer backs off and retries the SAME model, and does not switch models

#### Scenario: Every budget spent
- **WHEN** no model in the chain has daily budget left
- **THEN** an LLM-unavailable error names the exhausted budgets and states that the window is rolling

#### Scenario: Primary answers
- **WHEN** the configured model answers normally
- **THEN** the reply carries no model annotation

### Requirement: Only input-valid fields are replayed into the tool loop
When the assistant's tool-call turn is fed back into the conversation, the layer SHALL send only the fields the API accepts as input (role, content, tool_calls) — not every field the provider emitted.

Reasoning-capable models return output-only fields (`reasoning`) that the API rejects on input, which killed live turns once the fallback chain could reach such a model. This is a whitelist rather than a `reasoning` blacklist so the next output-only field a model invents cannot reproduce it.

#### Scenario: A reasoning-capable model makes a tool call
- **WHEN** the model's reply carries provider-specific output fields alongside its tool calls
- **THEN** the replayed turn contains only role, content and tool_calls, and the next request succeeds
