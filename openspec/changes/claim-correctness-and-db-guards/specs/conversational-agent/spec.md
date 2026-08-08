## ADDED Requirements

### Requirement: A claim operation the agent cannot perform is never silently saved as a task

When a message names a claim operation the agent has no tool for, the agent SHALL say it cannot perform it. It SHALL NOT fall through to `propose_create_task`, which saves a task and reads to the user as though the operation was accepted.

This is the failure being removed, not a hypothetical: "redo claim #7" was asked twice, matched no tool, and produced tasks #124 and #125 — an honest non-action that read like action, so the request was never actioned and the duplicate tasks were never noticed.

#### Scenario: A named claim operation has no matching tool

- **WHEN** a message asks for a claim operation the agent has no tool for
- **THEN** the agent SHALL reply that it cannot do it and name what it can do instead
- **AND** SHALL NOT create a task as a substitute for the operation

#### Scenario: The user genuinely wants a task

- **WHEN** the user asks for a reminder or a task in its own right
- **THEN** task capture SHALL work exactly as before

### Requirement: "Redo claim #N" resolves to the redo operation

Once the redo semantics are recorded, the agent SHALL route "redo claim #N" and its close variants to that operation rather than to task capture, and SHALL report which operation ran.

#### Scenario: Redo is asked in chat

- **WHEN** the user says "redo claim #7" or "claim #7 needs to be redone"
- **THEN** the agent SHALL invoke the redo operation on claim #7
- **AND** SHALL state which of the three operations it performed
