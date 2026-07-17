# ADR-0004: Gmail ingestion via polling, not push/watch

**Date**: 2026-07-08
**Status**: accepted
**Deciders**: Justin

## Context

Gmail push notifications require a public HTTPS endpoint (Pub/Sub webhook). This is a local-only deployment with no exposed port (per the `127.0.0.1`-only binding decision).

## Decision

Email ingestion polls the Gmail API (`messages.list`/`history.list`) on a configurable interval (default 5 min), deduping by message ID in a `processed_emails` table.

## Alternatives Considered

### Alternative 1: Gmail push/watch (Pub/Sub webhook)
- **Pros**: near-real-time detection of new mail
- **Cons**: requires a publicly reachable endpoint, which conflicts with the local-only/no-exposed-port design
- **Why not**: latency of a few minutes is fine for household admin; not worth the exposed-surface trade-off

## Consequences

### Positive
- No inbound port needed, consistent with the local-only security posture

### Negative
- Minutes of latency between an email arriving and it surfacing as a candidate task

### Risks
- None significant given the low-urgency use case
