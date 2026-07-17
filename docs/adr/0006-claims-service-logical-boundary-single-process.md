# ADR-0006: Claims service as a logical boundary inside the single app, not a separate deployable

**Date**: 2026-07-18
**Status**: accepted
**Deciders**: Justin

## Context

The vet-claim automation grew into a substantial domain of its own (transaction detection, invoice matching, claim form filling, Petcover status tracking) alongside the original personal-assistant features (tasks, reminders, email ingestion). Justin asked to confirm whether the claims work should be "its own service."

## Decision

The claims service is a logical module boundary inside the one FastAPI app, not a separate deployable. The claim modules (`vet_detection`, `invoice_matching`, `claim_forms`, `claim_status`, orchestrated by `pipeline`) form the service; the assistant side reaches it only via `pipeline.run_once()` and the dashboard routes.

## Alternatives Considered

### Alternative 1: Separate FastAPI app/container for claims
- **Pros**: Independent deploys and scaling; hard isolation.
- **Cons**: Needs IPC or shared-DB access rules; SQLite is single-writer, so a second process adds real contention; twice the operational surface.
- **Why not**: One user, one SQLite file, one scheduler — the costs are immediate and the benefits only materialize with multi-user or independent-scaling needs that don't exist.

### Alternative 2: Move claim modules into a `claims/` subpackage
- **Pros**: Boundary visible in the file tree.
- **Cons**: Import churn across every module and test for purely cosmetic gain.
- **Why not**: The boundary is behavioral (entry points), not spatial; renames add risk without changing behavior.

## Consequences

### Positive
- Zero migration work; the boundary is enforced by convention and documented entry points.
- SQLite stays single-process; no locking issues.

### Negative
- The boundary can erode silently — nothing mechanical stops a new dashboard route from reaching into claim internals.

### Risks
- If multi-user or remote deployment becomes real, revisit; the entry-point discipline (pipeline + routes) keeps the eventual extraction tractable.
