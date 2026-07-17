## ADDED Requirements

### Requirement: Read-only Gmail OAuth connection
The system SHALL authenticate to Gmail via OAuth with read-only scope and SHALL NOT request or use any write/send/modify scope.

#### Scenario: Initial OAuth consent
- **WHEN** the service is set up for the first time
- **THEN** it prompts for Google OAuth consent requesting only the Gmail read-only scope, and stores the resulting refresh token locally

### Requirement: Poll for new messages periodically
The system SHALL poll the Gmail account for new messages at a configurable interval rather than relying on push notifications.

#### Scenario: Polling picks up a new message
- **WHEN** a new email arrives in the connected Gmail account
- **THEN** the system detects it within one polling interval without requiring any inbound webhook

### Requirement: Dedupe already-processed messages
The system SHALL track which Gmail message IDs have already been processed and SHALL NOT surface the same message as a candidate task more than once.

#### Scenario: Same message seen across multiple polls
- **WHEN** a previously processed message is returned again by a subsequent poll
- **THEN** the system does not create a duplicate candidate task for it

### Requirement: Surface candidate task to task-capture
The system SHALL extract a candidate task description from a qualifying email and hand it to the task-capture pipeline for storage.

#### Scenario: Actionable email produces a candidate task
- **WHEN** a polled email contains actionable content (e.g. a request or appointment)
- **THEN** the system passes a candidate task description and the source message ID to task-capture
