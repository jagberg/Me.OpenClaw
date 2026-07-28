## ADDED Requirements

### Requirement: An insurer reply changes state only through the single write path
Reply classification and correlation SHALL remain as they are, but applying the resulting state SHALL go through the single claim-state write path rather than updating the claim row directly. A reply whose event would move the claim illegally SHALL be recorded and refused, with the claim flagged — never applied.

This generalises two rules that were previously enforced by conditions inside this module: that `unclassified` never writes status, and that a terminal claim is never reopened.

#### Scenario: A reply about a settled claim
- **WHEN** a correlated reply would move a `settled` claim to an earlier state
- **THEN** the event is recorded, the state is unchanged, and the claim is flagged naming both states

#### Scenario: Re-reading already-ingested mail
- **WHEN** previously processed Petcover mail is deliberately re-read
- **THEN** events are appended and the write path decides what, if anything, changes state — so no separate "a re-read may not write status" rule is needed

#### Scenario: Reference and serial learning is unaffected
- **WHEN** a reply teaches a claim its Petcover reference or serial
- **THEN** those fields are still written as facts, independently of whether the event moved the claim's state
