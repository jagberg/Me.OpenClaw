## ADDED Requirements

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
A batch submission (up to 4 invoices on one claim document, one draft) is several `vet_claims` rows sharing one `draft_id` and, once acknowledged, one Petcover reference — replies apply to the whole group. The system SHALL correlate each classified reply using, in order of confidence: (1) an exact claim-reference match (all rows carrying that reference), (2) pet-name match against claims in a submitted-and-awaiting-reply status (`sent` or later) with no reference learned yet. Transaction-date proximity SHALL NOT be required: a claim's transaction can be a year older than its submission (confirmed real case), so date windows reject genuine matches.

#### Scenario: Reference present and known
- **WHEN** a reply contains a claim reference already stored on one or more `vet_claims` rows
- **THEN** the event is attached to every row of that submission

#### Scenario: No reference known yet, single plausible submission by pet name
- **WHEN** a reply names a pet matching exactly one submission awaiting a reply (one claim, or several sharing one draft_id)
- **THEN** the event is attached to every claim of that submission, and a reference in the reply is learned onto all of them

#### Scenario: Ambiguous match across submissions
- **WHEN** a reply's pet name matches claims spanning more than one submission
- **THEN** the event is stored unlinked and flagged on the dashboard as "needs manual link" — never guessed

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
