## ADDED Requirements

### Requirement: Send extraction requests to Gemini
The system SHALL send every extraction/chat request to Google Gemini 2.5 Flash via a Google AI Studio API key.

#### Scenario: Extraction request handled
- **WHEN** a chat message or candidate email is submitted for extraction
- **THEN** the system sends it to Gemini 2.5 Flash and uses the returned result

### Requirement: Respect free-tier rate limit
The system SHALL throttle outgoing Gemini requests to stay within the free tier's rate limit (15 requests/min) and SHALL back off and retry rather than drop a request on a `429` response.

#### Scenario: Burst of requests exceeds the rate limit
- **WHEN** more than 15 extraction requests are queued within one minute
- **THEN** the system queues the excess requests and sends them after backing off, rather than failing them

### Requirement: Log every extraction call
The system SHALL record every Gemini call — timestamp, success/failure, and latency — to durable storage for usage/quota visibility.

#### Scenario: Call outcome is logged
- **WHEN** any extraction request completes (successfully or with an error)
- **THEN** the system stores a record with the timestamp, outcome, and latency for that call

### Requirement: Fail visibly on quota exhaustion or outage
The system SHALL surface a clear failure rather than silently dropping a task capture when Gemini is unreachable or the free-tier quota is exhausted.

#### Scenario: Gemini is unreachable
- **WHEN** a Gemini request fails after retries (outage or quota exhausted)
- **THEN** the system reports the failure back to the caller instead of silently discarding the task
