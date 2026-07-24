# claim-status-tracking — delta

**Note (2026-07-24):** this requirement's classification behavior (the `approved`/`below_excess` event types, the corrected phrase, and the transaction-date-based settlement math) already shipped as part of the `claim_status._validate_settlement` hotfix, ahead of and independent from this change — see ADR-0013. This delta is kept accurate to what's live rather than describing not-yet-built work; applying this change's tasks for classification is a no-op confirmation, not new code.

## MODIFIED Requirements

### Requirement: Classify Petcover reply emails into lifecycle events
The system SHALL poll `claims.au@petcovergroup.com`, `requiredinfo.au@petcovergroup.com`, and `accounts.au@petcovergroup.com` on the existing pipeline cycle (paginating past Gmail's page size so no reply is dropped, oldest-first so statuses never regress) and classify each new email into one of: `acknowledged`, `approved` (Petcover has assessed and approved the claim, carrying the only dollar breakdown in the lifecycle — precedes a dollar-less "payment processed" `settled` confirmation), `info_requested`, `suspended`, `settled`, `declined`, `below_excess` (Petcover confirms the condition is covered but the amount claimed is under the fixed excess — recognized from the confirmed-live phrase "Claim assessment outcome: Under excess", with the originally-guessed "under your fixed excess" checked too), `ignore` (recognized noise, e.g. "Automatic reply:" instant receipts — dropped without review), or `unclassified` (a real reply we couldn't classify — queued for manual review, and never written to the claim's status). Both `approved` and `below_excess` are recognized from body text, since their real subject is the generic "Petcover Insurance Claim for Ari" rather than a distinct keyworded subject. Emails from `marketing.au@petcovergroup.com` SHALL be excluded at the query level, not classified; emails older than the configured `PETCOVER_STATUS_SINCE` date SHALL be excluded at the query level (first-run backfill guard).

#### Scenario: Subject matches a known pattern
- **WHEN** a reply's subject contains a recognized keyword (e.g. "Acknowledgement Letter", "suspended", "Request for information", "Settlement EFT", "Declined")
- **THEN** it is classified accordingly without needing to read the body

#### Scenario: Approved/below-excess letter under a generic subject
- **WHEN** a reply's subject is the generic "Petcover Insurance Claim for Ari" but its body states the claim has been approved, or that it's under the fixed excess
- **THEN** it is classified `approved` or `below_excess` respectively, not left `unclassified`

#### Scenario: Subject is ambiguous or generic
- **WHEN** a reply's subject doesn't match any known keyword
- **THEN** the body text is checked as a fallback before falling back further to `unclassified`

#### Scenario: Marketing email arrives
- **WHEN** an email from `marketing.au@petcovergroup.com` is polled
- **THEN** it is excluded from classification entirely and never appears as a claim status event

## ADDED Requirements

### Requirement: A below-excess outcome is non-terminal and retains the invoice
A `below_excess` event SHALL set the claim's status to `below_excess` without discarding its matched invoice or `invoice_data`, and `below_excess` SHALL NOT be a terminal status — the claim continues to accrue toward its condition and is eligible to be re-drafted once the condition's claimable exceeds the excess (see `excess-threshold-accrual`). A below-excess outcome SHALL be distinguished from a `declined` outcome (which is terminal).

#### Scenario: Below-excess reply on a submitted claim
- **WHEN** a `below_excess` letter correlates to a submitted claim
- **THEN** the claim's status becomes `below_excess`, its invoice is retained, and the claim is not treated as declined or removed from accrual

#### Scenario: Below-excess claim re-drafted after the condition accrues
- **WHEN** the claim's condition later accrues past the excess and the release batch is drafted
- **THEN** the `below_excess` claim rejoins the `drafted` lifecycle using its existing invoice
