# claim-status-tracking — delta

## MODIFIED Requirements

### Requirement: Correlate a reply to the originating claim submission
A Petcover reference identifies a Condition Thread — one (pet, condition) pairing whose reference is reused for the life of the condition — not a Submission. The system SHALL correlate each classified reply using, in order of confidence: (1) an exact (reference, Sr) match — the event attaches to that single claim; (2) a reference-only match — the event attaches to the thread's non-terminal claims only (never `settled`/`declined`); (3) for replies with no stored reference (acknowledgements learning it): candidates are un-referenced claims in a submitted-and-awaiting-reply status for the printed pet (nickname-tolerant), narrowed by the reply's printed condition matching the claim's condition text (case-insensitive) — Petcover's printed condition is authoritative in their letters; if condition matching does not decide it, the reply SHALL be assumed to belong to the most recently sent matching submission, and multiple same-day replies SHALL map newest-reply→newest-sent working backwards. Transaction-date proximity SHALL NOT be required: a claim's transaction can be a year older than its submission (confirmed real case), so date windows reject genuine matches.

#### Scenario: Letter cites reference and serial
- **WHEN** a reply contains a stored reference and an Sr held by one claim
- **THEN** the event is attached to that claim only

#### Scenario: Reference present and known, no serial
- **WHEN** a reply contains a claim reference already stored on `vet_claims` rows and cites no Sr
- **THEN** the event is attached to that thread's non-terminal claims only; settled and declined claims are untouched

#### Scenario: Acknowledgement resolved by condition content
- **WHEN** an un-referenced acknowledgement prints pet "Ari" and condition "Arthritis", and exactly one awaiting submission holds claims with condition text "Arthritis"
- **THEN** the reference and Sr are learned onto the matching submission's claims

#### Scenario: Condition decides nothing — recency fallback
- **WHEN** an acknowledgement's printed condition matches no awaiting claim's condition text (Petcover re-conditioned the document)
- **THEN** the reply is attributed to the most recently sent awaiting submission for that pet, and the claim's own condition text is left unchanged

#### Scenario: Two acknowledgements the same day
- **WHEN** two un-referenced acknowledgements for one pet arrive the same day and two submissions are awaiting
- **THEN** each acknowledgement attaches to a distinct awaiting submission (learning a reference removes that submission from the un-referenced pool, so the second ack cannot collide onto the first's submission); the recency rule orders which is tried first, and any residual mis-pairing when conditions are indistinguishable is correctable via manual linking

#### Scenario: Acknowledgement without an extractable reference
- **WHEN** an acknowledgement correlates to a submission but no claim reference could be extracted from it
- **THEN** the claim is flagged `unclassified — reference format not recognized` rather than silently proceeding without one

#### Scenario: Manually linking an unattached reply
- **WHEN** Justin links an unattached event to a claim from the dashboard
- **THEN** the event is attached to that claim only — the claim's status is NOT rewritten (a late-linked old email must not regress a settled claim), and linking to a nonexistent claim is refused
