## Context

Greenfield build, no existing code. Runs on Justin's local machine: i7-10750H, 32GB RAM, GTX 1650 Ti (4GB VRAM), ~114GB free disk. Single user, local-only — no multi-tenant or remote-access requirements for v1. Stakeholders: Justin only. Depends on Google AI Studio (Gemini API, free tier) and Google OAuth/Gmail API (read-only). Justin is cancelling his Gemini Advanced consumer subscription; it was never usable here anyway — AI Studio API keys are a separate free product from that subscription.

## Goals / Non-Goals

**Goals:**
- Prove capture → schedule → follow-up-nudge loop end-to-end, driven by chat input and Gmail-sourced tasks.
- Run v1 extraction entirely on Gemini 2.5 Flash's free tier — $0/mo target while within its rate limits.
- Run entirely on the target hardware within its disk budget (no GPU/VRAM dependency in v1, since there's no local model yet).

**Non-Goals:**
- Local model inference (Ollama) — deferred to a follow-up change once the core loop is validated.
- Multi-model routing/escalation logic — v1 has exactly one LLM backend, no routing decision to make yet.
- Kids' activities tracking, medicine reminder dosing logic (future changes).
- Push notifications / mobile app — v1 surfaces tasks and reminders via a local web dashboard only.
- Multi-user auth, remote access, or public exposure of any service.
- Gmail write access (send/archive/label) — read-only for v1.

## Decisions

**Core service: Python + FastAPI + APScheduler + SQLite.**
Single-user local app, no need for a separate DB server. SQLite gives durable storage with zero ops overhead; APScheduler with an SQLite jobstore persists scheduled follow-ups across container restarts. FastAPI exposes the local dashboard and any internal HTTP endpoints. Alternative considered: Node/TypeScript — rejected only because Python has stronger Gmail API library support out of the box.

**LLM backend: Gemini 2.5 Flash via Google AI Studio free tier, single backend, no routing.**
Local model support (Ollama) is explicitly deferred — Justin wants to start with the free cloud tier now and evaluate local models later once the core loop is proven. This removes the need for confidence-based routing logic entirely for v1: every extraction request goes straight to Gemini. Alternative considered: Claude Haiku/Sonnet (Anthropic) as originally planned — rejected for v1 because it's paid from the first request, whereas Gemini 2.5 Flash's free tier covers expected household-admin volume at $0. Anthropic remains a candidate cloud option if/when a routing layer is built later.

**Rate limiting: respect the 15 requests/min free-tier cap with client-side throttling/backoff.**
The free tier caps at 15 req/min for Gemini 2.5 Flash. Household-admin task volume is low enough that this should rarely bind, but the client wrapper queues requests and backs off on `429` rather than dropping them, so a burst (e.g. several emails polled at once) degrades to a short delay instead of a failure.

**Email ingestion: polling, not Gmail push/watch.**
Gmail push notifications require a public HTTPS endpoint (Pub/Sub webhook), which this local-only deployment doesn't have. Polling every few minutes via the Gmail API (`history.list` / `messages.list`) is simpler, needs no exposed port, and is fast enough for household admin (not a real-time system). Dedupe on Gmail message ID stored in SQLite.

**Reminder delivery: local web dashboard (v1), not push notifications.**
The proposal doesn't specify a delivery channel, and building a push/notification integration (Telegram bot, Pushover, etc.) is a distinct chunk of scope. To keep v1 narrow, reminders and outstanding tasks surface on a small FastAPI-served dashboard Justin checks locally. Push notifications are a natural follow-up change once the core loop is validated.

**Deployment: Docker Compose, single `app` service + named volume.**
No `ollama` service in v1 — there's no local model to run, so no GPU passthrough or model volume needed either. This is simpler than originally planned and can be extended with an `ollama` service later without disrupting `app`. `app` container: FastAPI + scheduler + SQLite (mounted volume for the DB file).

## Risks / Trade-offs

- **Free-tier rate limit (15 req/min) could throttle a burst of email polling** → Mitigation: client-side queue/backoff on the Gemini wrapper; log `429`s to see how often this actually happens.
- **Free-tier usage may be used by Google for model training (unlike a paid tier)** → Mitigation: acceptable for household-admin text (tasks, email snippets) per Justin's call; revisit if handling anything more sensitive than "call painter" style content.
- **No local fallback means the assistant is fully dependent on Gemini's availability/API** → Mitigation: explicitly acceptable for now; local model is the documented next step, not a hard requirement for v1.
- **Polling-based Gmail ingestion adds latency (minutes, not seconds)** → Mitigation: acceptable for household admin use case; revisit with push/watch only if latency becomes a real complaint.
- **No delivery channel beyond a local dashboard means reminders can be missed if Justin doesn't check it** → Mitigation: explicitly deferred, flagged as a candidate follow-up change.
- **Local-only deployment (no auth) is fine on localhost but risky if the port is ever exposed** → Mitigation: bind FastAPI to `127.0.0.1` / Docker-internal network only, document as a hard constraint, not configurable via env var by accident.

## Migration Plan

No existing system — this is the initial deployment.
1. `docker compose up` brings up `app`.
2. Generate a Gemini API key in Google AI Studio (free tier) and add it to the local secrets file.
3. One-time interactive Google OAuth consent flow to obtain the Gmail read-only refresh token; store it in the same local secrets file (not committed, mounted into `app`).
4. Smoke test: manually capture a task via chat, confirm it's scheduled, confirm a test Gmail message surfaces as a candidate task on the dashboard.
5. Rollback: `docker compose down` — no external state to unwind since nothing outside Justin's machine is touched (Gmail access is read-only, Gemini free tier has no billing to unwind).

## Open Questions

- When to revisit local Ollama models — Justin's intent is "later, once this loop works," so this stays a follow-up change rather than a v1 task.
- Confidence/quality of Gemini 2.5 Flash on this extraction task hasn't been benchmarked yet — first real signal comes from actual usage once v1 is running.
- Whether a lightweight notification channel (e.g. desktop notification, Telegram) should pull forward from follow-up work if the dashboard-only approach proves easy to ignore in practice.
