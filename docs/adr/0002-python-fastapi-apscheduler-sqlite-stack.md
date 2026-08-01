# ADR-0002: Python/FastAPI/APScheduler/SQLite core stack, single Docker Compose service

**Date**: 2026-07-08
**Status**: accepted
**Deciders**: Justin

## Context

Greenfield build, single user, local-only deployment on Justin's machine. Needs durable task/reminder storage and a scheduler that survives restarts, plus a way to expose a dashboard.

## Decision

Core service is Python + FastAPI (dashboard/HTTP) + APScheduler with a SQLite jobstore (restart-safe scheduling) + SQLite (task/reminder/log storage). Deployed as a single `app` Docker Compose service (no separate `ollama` service in v1, per ADR-0001).

## Alternatives Considered

### Alternative 1: Node/TypeScript stack
- **Pros**: strong async ecosystem
- **Cons**: weaker out-of-the-box Gmail API and Ollama-adjacent tooling
- **Why not**: Python's library support for this specific integration set is stronger

### Alternative 2: Postgres instead of SQLite
- **Pros**: better concurrent-write handling, more familiar ops story
- **Cons**: needs a separate DB server/container for a single-user local app
- **Why not**: no concurrency requirement justifies the added ops overhead

## Consequences

### Positive
- Zero extra infra — one container, one DB file
- APScheduler + SQLite jobstore gives restart-safe reminders for free

### Negative
- SQLite limits future multi-writer/concurrent-access scenarios if this ever stops being single-user

### Risks
- None significant at current scale

---

## Addendum — 2026-08-01: the "single Docker Compose service" half is superseded

**Superseded by:** ADR-0024. **Still holds:** Python, FastAPI, SQLite and the
claims domain — none of that moves.

**What changed:** the stack is now two runtimes, the Python service and the
OpenClaw gateway (Node), each in its own container. The original reasoning is
kept below rather than rewritten, because it was correct for what it decided and
the cost of leaving it behind should be visible.

What that cost actually is:

- **Two configurations.** `.env` already diverged between the main checkout and
  the deploy worktree before any of this; a second runtime with its own config
  doubles that surface.
- **Two versions.** `app_version` can no longer stand for "the code that
  produced this row" on its own — see the ADR-0014 addendum.
- **A deploy that can half-succeed.** One command still brings both up, and a
  partial start must report failure rather than success.

APScheduler is also displaced: the 15-minute tick, Gmail ingest and the daily
nudge become gateway cron entries invoking the app. `reminders.py` goes with it
(`cron --at` covers the misfire behaviour it was written for). `tasks.py` stays.
