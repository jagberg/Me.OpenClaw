# claims-pipeline-resilience

## ADDED Requirements

### Requirement: One claim's failure never starves the tick
`pipeline.run_once` SHALL isolate per-claim matching/drafting failures: an exception while processing one claim is logged, written to that claim's `flag`, and the tick SHALL continue — claim-form drafting, Petcover status polling, and Telegram notifications always run. Confirmed live: an extraction 429 on the first pending claim starved status polling for days (`claim_status_events` empty with 3 sent claims).

#### Scenario: Extraction error on the first pending claim
- **WHEN** matching claim A raises an unexpected error
- **THEN** claim A is flagged with the reason and claims B…N, claim forms, Petcover polling and notifications still run this tick

#### Scenario: LLM provider unavailable
- **WHEN** matching raises `LLMUnavailableError` (quota/outage — global, not per-claim)
- **THEN** remaining matching is skipped this tick, affected claims carry an `invoice extraction unavailable` flag, and all non-LLM stages still run

### Requirement: LLM quota use is bounded per email, not per tick
The pipeline SHALL NOT re-spend LLM extraction on content it has already extracted (see invoice-matching's per-email cache). A persistent extraction failure SHALL surface as a visible flag rather than an unbounded silent retry-burn. Confirmed live: identical candidates re-extracted every 15 minutes exhausted a 20/day quota indefinitely.

#### Scenario: Provider quota exhausted mid-day
- **WHEN** the provider starts returning quota errors
- **THEN** subsequent ticks do not burn further calls re-extracting already-cached emails, and the failure is visible on the dashboard flags, not only in logs
