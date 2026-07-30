## ADDED Requirements

### Requirement: One write path owns claim state
The system SHALL route every change of a claim's state through a single function, which appends the event and then decides whether it moves the state. No other code SHALL write `vet_claims.status`. The event SHALL be appended whether or not the transition is permitted — the event happened, and only the state change is in question.

#### Scenario: A legal transition
- **WHEN** a `drafted` claim is marked sent
- **THEN** a `sent` event is appended and the claim's state becomes `sent`

#### Scenario: No other writer
- **WHEN** any module needs to change a claim's state
- **THEN** it calls the single write path, and a direct `status` write anywhere else is a defect the system can detect

### Requirement: Legal transitions are declared, and an illegal one is refused visibly
The permitted transitions SHALL be declared in one place as data. A transition not declared SHALL NOT be applied; the system SHALL record the event, leave the state unchanged, and flag the claim with a human-readable reason naming the current state, the attempted state, and the event. Silence is not an option — a refused transition means either a defect or genuinely new insurer behaviour, and both need to be seen.

#### Scenario: A settled claim cannot be reopened by a reply
- **WHEN** an `acknowledged` event correlates to a claim already `settled`
- **THEN** the event is recorded, the claim stays `settled`, and it is flagged naming both states

#### Scenario: The 2026-07-27 regression the table can refuse
- **WHEN** an `acknowledged` event is applied to a claim already `settled`, as happened to claims #6 and #7 on 2026-07-27
- **THEN** it is refused

#### Scenario: The 2026-07-27 regressions the table cannot refuse
- **WHEN** `sent`→`below_excess` (claim #22) or `below_excess`→`acknowledged` (claim #18) is applied
- **THEN** each is **permitted**, because both are ordinary forward moves — `below_excess` is non-terminal by decision, the invoice being retained — and what was wrong on 2026-07-27 was the routing and the replay, not the transition. This table is not their guard, and **no other mechanism has been shown to be one**: ADR-0020 records event-level idempotency as tried against the real DB for this incident and found insufficient, and reference/Sr routing precedence has never been tested against a replay of misrouted mail. The system SHALL NOT claim these two are guarded until one is demonstrated

#### Scenario: A state a claim is already in
- **WHEN** a transition targets the state the claim already holds and that self-transition is declared legal (a re-match after a split)
- **THEN** it is applied normally

### Requirement: Some events change no state
An event type SHALL be classified as data rather than as a condition inside a writer, into exactly one of three kinds. **State-changing**: names one fixed target state, applied only if the transition is declared. **Stateless**: recorded, and SHALL NOT move the claim's state. **Backfill**: names a per-event target read from the event's own detail, and is exempt from the transition table because it asserts a state the log has no path to — it exists to give a claim whose transitions predate the log a history the projection can fold. An event type belonging to no kind SHALL be refused and the claim flagged, never treated as stateless by default.

#### Scenario: An unclassifiable reply
- **WHEN** a Petcover reply cannot be classified
- **THEN** the `unclassified` event is recorded for review and the claim's state is untouched

#### Scenario: Justin confirms an information request resolved
- **WHEN** a `confirmed_resolved` event is recorded
- **THEN** the needs-action condition clears per the existing rule and the claim's state is unchanged

### Requirement: A claim's state is derivable from its event log
The system SHALL be able to compute a claim's state by replaying its events in recorded order — skipping stateless and reverted events, and applying state events subject to the declared transitions. The stored `status` column MAY remain as the cache every reader uses, but a disagreement between the column and the replayed result SHALL be detectable.

#### Scenario: Replay agrees with the stored column
- **WHEN** a claim's events are replayed
- **THEN** the result equals the claim's stored status

#### Scenario: A disagreement is surfaced, not ignored
- **WHEN** replay disagrees with the stored column for any claim
- **THEN** the claim is reported with a count exposed on the health endpoint, at a severity that signals a developer defect rather than an action for Justin

### Requirement: A state change is reverted by appending, never by deleting
The system SHALL provide reversion of a recorded state change: appending a reversion event that names the event it reverts, so replay ignores that event and the claim returns to the state the remaining events produce. The reverted event SHALL remain in the log and remain visible, marked as reverted. Reverting a reversion SHALL work by the same mechanism, with no special case.

Reversion SHALL be confirmed explicitly before it is applied, naming the state before and after, and SHALL record who reverted what and why.

#### Scenario: A misrouted letter is undone
- **WHEN** a status event that was attached to the wrong claim is reverted
- **THEN** that claim returns to its prior state as derived from its remaining events, and the reverted event is still on record

#### Scenario: The prior state is read, not guessed
- **WHEN** a claim's state is reverted
- **THEN** the prior state comes from the event log rather than being inferred from the absence of other events

#### Scenario: Reverting a terminal state
- **WHEN** the event that settled a claim is reverted
- **THEN** the claim leaves `settled`, because an undo of a wrong event is not the same as reopening a closed claim — and no path allows asserting a new state on a terminal claim directly

#### Scenario: Undoing an undo
- **WHEN** a reversion event is itself reverted
- **THEN** the originally reverted event applies again, by the same replay rule

### Requirement: Existing claims are backfilled without inventing history
Claims whose transitions predate the event log SHALL receive a synthetic event carrying their current state, so replay does not regress them. Where a claim's existing events already replay to its stored status, no synthetic event SHALL be added. A synthetic event SHALL be marked as a backfill so a shallow history is never presented as a real one.

#### Scenario: A claim whose state we caused
- **WHEN** a claim sits at `matched` from a transition that was never recorded
- **THEN** one backfill event carrying `matched` is appended, marked as a backfill

#### Scenario: A claim with genuine history
- **WHEN** a claim's recorded events already replay to its stored status
- **THEN** no synthetic event is added and its history is untouched

#### Scenario: Backfilled history is labelled
- **WHEN** a backfilled claim's timeline is shown
- **THEN** the synthetic entry is identified as a backfill rather than shown as an observed transition
