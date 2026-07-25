# bank-transaction-feed Specification

## Purpose
Get Commbank credit-card charges into `bank_transactions` without ever holding bank credentials. Manual NetBank CSV upload through the dashboard, parsed positionally, deduped on re-upload. `netbank_csv.py`.

The no-credentials rule is a hard rule in `CLAUDE.md`, not a preference.

## Requirements

### Requirement: Ingest Commbank transactions from a manually uploaded CSV export
The system SHALL accept a NetBank CSV export uploaded by Justin through the dashboard and parse it into transaction records (date, amount, merchant). It SHALL NOT store or scrape Commbank login credentials, and SHALL NOT use a paid third-party feed or aggregator.

Confirmed real format (inspected directly): **no header row**, 4 quoted columns, positional not named — `DD/MM/YYYY`, signed decimal amount (negative = debit), fixed-width-padded "merchant name + location" description, and balance-or-blank. Shape (synthetic): `09/07/2026,"-19.64","EXAMPLE MERCHANT PTY LT  SYDNEY      AUS",""`.

The merchant field needs whitespace normalising before keyword matching because it is fixed-width padded, and location text runs into the merchant name with no reliable delimiter in some rows.

#### Scenario: CSV uploaded overlapping a previous import — the normal case, not an edge case
- **WHEN** Justin uploads an export, which is expected to overlap a prior upload's date range (exports aren't sliced to exactly the un-imported range)
- **THEN** each row is parsed positionally and inserted only if its date+amount+merchant combination isn't already stored; already-seen rows are skipped silently on every routine re-upload, not merely "on retry"

#### Scenario: CSV format doesn't match the parser
- **WHEN** an uploaded file doesn't match the expected 4-column positional layout (Commbank changes the export, or another account type includes a header row)
- **THEN** a visible failure is surfaced rather than silently skipping rows or inserting garbage

### Requirement: Store transaction metadata only, no bank credentials
The system SHALL persist transaction metadata (date, amount, merchant) locally and SHALL NOT persist Commbank login credentials anywhere in OpenClaw.

#### Scenario: Transaction stored
- **WHEN** a transaction is parsed from an uploaded CSV row
- **THEN** the stored row contains only date, amount, and merchant name

## Note — the alternatives were never formally closed out

The original change left open: *"Confirm manual-CSV workflow is acceptable, or pick an alternative (SMS-forwarding automation / paid aggregator)"*.

It is closed in practice, not by an explicit decision record: manual CSV upload is what shipped, has been in use since, and the no-credentials constraint was promoted to a hard rule in `CLAUDE.md`. The two alternatives were never evaluated in writing. Recorded here as settled-by-practice rather than left looking undecided — if either alternative is ever revisited it starts from scratch.
