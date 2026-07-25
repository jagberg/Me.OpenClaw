## MODIFIED Requirements

### Requirement: Claim history and outstanding actions on demand
The system SHALL provide a paged view of the past year's claims and a view of everything currently waiting on Justin, both rendered as card images, with one tap-to-resolve card per outstanding actionable item.

Added after the original change. The actions view derives from the shared `claim_status.pending_actions()` so it cannot disagree with the chat agent's answer, separates blocked items (no tap can clear them), and states what it held back rather than truncating silently.

**One card per submission, not per claim.** Because the shared derivation now collapses submission-level actions, a batch of claims sharing one Gmail draft produces exactly one card with one button. The summary card's total and the caption's "N to action" count SHALL be computed from the same collapsed list as the cards themselves, so the count and the cards can never disagree — the very failure the shared-derivation rule exists to prevent.

#### Scenario: Actions requested
- **WHEN** the actions view is requested
- **THEN** a summary card is sent plus one tap-to-resolve card per actionable item, with blocked items reported separately

#### Scenario: More actions than the card cap
- **WHEN** actionable items exceed the display cap
- **THEN** the count held back is stated explicitly

#### Scenario: A batched submission awaiting send
- **WHEN** two `drafted` claims share one Gmail draft
- **THEN** one card is sent, naming both claim ids and their submission group id, with a single "Mark sent" button that advances both

#### Scenario: Caption agrees with the cards
- **WHEN** the actions view includes a batched submission
- **THEN** the caption's actionable count equals the number of cards sent, counting that submission once

## ADDED Requirements

### Requirement: A message about a whole submission names its group id

Where an outbound message already describes a submission as a unit — the drafted-batch notification, the actions card for a submission-level action, and the mark-sent result — it SHALL carry the submission's group id (`S6+7`) in addition to the individual claim `#id`s that the existing per-claim requirement mandates.

The group id supplements the claim ids, never replaces them: Justin's commands take claim ids, and the standing requirement that every claim mention carries `#N` is unchanged. Once Petcover's claim reference is known it stays the leading label, with the group id available for the earlier states where no reference exists yet.

#### Scenario: Drafted batch notification
- **WHEN** a batch of claims reaches `drafted` and is announced
- **THEN** the message carries the group id and each member's `#id`

#### Scenario: Mark-sent result for a batch
- **WHEN** the mark-sent tap advances a multi-claim submission
- **THEN** the reply names the group id and how many claims advanced

#### Scenario: A single-claim submission
- **WHEN** the submission is one claim with no batch
- **THEN** its `#id` is present as always and the group id adds no ambiguity
