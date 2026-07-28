## ADDED Requirements

### Requirement: A claim's timeline and a reversion are available on Telegram
Justin acts from Telegram more than from the dashboard, so the timeline SHALL be available there on request for a named claim, and a state change SHALL be revertible by tap. Reversion is a mutation, so it SHALL follow the existing confirm-by-tap pattern rather than acting on the first press, and the confirmation SHALL name the state before and after.

#### Scenario: Timeline requested for a claim
- **WHEN** Justin asks for a claim's history by id
- **THEN** its recorded events are returned in order, with reverted entries marked and backfilled entries identified

#### Scenario: Reversion is confirmed, not immediate
- **WHEN** Justin taps to revert a state change
- **THEN** he is shown the state it will return to and the change is applied only after he confirms

#### Scenario: A refused transition is worth telling him about
- **WHEN** an insurer reply is refused because the transition is not legal
- **THEN** it is surfaced rather than left only in a flag, because it means either a defect or new insurer behaviour
