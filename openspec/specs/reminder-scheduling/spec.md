# reminder-scheduling Specification

## Purpose
Fire a task's follow-up at its scheduled time, and survive a restart doing it. `reminders.py` over APScheduler with a SQLAlchemy jobstore.

See ADR-0003 (dashboard-only reminders, no push) — still the recorded decision for *this* capability, with the caveat below.

## Requirements

### Requirement: Schedule follow-up reminder tied to a task
The system SHALL allow a reminder to be scheduled for a specific future date/time, linked to exactly one task.

#### Scenario: Reminder scheduled at task capture
- **WHEN** a task is captured with a follow-up date
- **THEN** a reminder job is created for that date/time, linked to the task's ID

### Requirement: Reminder fires and surfaces on the dashboard
The system SHALL, when a reminder's scheduled time arrives, mark it `due` and surface it on the local dashboard alongside its linked task.

**Caveat worth knowing (unresolved):** ADR-0003 chose dashboard-only, no push, deliberately. A Telegram push channel has since shipped for the *claims* side (ADR-0003's deferral was lifted there by `telegram-claim-actions`), and the chat agent can now read and mutate tasks — but **assistant-side reminders still do not push**. A reminder that comes due is visible only if Justin opens the dashboard. Whether that should change was never asked; it is a gap, not a decision.

#### Scenario: Reminder becomes due
- **WHEN** the current time reaches a reminder's scheduled time
- **THEN** the reminder is marked `due` and displayed on the dashboard next to its task

### Requirement: Reminders persist across service restart
The system SHALL persist scheduled reminders to durable storage so restarting the service neither loses nor duplicates pending reminders. A reminder whose time passed while the service was down SHALL fire on startup rather than being treated as missed.

Implemented with `misfire_grace_time=None`, which is what makes a passed-while-down reminder fire instead of being skipped.

#### Scenario: Service restarts with pending reminders
- **WHEN** the service restarts while a reminder is scheduled for a future time
- **THEN** it fires at its original scheduled time exactly once — not lost, not duplicated

#### Scenario: Service was down when the reminder was due
- **WHEN** the service starts and a reminder's scheduled time has already passed
- **THEN** it fires immediately rather than being discarded as a missed run
