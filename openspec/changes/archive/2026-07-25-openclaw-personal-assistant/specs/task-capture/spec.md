## ADDED Requirements

### Requirement: Capture task from chat input
The system SHALL accept a free-text task description from chat input and store it as a task record with a status, description, and creation timestamp.

#### Scenario: User captures a task via chat
- **WHEN** Justin sends a message describing a task (e.g. "call painter about the quote")
- **THEN** the system creates a task record with status `open`, the description, and the current timestamp

### Requirement: Capture task from email-sourced candidate
The system SHALL accept a candidate task surfaced by the email-ingestion pipeline and store it as a task record, linked to the source email message ID.

#### Scenario: Candidate task from email is captured
- **WHEN** the email-ingestion pipeline surfaces a candidate task extracted from a Gmail message
- **THEN** the system creates a task record with status `open`, linked to the originating Gmail message ID

### Requirement: Optional follow-up scheduling on capture
The system SHALL allow a task to be created with an optional follow-up date/time; if provided, it MUST hand the task to reminder-scheduling.

#### Scenario: Task captured with a follow-up date
- **WHEN** a task is captured with a follow-up date (e.g. "call painter, follow up Friday")
- **THEN** the system stores the follow-up date on the task and schedules a reminder for that date

#### Scenario: Task captured with no follow-up date
- **WHEN** a task is captured with no follow-up date specified
- **THEN** the system stores the task without scheduling any reminder

### Requirement: Outcome logging
The system SHALL allow an outcome to be recorded against a task, capturing who was spoken to and what was said, and SHALL update the task's status accordingly.

#### Scenario: Outcome recorded closes the loop
- **WHEN** Justin records an outcome for a task (e.g. "spoke to painter, quote coming Monday")
- **THEN** the system stores the outcome text and timestamp against the task and updates its status
