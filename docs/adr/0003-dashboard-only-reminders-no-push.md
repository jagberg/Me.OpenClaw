# ADR-0003: Reminder delivery via local web dashboard only (no push notifications) for v1

**Date**: 2026-07-08
**Status**: accepted
**Deciders**: Justin (default chosen to keep momentum; not explicitly requested)

## Context

The original proposal didn't specify how reminders reach Justin. Building a push/notification channel (Telegram bot, Pushover, desktop notification) is a distinct chunk of scope.

## Decision

v1 surfaces due reminders and open tasks on a small FastAPI-served local dashboard that Justin checks manually. No outbound notification channel exists yet.

## Alternatives Considered

### Alternative 1: Telegram bot / Pushover push notifications
- **Pros**: reminders reach Justin actively, no need to remember to check a dashboard
- **Cons**: separate integration, auth, and delivery-reliability surface to build
- **Why not**: out of scope for proving the core capture→schedule→follow-up loop

## Consequences

### Positive
- Keeps v1 scope narrow, nothing to build/maintain beyond the existing FastAPI app

### Negative
- Reminders can be silently missed if Justin doesn't check the dashboard

### Risks
- Dashboard-only approach may prove unusable in practice — flagged as the first candidate follow-up change if so
