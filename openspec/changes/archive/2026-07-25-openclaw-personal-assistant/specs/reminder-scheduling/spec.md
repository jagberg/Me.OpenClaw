## ADDED Requirements

### Requirement: Schedule follow-up reminder tied to a task
The system SHALL allow a reminder to be scheduled for a specific future date/time, linked to exactly one task.

#### Scenario: Reminder scheduled at task capture
- **WHEN** a task is captured with a follow-up date
- **THEN** the system creates a reminder job for that date/time, linked to the task's ID

### Requirement: Reminder fires and surfaces on the dashboard
The system SHALL, when a reminder's scheduled time arrives, mark it as due and surface it on the local dashboard alongside its linked task.

#### Scenario: Reminder becomes due
- **WHEN** the current time reaches a reminder's scheduled time
- **THEN** the system marks the reminder `due` and displays it on the dashboard next to its task

### Requirement: Reminders persist across service restart
The system SHALL persist scheduled reminders to durable storage so that restarting the service does not lose or duplicate pending reminders.

#### Scenario: Service restarts with pending reminders
- **WHEN** the service restarts while a reminder is still scheduled for a future time
- **THEN** the reminder fires at its original scheduled time exactly once, not lost and not duplicated
