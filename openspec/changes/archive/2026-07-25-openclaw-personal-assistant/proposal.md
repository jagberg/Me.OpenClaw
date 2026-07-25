## Why

Justin wants a personal assistant ("OpenClaw") that offloads recurring household admin — chasing tradespeople, tracking kids' activities and medicine schedules, and reading email for actionable items — instead of holding it all in his head or scattered notes. This is a fresh project (empty repo) with no existing code to build on.

## What Changes

- Stand up a local Docker-based service that runs a core task/reminder loop: capture a task (e.g. "call painter"), schedule a follow-up, log outcomes (who was spoken to, what was said).
- LLM backend for v1: Google Gemini 2.5 Flash via the Google AI Studio free tier (a developer API key, separate from the paid Gemini Advanced consumer subscription) for all task extraction/parsing. Cost target: $0/mo while staying within free-tier rate limits (15 requests/min). Local model (Ollama) and multi-model routing are deferred to a follow-up change once this loop is validated — see design.md.
- Gmail read-only integration (OAuth) as the first email source, feeding the task-extraction pipeline.
- v1 scope is deliberately narrow: build and prove the core capture → schedule → follow-up-nudge loop only. Kids' activities and medicine reminders are explicitly deferred to follow-up changes once this loop is validated.

## Capabilities

### New Capabilities
- `task-capture`: Turn a described task (from chat or email) into a stored task with optional follow-up scheduling and outcome logging.
- `reminder-scheduling`: Schedule and fire follow-up reminders/nudges tied to a task.
- `email-ingestion`: Read-only Gmail polling/watch that surfaces candidate tasks for the task-capture pipeline.
- `llm-extraction`: Send an extraction/chat request to Google Gemini (AI Studio free tier) and track usage against the free-tier rate limit.

### Modified Capabilities
(none — greenfield project, no existing specs)

## Impact

- New Docker Compose stack on Justin's local machine (i7-10750H, 32GB RAM, GTX 1650 Ti 4GB VRAM, ~114GB free disk).
- New external dependencies: Google AI Studio API (Gemini 2.5 Flash, free tier) for extraction, Google OAuth + Gmail API (read-only scope) for email ingestion. No Anthropic API and no local Ollama runtime in v1 — both deferred.
- No existing code/systems affected — this is the initial build.
- Explicitly out of scope for this change: kids' activities tracking, medicine reminder dosing logic — captured as future work, not designed here.
