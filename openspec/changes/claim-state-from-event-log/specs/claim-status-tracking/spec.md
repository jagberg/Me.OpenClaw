## ADDED Requirements

### Requirement: An insurer reply changes state only through the single write path
Reply classification and correlation SHALL remain as they are, but applying the resulting state SHALL go through the single claim-state write path rather than updating the claim row directly. A reply whose event would move the claim illegally SHALL be recorded and refused, with the claim flagged — never applied.

This generalises two rules that were previously enforced by conditions inside this module: that `unclassified` never writes status, and that a terminal claim is never reopened.

#### Scenario: A reply about a settled claim
- **WHEN** a correlated reply would move a `settled` claim to an earlier state
- **THEN** the event is recorded, the state is unchanged, and the claim is flagged naming both states

#### Scenario: Re-reading already-ingested mail
- **WHEN** previously processed Petcover mail is deliberately re-read
- **THEN** events are appended and the write path decides what, if anything, changes state — which subsumes the "a re-read may not write status" rule for every transition the table refuses. It does NOT subsume it for a re-read whose event is a legal forward move applied to the wrong claim — that case has no demonstrated guard, since ADR-0020 records event-level idempotency as tried and found insufficient for it, so ADR-0020's Decision 1 stays open

#### Scenario: Reference and serial learning is unaffected
- **WHEN** a reply teaches a claim its Petcover reference or serial
- **THEN** those fields are still written as facts, independently of whether the event moved the claim's state

## MODIFIED Requirements

### Requirement: Persist an append-only status history per claim
The system SHALL record every classified event to a `claim_status_events` log rather than overwriting the claim's current status, so the full sequence (e.g. suspended → info supplied → settled) remains visible.

A claim's current status SHALL be the state its event log projects, not simply the latest event: an event whose transition is not declared legal is recorded and refused, and a refused event therefore does NOT become the claim's status. Stateless events (a review-queue entry, a dismissal, a detached reference) are recorded and move nothing. This is a narrowing of the original wording — "the claim's current status reflects the latest event" was true when every recorded event wrote status unconditionally, which is precisely the behaviour that let a re-read of an old acknowledgement walk two settled claims backwards on 2026-07-27.

#### Scenario: Claim receives multiple events over time
- **WHEN** a claim is acknowledged, then later suspended, then later settled
- **THEN** all three events exist in the history, each with its own timestamp and source email, and the claim's current status is `settled`

#### Scenario: The latest event is one the lifecycle does not allow
- **WHEN** the most recent event recorded against a claim is one its current state may not transition to
- **THEN** the event is kept in the history as evidence, the claim's status is unchanged, and the claim is flagged — the latest event is not the status
