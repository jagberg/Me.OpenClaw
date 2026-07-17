# ADR-0008: Append-only event log for claim status, with explicit confirm-to-resolve

**Date**: 2026-07-18
**Status**: accepted
**Deciders**: Justin

## Context

After a claim is submitted, Petcover's replies (acknowledgement, info requests, suspensions, settlements, declines) arrive as loosely threaded emails; a claim can be suspended, answered, then settled. A single mutable status column cannot represent that back-and-forth, and Petcover's own follow-through is inconsistent (observed: repeated "request for X" emails on one claim). Justin explicitly wants open requests to stay visible until he confirms them closed.

## Decision

Every classified Petcover reply is appended to `claim_status_events` (never overwritten); a claim's current status is its latest lifecycle event. "Needs your action" (info_requested/suspended) persists — even across a later settlement — until Justin records an explicit `confirmed_resolved` event from the dashboard. `unclassified` events are a review-queue entry only and never write to claim status; manual linking attaches an event without rewriting status.

## Alternatives Considered

### Alternative 1: Mutable status column only
- **Pros**: Simplest; already existed for the pre-submission stages.
- **Cons**: Loses history; a settlement email would silently clear an unanswered info request.
- **Why not**: The whole point is that nothing falls off the radar without Justin's sign-off.

### Alternative 2: Auto-resolve action items when a terminal event (settled/declined) arrives
- **Pros**: Less clicking.
- **Cons**: Assumes Petcover's sequencing is reliable; observed data says it isn't.
- **Why not**: Justin explicitly chose confirm-to-close ("I have to confirm if they can be closed").

## Consequences

### Positive
- Full audit trail per claim; suspended→resolved→settled visible end to end.
- Status regressions are structurally impossible from ordering alone (poll processes oldest-first) and from noise (unclassified never writes status).

### Negative
- One extra click per resolved action item.
- Dashboard rollups derive state from the event list instead of reading a column (`claim_status.dashboard_lists()` owns this logic).

### Risks
- Event volume is trivial at personal scale; no pruning needed.
