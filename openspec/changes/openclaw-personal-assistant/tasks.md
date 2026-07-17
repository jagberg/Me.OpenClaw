## 1. Project & Docker Setup

- [x] 1.1 Scaffold Python project (FastAPI app) with dependency management (uv/poetry/pip-tools)
- [x] 1.2 Write `docker-compose.yml` with a single `app` service and a named volume for the SQLite DB
- [x] 1.3 Bind `app`'s HTTP port to `127.0.0.1` only (no external exposure)
- [x] 1.4 Add `.env`/secrets file convention for the Gemini (Google AI Studio) API key and Gmail OAuth credentials, excluded from git

## 2. Storage & Scheduling Foundation

- [x] 2.1 Define SQLite schema: `tasks`, `reminders`, `llm_calls`, `processed_emails` tables
- [x] 2.2 Wire APScheduler with SQLite jobstore for durable, restart-safe scheduling
- [x] 2.3 Write migration/init script to create schema on first run

## 3. LLM Extraction (Gemini)

- [x] 3.1 Generate a Gemini API key via Google AI Studio (free tier) — Justin added it to `app/.env`
- [x] 3.2 Implement Gemini 2.5 Flash client wrapper for extraction requests
- [x] 3.3 Implement client-side throttling/backoff to stay within the 15 req/min free-tier limit, retrying on `429` instead of dropping requests
- [x] 3.4 Log every call (timestamp, success/failure, latency) to `llm_calls`
- [x] 3.5 Surface a clear failure (not a silent drop) when Gemini is unreachable or quota is exhausted

## 4. Task Capture

- [x] 4.1 Implement chat-input endpoint that creates a task record via the LLM extraction pipeline
- [x] 4.2 Implement optional follow-up date extraction on capture, wired to reminder-scheduling
- [x] 4.3 Implement outcome-logging endpoint that records outcome text/timestamp and updates task status
- [x] 4.4 Implement candidate-task intake from email-ingestion, linked to source Gmail message ID

## 5. Reminder Scheduling

- [x] 5.1 Implement reminder creation tied to a task ID and scheduled date/time
- [x] 5.2 Implement scheduled job that marks a reminder `due` when its time arrives
- [x] 5.3 Verify reminders survive an `app` container restart (fire once, no loss/duplication)

## 6. Email Ingestion

- [ ] 6.1 Implement Google OAuth read-only consent flow, store refresh token locally — code written (`scripts/gmail_auth.py`), but running it needs Justin's Google Cloud OAuth client `credentials.json` and an interactive browser consent
- [x] 6.2 Implement Gmail polling job (configurable interval) using `messages.list`/`history.list`
- [x] 6.3 Implement dedupe against `processed_emails` by Gmail message ID
- [x] 6.4 Implement candidate-task extraction from qualifying emails, handed to task-capture

## 7. Dashboard

- [x] 7.1 Build minimal FastAPI-served dashboard page listing open tasks and due reminders
- [x] 7.2 Add simple view for recording an outcome against a task from the dashboard

## 8. End-to-End Verification

- [x] 8.1 Smoke test: capture a task via chat, confirm it schedules a reminder correctly — verified live against real Gemini API: "call painter... follow up Friday" correctly extracted a follow-up date and scheduled a reminder
- [ ] 8.2 Smoke test: send a test email, confirm it surfaces as a candidate task on the dashboard — **needs a real Gmail token (blocked on 6.1)**
- [x] 8.3 Verify `docker compose up`/`down` cycle preserves all data (tasks, reminders, processed emails) — verified via process-restart check (schedule → exit → fresh process resumes → reminder fires), same persistence path Compose restarts use
- [x] 8.4 Review `llm_calls` log after a test session to confirm call volume stays within the free-tier rate limit and no calls were silently dropped — 4 live calls logged, all succeeded, well under the 15/min cap
