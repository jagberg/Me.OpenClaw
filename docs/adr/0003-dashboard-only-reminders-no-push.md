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

---

## Amendment (2026-08-01) — the original reason has expired; the decision has not been retaken

This ADR's reasoning was that no push channel existed. **That is no longer
true.** Telegram has been a live outbound channel since the bot shipped, and
under ADR-0024 the gateway owns push across 25+ channels plus cron.

So the constraint that produced "dashboard only" is gone. What has *not*
happened is anyone deciding to keep or change the behaviour on its merits — the
status quo is currently held up by a reason that has expired.

Recorded here so the next reader meets a live question rather than a settled
decision. Justin's call, 2026-08-01: reminders-and-push is a **separate change
after** the gateway swap — independent of the transport work, and trivial once
the gateway exists, so folding it in would buy nothing but scope. Tracked in
`openspec/BACKLOG.md`.

Not superseded, because nothing has replaced it. Flagged.
