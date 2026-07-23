# ops-alerting Specification

## Purpose
Make operational failures that would otherwise die silently in container logs — currently Gmail OAuth token death — loud on Telegram (which keeps working when Gmail doesn't), rate-limited so a persistent failure can't spam, with a one-time recovery confirmation.

## Requirements

### Requirement: Gmail auth death is detected specifically and alerted on Telegram
The system SHALL catch Gmail credential failures (refresh failure or missing token) distinctly from generic errors at the start of the pipeline's Gmail-dependent work, and SHALL send a Telegram alert naming the recovery command (`python scripts/gmail_auth.py`). Alerts SHALL be capped at 5 per rolling 24 hours while the failure persists; alert state survives container restarts.

#### Scenario: Token refresh fails on a tick
- **WHEN** a pipeline tick hits a credential refresh failure and fewer than 5 auth alerts were sent in the last 24 hours
- **THEN** one Telegram alert is sent naming the recovery command, and Gmail-dependent steps are skipped for the tick

#### Scenario: Sixth failure within 24 hours
- **WHEN** a tick fails auth and 5 alerts were already sent in the trailing 24 hours
- **THEN** no message is sent; the failure is still logged

### Requirement: Recovery is confirmed once
The system SHALL send exactly one "Gmail access restored" Telegram message on the first successful Gmail call following any auth alert.

#### Scenario: Justin re-authorizes
- **WHEN** a tick succeeds after one or more auth alerts were sent
- **THEN** one restore confirmation is sent and the alert state clears (a later failure starts a fresh alert cycle)
