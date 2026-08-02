## MODIFIED Requirements

### Requirement: Task mutations from chat are confirmed before they commit
Creating or closing a task from the chat agent SHALL be presented as a proposal requiring a tap, and MUST NOT write until confirmed. An outcome MUST NOT be invented when closing.

The gate's location changes with the move to the gateway. It previously lived in `telegram_bot._execute_action` (ADR-0016). Under ADR-0025 it is **split by origin**: a confirm tap on a card commits behind `/internal`, and a chat-initiated proposal commits in the MCP surface. Both are inside the Python app and SHALL call the same commit function; what differs is the entry point.

The confirmation SHALL be composed by code from the task record — its text, its follow-up date if any — and never from the model's description of what it intends to write. A freely-written confirmation can describe a task the model misheard as convincingly as one it heard correctly, which turns the tap into a formality.

Gated because capture spends an LLM call on follow-up extraction, and because a misheard task resurfaces later as a false obligation — the worst failure mode for a reminder system.

#### Scenario: Capture proposed from chat
- **WHEN** the agent is asked to remember something
- **THEN** it proposes the task text with a confirm button and writes nothing until the tap

#### Scenario: Confirmation text is not the model's account
- **WHEN** a task proposal is presented
- **THEN** the text shown is built from the record about to be written, so a misheard task is visible as what it would actually store

#### Scenario: Each entry point is gated independently
- **WHEN** the commit paths are exercised
- **THEN** neither `/internal` nor the MCP surface can commit a task mutation without a confirm, and each is proven separately rather than by one test standing for both

#### Scenario: Close without an outcome
- **WHEN** the agent is asked to close a task and no outcome was supplied
- **THEN** it asks for one rather than inventing it, consistent with never guessing a field the user must supply

### Requirement: Optional follow-up scheduling on capture
The system SHALL allow a task to be created with an optional follow-up date/time, extracted from the task's own text by the LLM; if present it MUST hand the task to reminder-scheduling.

Most tasks have none — the extractor returning "no date" is the normal case, not a failure.

This requirement survives the transport change **unaltered**, and is restated here only to record that it was checked rather than overlooked. It names the collaborator (`reminder-scheduling`) and not the mechanism, so `reminders.py` being replaced by gateway cron plus an app-side catch-up sweep changes nothing it asserts. The one thing to hold: `create_task` raises when follow-up extraction fails rather than storing a task with a silently missing reminder, and that must survive `llm.py`'s chat/extraction split — extraction keeps its own daily-budget walk precisely so this path stays reliable.

#### Scenario: Task captured with a follow-up date
- **WHEN** the description implies a date (e.g. "call painter, follow up Friday")
- **THEN** the follow-up is stored on the task and a reminder is scheduled for it

#### Scenario: Follow-up extraction fails
- **WHEN** the LLM is unavailable while extracting the follow-up
- **THEN** the failure is raised to the caller rather than swallowed into a task stored with a silently-missing reminder
