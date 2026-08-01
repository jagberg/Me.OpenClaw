## MODIFIED Requirements

### Requirement: Reminders persist across service restart
The system SHALL persist scheduled reminders to durable storage so restarting either runtime neither loses nor duplicates pending reminders. A reminder whose time passed while the service was down SHALL fire on startup rather than being treated as missed.

Scheduling moves from APScheduler with a SQLAlchemy jobstore to gateway cron, which changes what provides this guarantee. The behaviour was previously obtained from `misfire_grace_time=None`; a cron scheduler has no equivalent knob, so the reminder row itself becomes the source of truth: due-but-unfired reminders SHALL be detected and fired by a catch-up sweep, not inferred from scheduler state.

A restart of the Python app, a restart of the gateway, and a restart of both SHALL each preserve exactly-once firing.

#### Scenario: Service restarts with pending reminders
- **WHEN** either runtime restarts while a reminder is scheduled for a future time
- **THEN** it fires at its original scheduled time exactly once — not lost, not duplicated

#### Scenario: Service was down when the reminder was due
- **WHEN** the app starts and a reminder's scheduled time has already passed
- **THEN** the catch-up sweep fires it immediately rather than discarding it as a missed run

#### Scenario: Gateway redelivers a cron trigger
- **WHEN** a cron invocation is delivered twice for the same window
- **THEN** an already-fired reminder is not fired again

#### Scenario: Cron entry lost
- **WHEN** the gateway's cron entry is missing or disabled
- **THEN** the gap is visible rather than presenting as an absence of due reminders
