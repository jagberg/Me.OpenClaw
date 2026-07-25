# task-capture Specification

## Purpose
The assistant half of OpenClaw: turn a described piece of household admin into a stored `tasks` row, optionally with a follow-up that schedules a reminder, and record what actually happened when it closes. `tasks.py`.

Independent of the claims service — a Task is never a step in the claims pipeline. See `CONTEXT.md` for why the vocabulary is kept separate from claims-side "Action".

## Requirements

### Requirement: Capture task from chat input
The system SHALL accept a free-text task description and store it as a task record with a status, description, and creation timestamp.

#### Scenario: A task is captured
- **WHEN** Justin describes a task (e.g. "call painter about the quote")
- **THEN** a task record is created with status `open`, the description, and the current timestamp

### Requirement: Capture task from email-sourced candidate
The system SHALL accept a candidate task surfaced by email-ingestion and store it as a task record, linked to the source Gmail message ID.

#### Scenario: Candidate task from email is captured
- **WHEN** email-ingestion surfaces a candidate task
- **THEN** a task record is created with status `open`, carrying the originating Gmail message ID

### Requirement: Optional follow-up scheduling on capture
The system SHALL allow a task to be created with an optional follow-up date/time, extracted from the task's own text by the LLM; if present it MUST hand the task to reminder-scheduling.

Most tasks have none — the extractor returning "no date" is the normal case, not a failure.

#### Scenario: Task captured with a follow-up date
- **WHEN** the description implies a date (e.g. "call painter, follow up Friday")
- **THEN** the follow-up is stored on the task and a reminder is scheduled for it

#### Scenario: Task captured with no follow-up date
- **WHEN** the description implies no date
- **THEN** the task is stored with no reminder scheduled

#### Scenario: Follow-up extraction fails
- **WHEN** the LLM is unavailable while extracting the follow-up
- **THEN** the failure is raised to the caller rather than swallowed into a task stored with a silently-missing reminder

### Requirement: Outcome logging
The system SHALL allow an outcome to be recorded against a task — who was spoken to and what was said — and SHALL close the task when it is recorded.

#### Scenario: Outcome recorded closes the loop
- **WHEN** Justin records an outcome (e.g. "spoke to painter, quote coming Monday")
- **THEN** the outcome text and timestamp are stored against the task and its status becomes `closed`

### Requirement: Task mutations from chat are confirmed before they commit
Creating or closing a task from the Telegram chat agent SHALL be presented as a proposal requiring a tap, and MUST NOT write until confirmed. An outcome MUST NOT be invented when closing.

Added 2026-07-25 with the Telegram surface (ADR-0016). Gated because capture spends an LLM call on follow-up extraction, and because a misheard task resurfaces later as a false obligation — the worst failure mode for a reminder system. Full detail in the `task-telegram-surface` capability.

#### Scenario: Capture proposed from chat
- **WHEN** the agent is asked to remember something
- **THEN** it proposes the task text with a confirm button and writes nothing until the tap
