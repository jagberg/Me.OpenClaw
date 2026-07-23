# claim-status-tracking Specification

## Purpose
Track the lifecycle of submitted Petcover vet claims by polling Petcover's reply mailboxes, classifying replies into status events, correlating them to the originating claim submissions, and surfacing action items and settlement reconciliation on the dashboard.

## Requirements

### Requirement: Learn and store Petcover's claim reference number
Petcover assigns its own claim reference (e.g. `DC1-27-5628`, `GABR-0305`, `ELD-24-2146` — format has changed over time) once it acknowledges a submitted claim; this reference is distinct from the policy number and from the internal `vet_claims.id`. The system SHALL extract this reference from the acknowledgement reply and store it against the originating `vet_claims` row.

#### Scenario: Acknowledgement reply contains a claim reference
- **WHEN** an email from `claims.au@petcovergroup.com` matches the acknowledgement pattern and contains a claim reference in a recognized format
- **THEN** the reference is stored on the corresponding `vet_claims` row and used for all future correlation of replies about that claim

#### Scenario: Reference format not recognized
- **WHEN** an acknowledgement reply's claim reference doesn't match any known pattern
- **THEN** the claim is flagged `unclassified — reference format not recognized` rather than guessing or discarding the email

### Requirement: Classify Petcover reply emails into lifecycle events
The system SHALL poll `claims.au@petcovergroup.com`, `requiredinfo.au@petcovergroup.com`, and `accounts.au@petcovergroup.com` on the existing pipeline cycle (paginating past Gmail's page size so no reply is dropped, oldest-first so statuses never regress) and classify each new email into one of: `acknowledged`, `info_requested`, `suspended`, `settled`, `declined`, `ignore` (recognized noise, e.g. "Automatic reply:" instant receipts — dropped without review), or `unclassified` (a real reply we couldn't classify — queued for manual review, and never written to the claim's status). Emails from `marketing.au@petcovergroup.com` SHALL be excluded at the query level, not classified; emails older than the configured `PETCOVER_STATUS_SINCE` date SHALL be excluded at the query level (first-run backfill guard — historical replies about long-settled claims must not be ingested or mis-correlated).

#### Scenario: Subject matches a known pattern
- **WHEN** a reply's subject contains a recognized keyword (e.g. "Acknowledgement Letter", "suspended", "Request for information", "Settlement EFT", "Declined")
- **THEN** it is classified accordingly without needing to read the body

#### Scenario: Subject is ambiguous or generic
- **WHEN** a reply's subject doesn't match any known keyword (e.g. templated subjects reused across claim types)
- **THEN** the body text is checked as a fallback before falling back further to `unclassified`

#### Scenario: Marketing email arrives
- **WHEN** an email from `marketing.au@petcovergroup.com` is polled
- **THEN** it is excluded from classification entirely and never appears as a claim status event

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

### Requirement: Persist an append-only status history per claim
The system SHALL record every classified event to a `claim_status_events` log rather than overwriting the claim's current status, so the full sequence (e.g. suspended → info supplied → settled) remains visible.

#### Scenario: Claim receives multiple events over time
- **WHEN** a claim is acknowledged, then later suspended, then later settled
- **THEN** all three events exist in the history, each with its own timestamp and source email, and the claim's current status reflects the latest event

### Requirement: Surface action items and settlement reconciliation on the dashboard
The system SHALL show, on the dashboard: claims with an open `info_requested` or `suspended` event that Justin has not confirmed resolved (needs Justin's action), and for `settled` claims, the paid amount alongside the originally claimed amount.

#### Scenario: Open info request with no response yet
- **WHEN** a claim's latest event is `info_requested` or `suspended` and it has not been confirmed resolved
- **THEN** it appears in a "needs your action" list on the dashboard

#### Scenario: Settled claim with a different paid amount
- **WHEN** a claim's `settled` event includes a paid amount that differs from the originally claimed amount
- **THEN** both amounts are shown side by side on the dashboard (e.g. after an excess deduction) rather than showing only one

### Requirement: An info-requested or suspended claim stays flagged until Justin explicitly confirms it resolved
A new event arriving on a claim (even `settled` or `declined`) SHALL NOT automatically clear its "needs your action" status. The claim SHALL only leave the action list when Justin explicitly confirms it resolved via the dashboard, so a claim isn't silently dropped when Petcover's own follow-through is inconsistent (real pattern observed: repeated "request for X" emails on the same claim before resolution).

#### Scenario: New event arrives on an already-flagged claim
- **WHEN** a claim already in the "needs your action" list (e.g. `suspended`) receives a new event (e.g. `settled`)
- **THEN** the claim remains visible on the action list, now showing both events, until Justin confirms it resolved

#### Scenario: Justin confirms a claim resolved
- **WHEN** Justin clicks "confirm resolved" on a flagged claim
- **THEN** the claim is removed from the "needs your action" list; this confirmation is itself recorded as an event in the claim's status history
