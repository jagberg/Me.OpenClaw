## ADDED Requirements

### Requirement: Accept a NetBank CSV sent to the Telegram bot as a document

The system SHALL accept a NetBank CSV export delivered as a Telegram document attachment
from the single authorized user, and SHALL parse and store it through the same parser and
the same dedupe rule as a dashboard upload. It SHALL NOT store or scrape Commbank login
credentials, and SHALL NOT treat the arrival of a file as authorization by itself — the
sender is authorized on the app's side, by the same username check that guards commands.

The two channels are one entrypoint, not two implementations. A second copy of "parse a
NetBank export" would eventually disagree with the first about which rows are new, and
that disagreement is a claim created twice or not at all.

#### Scenario: A CSV arrives as a Telegram document

- **WHEN** the authorized user sends a NetBank CSV export to the bot as a document
- **THEN** every row is parsed positionally and inserted only if its date+amount+merchant
  combination is not already stored, identically to a dashboard upload of the same file

#### Scenario: The same export is sent twice, or overlaps one already uploaded

- **WHEN** the user sends an export whose date range overlaps one already ingested through
  either channel
- **THEN** the already-seen rows are skipped, no duplicate transaction row is created, and
  therefore no duplicate claim is created — an overlapping re-send is the normal case

#### Scenario: The document is not a NetBank CSV

- **WHEN** the attached file does not match the expected 4-column positional layout
- **THEN** nothing is inserted and the user is told what was rejected and why, naming the
  offending row — the file is never partially imported and never silently discarded

#### Scenario: The sender is not the authorized user

- **WHEN** a document arrives from any account other than the single authorized user
- **THEN** the file is not parsed, nothing is stored, and the refusal is recorded rather
  than being indistinguishable from a message that never arrived

### Requirement: An accepted upload runs the claims scan immediately and reports what it found

The system SHALL run the claims pipeline immediately after an accepted upload through
either channel, and SHALL NOT allow that run to overlap a scheduled run of the same
pipeline. The reply to the upload MUST state what the upload changed: how many rows were
read, how many were new, and how many claims now need Justin's attention.

Both halves are load-bearing. Without the immediate run, an upload does nothing visible
until cron next fires. Without the mutual exclusion, an upload landing during a scheduled
tick means two concurrent pipeline runs, which is two Gmail drafts for one set of invoices
and therefore two submissions to the insurer.

#### Scenario: Upload while nothing else is running

- **WHEN** a CSV is accepted and no pipeline run is in flight
- **THEN** the claims scan runs before the reply is sent, and the reply states the row
  counts and what the scan found

#### Scenario: Upload lands while a scheduled tick is already running

- **WHEN** a CSV is accepted while a pipeline run is already in flight
- **THEN** a second concurrent run is not started, and the reply says the rows were stored
  and the scan is already running — a skipped run is never reported as a completed one

#### Scenario: The scan fails after the rows were stored

- **WHEN** the rows import successfully but the claims scan raises
- **THEN** the reply states that the transactions were stored and the scan failed, with the
  reason — a partial success is never reported as a plain success

### Requirement: Report the coverage watermark, derived from the stored transactions

The system SHALL report the date of the most recent transaction it holds — the coverage
watermark — so the user knows what period is already covered and what to export from
NetBank next. That date MUST be derived at read time from the stored transactions, and
MUST NOT be maintained as a separate stored value that can disagree with them.

It is reported in the reply to an upload and on the dashboard's upload panel. When no
transactions are stored at all, it says so rather than reporting an empty or invented date.

#### Scenario: The watermark advances after an upload

- **WHEN** an accepted upload contains transactions later than any already stored
- **THEN** the reported watermark is the latest transaction date in the combined set

#### Scenario: An upload adds nothing new

- **WHEN** an accepted upload contains only rows already stored
- **THEN** the reported watermark is unchanged, and the reply says nothing was new rather
  than implying the covered period moved

#### Scenario: No transactions are stored

- **WHEN** the watermark is requested and the transaction table is empty
- **THEN** the report says no transactions are held, and does not present a date

#### Scenario: The watermark is not a second source of truth

- **WHEN** the stored transactions are inspected
- **THEN** no column, row or file holds a separately maintained "last transaction date" —
  the reported value is computed from the transactions themselves on every read
