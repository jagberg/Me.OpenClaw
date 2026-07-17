## ADDED Requirements

### Requirement: Ingest Commbank credit card transactions from a manually uploaded CSV export
The system SHALL accept a NetBank CSV export uploaded by Justin through the dashboard (produced however he likes — manual export or his own Playwright script), and SHALL parse it into transaction records (date, amount, merchant), never by storing or scraping Commbank login credentials, and never via a paid third-party feed/aggregator.

Confirmed real format (inspected directly, no header row): 4 quoted columns, positional not named — `DD/MM/YYYY, signed decimal amount (negative = debit), fixed-width-padded "merchant name + location" description, balance-or-blank`. Example shape (synthetic, not a real transaction): `09/07/2026,"-19.64","EXAMPLE MERCHANT PTY LT  SYDNEY      AUS",""`. Merchant field needs whitespace-trimming/normalizing before keyword matching since it's fixed-width padded, and location text runs into the merchant name with no reliable delimiter in some rows.

#### Scenario: CSV uploaded, overlapping a previous import (the expected normal case, not an edge case)
- **WHEN** Justin uploads a NetBank CSV export via the dashboard — every real-world export is expected to overlap the date range of a prior upload, since exports aren't sliced to exactly the un-imported range
- **THEN** each row is parsed positionally (no header expected) and inserted into `bank_transactions` only if its date+amount+merchant combination isn't already stored; already-seen rows are silently skipped without error, not just "on retry" but on every routine re-upload

#### Scenario: CSV format doesn't match the expected parser
- **WHEN** an uploaded file doesn't match the expected 4-column positional layout (e.g. Commbank changes their export format, or a different account type includes a header row)
- **THEN** the system surfaces a visible failure (dashboard/log) rather than silently skipping rows or inserting garbage data

### Requirement: Store transaction metadata only, no bank credentials
The system SHALL persist transaction metadata (date, amount, merchant) locally, and SHALL NOT persist Commbank login credentials anywhere in OpenClaw.

#### Scenario: Transaction stored
- **WHEN** a transaction is parsed from an uploaded CSV row
- **THEN** the stored row contains only date, amount, and merchant name
