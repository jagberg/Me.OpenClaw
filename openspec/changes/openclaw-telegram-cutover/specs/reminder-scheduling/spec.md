## MODIFIED Requirements

### Requirement: Reminders persist across service restart
The system SHALL persist scheduled reminders to durable storage so restarting either runtime neither loses nor duplicates pending reminders. A reminder whose time passed while the service was down SHALL fire on startup rather than being treated as missed.

Scheduling moves from APScheduler with a SQLAlchemy jobstore to gateway cron, which changes what provides this guarantee. The reminder row itself SHALL become the source of truth: due-but-unfired reminders SHALL be detected and fired by an app-side catch-up sweep, not inferred from scheduler state.

**Corrected 2026-08-01 — the original reason given here was wrong, and the correction matters more than the conclusion.** This paragraph said a cron scheduler "has no equivalent knob" for `misfire_grace_time=None`. It has exactly that: `planStartupCatchup` runs missed jobs on boot and will not re-run a one-shot that already fired. Anyone reading the old reason and then discovering that would reasonably delete the sweep.

The sweep is still required, for a different reason. Gateway cron guarantees the *invocation* fires, not that the *app processed it*. If the Python app is down or mid-restart when cron calls `/internal`, the gateway records an invocation that failed or timed out while the reminder itself was never handled — and on the gateway's next boot that slot is in the past, not missed. Two further limits reinforce it: catch-up runs at most **5** missed jobs per restart, deferring the rest, and missed agent jobs are held a further two minutes. So the app must be able to answer "which reminders are due and unfired?" from its own rows, independently of what any scheduler believes.

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
