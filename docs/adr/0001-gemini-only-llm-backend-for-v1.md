# ADR-0001: Use Gemini 2.5 Flash (AI Studio free tier) as sole LLM backend for v1

**Date**: 2026-07-08
**Status**: superseded by ADR-0009
**Deciders**: Justin

## Context

Original plan (proposal.md) called for a hybrid backend: a local Ollama model for routine extraction with Claude Haiku/Sonnet cloud fallback for ambiguous cases, targeting <$5/mo. Justin has a Google AI Studio developer API key covering Gemini 2.5 Flash's free tier (separate from his paid Gemini Advanced consumer subscription, which he's cancelling) and wants to start there before investing in local-model tuning.

## Decision

v1 sends every extraction/chat request to Gemini 2.5 Flash via the AI Studio free tier. No local model, no multi-model routing/escalation logic exists yet — single backend only.

## Alternatives Considered

### Alternative 1: Ollama (local) + Claude Haiku/Sonnet (cloud fallback) — original plan
- **Pros**: no cloud dependency for routine cases, cost ceiling well understood
- **Cons**: needs GPU model tuning/benchmarking before it's usable; Claude fallback is paid from the first request
- **Why not**: more upfront work than justified before the core loop is even proven

### Alternative 2: Claude Haiku 4.5 only (no local, no Gemini)
- **Pros**: strong extraction quality, already integrated elsewhere in Justin's tooling
- **Cons**: paid immediately, no free tier
- **Why not**: Gemini's free tier covers expected volume at $0

## Consequences

### Positive
- $0/mo cost for v1 testing
- No GPU/model-benchmarking work blocking the core loop
- Simpler code — no confidence-based routing logic needed yet

### Negative
- Fully dependent on Gemini's availability — no fallback if it's down or rate-limited
- Model choice/quality unvalidated against real household-admin extraction tasks

### Risks
- Free-tier rate limit (15 req/min) could throttle a burst — mitigated with client-side queue/backoff
- Free-tier usage may be used by Google for model training — accepted for household-admin-level text, revisit if handling more sensitive content
