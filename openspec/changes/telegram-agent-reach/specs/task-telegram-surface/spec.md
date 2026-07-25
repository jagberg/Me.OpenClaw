## ADDED Requirements

### Requirement: Tasks are readable from Telegram
The agent SHALL be able to list assistant-side tasks, optionally filtered by status, reporting each task's id, description, status, and follow-up time if one is set.

Today the `tasks` table has no reader anywhere in the codebase and no Telegram surface at all — it is reachable only by querying the DB directly.

#### Scenario: Ask what tasks are open
- **WHEN** the user asks what tasks he has
- **THEN** the agent lists open tasks with their ids and follow-up times, or says there are none

#### Scenario: Task ids are always shown
- **WHEN** the agent mentions any task
- **THEN** it includes the task id, since that is the handle for closing it

### Requirement: Task capture and closure are proposal-gated
Creating a task and recording its outcome SHALL be presented as a confirmation with an inline confirm button, and MUST NOT commit until the user taps confirm — the same gate claims mutations use. The agent MUST NOT describe the task as created or closed before the tap.

Capture is gated rather than immediate for two reasons: it spends an LLM call extracting a follow-up date, and a misheard task in a reminder system surfaces again later as a false obligation.

#### Scenario: Capture a task
- **WHEN** the user asks for something to be remembered as a task
- **THEN** the agent replies with the task text it intends to store plus a confirm button, and writes nothing until the tap

#### Scenario: Not confirmed
- **WHEN** the confirmation is shown but not tapped
- **THEN** no task row is created

#### Scenario: Close a task with an outcome
- **WHEN** the user says a task is done and what happened
- **THEN** the agent proposes closing that task id with that outcome text, and records it only after the tap

#### Scenario: Outcome not supplied
- **WHEN** the user says a task is done but gives no outcome
- **THEN** the agent asks what happened rather than inventing an outcome

### Requirement: Confirmation tokens are not claim-shaped
The pending-action token SHALL identify a proposed mutation independently of any claim id, so non-claim mutations (tasks) can use the same confirm-before-commit path.

The existing token is `<action>:<claim_id>`, which tasks have no value for. Tokens must stay well inside Telegram's 64-byte `callback_data` limit.

#### Scenario: A task proposal is confirmed
- **WHEN** a task-creation or task-closure proposal is confirmed by tap
- **THEN** the correct mutation executes, with no claim id involved anywhere in the round trip

#### Scenario: An expired proposal
- **WHEN** a confirm tap arrives for a proposal no longer held (e.g. after a restart)
- **THEN** the user is told it expired and nothing is written
