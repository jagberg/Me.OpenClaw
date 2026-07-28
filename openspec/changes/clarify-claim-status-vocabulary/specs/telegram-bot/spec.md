## ADDED Requirements

### Requirement: Rendered cards and notifications use the shared status vocabulary
The claim-history renderer, the actions-summary renderer and the lifecycle notification text SHALL take their status wording from the shared display vocabulary (`claim-status-vocabulary`) rather than a copy maintained alongside the dashboard's. Card colour choices MAY remain renderer-local, but SHALL be keyed by status rather than by label text so a rewording cannot drop a colour.

#### Scenario: History card shows a blocked claim
- **WHEN** the history card renders a `matched` claim blocked on an undefined insurer process
- **THEN** the row reads as blocked, matching the dashboard word for word

#### Scenario: No second label map
- **WHEN** the card renderer needs a status's wording
- **THEN** it reads the shared vocabulary, and no "mirrors the dashboard" duplicate map remains in the renderer

#### Scenario: A new state costs one edit
- **WHEN** a new claim state is introduced
- **THEN** its wording is added to the shared vocabulary alone, and the cards and notifications pick it up with no renderer change
