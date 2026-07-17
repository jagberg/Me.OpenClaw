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
