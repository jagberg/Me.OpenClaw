# claim-status-tracking — delta

## MODIFIED Requirements

### Requirement: Classify Petcover reply emails into lifecycle events
The system SHALL poll `claims.au@petcovergroup.com`, `requiredinfo.au@petcovergroup.com`, and `accounts.au@petcovergroup.com` on the existing pipeline cycle (paginating past Gmail's page size so no reply is dropped, oldest-first so statuses never regress) and classify each new email into one of: `acknowledged`, `info_requested`, `suspended`, `settled`, `declined`, `below_excess` (Petcover confirms the condition is covered but the amount claimed is under the fixed excess — recognized from the distinctive "under your fixed excess" wording in the letter body), `ignore` (recognized noise, e.g. "Automatic reply:" instant receipts — dropped without review), or `unclassified` (a real reply we couldn't classify — queued for manual review, and never written to the claim's status). Because a below-excess letter carries the generic "Acknowledgement Letter" subject, `below_excess` SHALL be checked ahead of `acknowledged`. Emails from `marketing.au@petcovergroup.com` SHALL be excluded at the query level, not classified; emails older than the configured `PETCOVER_STATUS_SINCE` date SHALL be excluded at the query level (first-run backfill guard).

#### Scenario: Subject matches a known pattern
- **WHEN** a reply's subject contains a recognized keyword (e.g. "Acknowledgement Letter", "suspended", "Request for information", "Settlement EFT", "Declined")
- **THEN** it is classified accordingly without needing to read the body

#### Scenario: Below-excess letter under an acknowledgement subject
- **WHEN** a reply's subject is the generic "Acknowledgement Letter" but its body says the amount claimed is under the fixed excess
- **THEN** it is classified `below_excess`, not `acknowledged`

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
