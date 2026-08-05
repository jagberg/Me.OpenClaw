## MODIFIED Requirements

### Requirement: Persist an append-only status history per claim
The system SHALL record every classified event to a `claim_status_events` log rather than overwriting the claim's current status, so the full sequence (e.g. suspended → info supplied → settled) remains visible.

A transition the state machine refuses SHALL still be recorded as an event, with the detail it carries today. Whether that refusal is also written to the claim's `flag` column depends on why the mail was being read:

- When mail is polled **normally**, a refused transition is genuinely surprising — something arrived out of order — and the system SHALL flag the claim, naming both states, so it is visible.
- When mail is **replayed deliberately** (`poll_petcover_status(reread=True)`, which re-applies a classifier or extraction fix to mail already ingested), a refused transition is the expected outcome for every claim whose state has moved on since the mail was first read. The system SHALL NOT write those refusals to the claim's `flag`. The event, and its detail, are recorded unchanged.
- During a replay, a settlement finding the re-read genuinely produces SHALL reach the `flag` column, instead of losing precedence to a refusal that was not written.

Live evidence, 2026-08-05: a recovery replay of five approval letters left `refused settled -> acknowledged` text on six claims (#1, #2, #6, #7, #8, #13). On claim #2 that text displaced the finding the replay existed to produce — `claimable subtotal not recorded` — because `process_reply` prefers a refusal over a settlement flag. Six expected consequences of asking for a replay read as six new failures, and hid one real one.

The knowledge that a replay is in progress SHALL be passed explicitly from the poller, not inferred from the event already existing: a genuinely late-arriving letter is indistinguishable by that test, and inferring it would silence the case the flag exists for.

#### Scenario: Claim receives multiple events over time
- **WHEN** a claim is acknowledged, then later suspended, then later settled
- **THEN** all three events exist in the history, each with its own timestamp and source email, and the claim's current status reflects the latest event

#### Scenario: A refused transition during ordinary polling
- **WHEN** a letter arrives out of order and its event is not a declared transition from the claim's current state
- **THEN** the event is recorded, the status is left alone, and the claim is flagged naming both states

#### Scenario: A refused transition during a deliberate replay
- **WHEN** `poll_petcover_status(reread=True)` re-reads an acknowledgement against a claim that has since settled
- **THEN** the event is recorded with its full detail, the status is left alone, and the claim's `flag` is NOT overwritten with the refusal

#### Scenario: A replay produces a genuine finding
- **WHEN** a replay re-reads an approval letter whose transition is refused, and the settlement check raises a finding for that claim
- **THEN** the finding is written to the claim's `flag`, rather than being suppressed behind the unwritten refusal
