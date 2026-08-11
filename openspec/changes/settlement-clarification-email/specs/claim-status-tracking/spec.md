## ADDED Requirements

### Requirement: A claim awaiting Petcover clarification is a distinct pending-action state
The system SHALL support an `awaiting_petcover_clarification` pending-action state, entered only when a claim is queued into an open clarification draft to Petcover (see `settlement-clarification-email`) — never while it is merely showing the pre-send settlement-review card, since at that point nothing has been asked of Petcover yet. It is distinct from `info_requested`/`suspended` on the dashboard's "needs your action" list, since this state means Justin is waiting on Petcover rather than needing to act himself. It follows the same persistence rule as `info_requested`/`suspended`: a new unrelated event SHALL NOT clear it — only an exact-match auto-resolved reply or an explicit Acceptable/dismiss action does.

#### Scenario: Claim enters the clarification state
- **WHEN** a claim is queued into an open clarification draft via "More Info"
- **THEN** its pending-action state becomes `awaiting_petcover_clarification` and it appears on the dashboard as waiting on Petcover, not as needing Justin's action

#### Scenario: Not yet entered while only the review card is showing
- **WHEN** a claim carries an open Check B or unrecorded-subtotal flag and its settlement-review card is showing, but "More Info" has not been clicked
- **THEN** the claim is NOT in `awaiting_petcover_clarification` — no email has been asked for yet

#### Scenario: Distinct from needs-your-action
- **WHEN** the dashboard renders pending-action cards
- **THEN** `awaiting_petcover_clarification` claims are visually/semantically distinguished from `info_requested`/`suspended` claims, since the latter need Justin to do something and the former do not

#### Scenario: Cleared only by resolve or explicit action
- **WHEN** a claim in `awaiting_petcover_clarification` receives any event other than an exact-match clarification reply
- **THEN** it remains in that state until either an exact match resolves it or Justin explicitly clicks Acceptable
