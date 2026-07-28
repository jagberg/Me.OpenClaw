## ADDED Requirements

### Requirement: A claim's timeline is visible, and a state change can be reverted from it
The history has existed since the event log shipped and has never been shown. The dashboard SHALL present a claim's recorded events in order — what happened, when, and where it came from — and SHALL offer reversion of a recorded state change from that view. A reverted event SHALL still appear, marked reverted, and a backfilled entry SHALL be identified as a backfill rather than as an observed transition.

Reversion SHALL name the state before and after before it is applied, and SHALL NOT be a single unconfirmed click.

#### Scenario: Timeline for a claim with real history
- **WHEN** Justin opens a claim that has been acknowledged, had information requested, and settled
- **THEN** all three events are listed in order with their dates and sources

#### Scenario: Reverting from the timeline
- **WHEN** Justin reverts a state event from the timeline
- **THEN** the control names the state it will return to, requires confirmation, and the reverted event remains listed and marked

#### Scenario: A reverted event stays visible
- **WHEN** a claim has a reverted event
- **THEN** it is shown as reverted rather than hidden, and the reason recorded with the reversion is shown

#### Scenario: Backfilled entries are labelled
- **WHEN** a claim's only event is the synthetic backfill of its current state
- **THEN** the timeline says so, rather than implying the transition was observed
