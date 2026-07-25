# claims-pipeline-resilience Specification

## Purpose
One claim's failure must never starve the tick. `pipeline.run_once` runs matching, drafting, reconciliation, Petcover polling and Telegram notification in order; this capability is the isolation and quota discipline that keeps the later stages running when an earlier one fails.

Both requirements come from live failures, not from theory.

## Requirements

### Requirement: One claim's failure never starves the tick
`pipeline.run_once` SHALL isolate per-claim matching/drafting failures: an exception while processing one claim is logged, written to that claim's `flag`, and the tick SHALL continue — claim-form drafting, Petcover status polling, and Telegram notifications always run.

Confirmed live: an extraction 429 on the first pending claim starved status polling for days (`claim_status_events` empty while three claims sat `sent`).

#### Scenario: Extraction error on the first pending claim
- **WHEN** matching claim A raises an unexpected error
- **THEN** claim A is flagged with the reason and claims B…N, claim forms, Petcover polling and notifications still run this tick

#### Scenario: LLM provider unavailable
- **WHEN** matching raises `LLMUnavailableError` (quota/outage — global, not per-claim)
- **THEN** remaining matching is skipped this tick, affected claims carry an `invoice extraction unavailable` flag, and all non-LLM stages still run

#### Scenario: Transient flags don't stick
- **WHEN** a claim flagged with a transient matching failure is retried on a later tick and succeeds
- **THEN** the stale error flag is cleared rather than left on a now-healthy claim

### Requirement: LLM quota use is bounded per email, not per tick
The pipeline SHALL NOT re-spend LLM extraction on content it has already extracted (see `invoice-matching`'s per-email cache). A persistent extraction failure SHALL surface as a visible flag rather than an unbounded silent retry-burn.

Confirmed live: identical candidates re-extracted every 15 minutes exhausted a 20/day quota indefinitely.

#### Scenario: Provider quota exhausted mid-day
- **WHEN** the provider starts returning quota errors
- **THEN** subsequent ticks do not burn further calls re-extracting already-cached emails, and the failure is visible on the dashboard flags, not only in logs

### Requirement: A dead draft self-heals instead of retrying forever
When a stored `draft_id` no longer exists in Gmail (404), the pipeline SHALL clear it and flag the claim for a fresh invoice request, rather than retrying the missing draft every tick.

Confirmed live: claim #17 logged the same 404 more than ten times a day because a deleted draft can never resolve itself — distinct from a transient fetch failure, which does retry.

#### Scenario: Draft deleted from Gmail
- **WHEN** reconciliation fetches a `draft_id` and Gmail returns 404
- **THEN** `draft_id` is cleared, the claim is flagged to send a fresh invoice request, and the 404 is not retried on later ticks

#### Scenario: Transient fetch failure
- **WHEN** the fetch fails for any other reason
- **THEN** the claim is left untouched and retried on the next tick
