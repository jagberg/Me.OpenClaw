## ADDED Requirements

### Requirement: A Check B or unrecorded-subtotal flag is eligible for a clarification request
A claim carrying an open, undismissed Check B assessment-difference flag, or a flag that its claimable subtotal was not recorded, SHALL be eligible for inclusion in a consolidated clarification request (see `settlement-clarification-email`). A Check A arithmetic-difference flag SHALL NOT be included — that is a dispute with Petcover's own stated math, not a request to confirm what they assessed, and stays a manual dispute as today.

Resolving a clarification request via an exact-matching reply SHALL use the same dismissal mechanism, and record the same figures, as an existing manual dismiss (see "An unexplained assessment difference is not dismissible to invisible"). It SHALL NOT re-route the settlement, and SHALL NOT rewrite `claimable_subtotal`, any paid amount, or any other historical row — this requirement is unchanged by clarification requests existing.

#### Scenario: Assessment difference is eligible
- **WHEN** claim #8 carries an open Check B assessment-difference flag
- **THEN** it is eligible for inclusion in a clarification batch

#### Scenario: Arithmetic difference is not eligible
- **WHEN** a claim carries only a Check A arithmetic-difference flag
- **THEN** it is not eligible for inclusion in a clarification batch, and remains a manual dispute

#### Scenario: Clarification resolution reuses dismissal recording
- **WHEN** a clarification reply exactly confirms claim #8's recorded claimable subtotal
- **THEN** the resulting dismissal event records the reply's stated figures in the same shape as a manual dismiss, and no settlement row is rewritten
