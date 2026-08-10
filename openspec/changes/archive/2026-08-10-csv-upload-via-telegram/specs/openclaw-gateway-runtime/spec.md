## ADDED Requirements

### Requirement: The in-gateway plugin forwards an inbound document without interpreting it

When a document arrives on a channel the gateway owns, the in-gateway plugin SHALL forward
the file's bytes and its original filename to the app's `/internal` surface and render
whatever the app returns. It SHALL NOT parse the file, SHALL NOT decide whether the file is
acceptable, and SHALL NOT decide who is allowed to send one — those are the app's
decisions, next to the data and the rules.

This extends the boundary the plugin's command handlers already keep. A file format
understood in two runtimes is the same defect as a claim rule understood in two runtimes:
the copies drift, and drift here means a transaction imported wrong or not at all.

A forwarded document MUST NOT reach the agent as a chat turn. An attachment that falls
through to the model costs tokens, produces a plausible-sounding answer about a file
nothing imported, and is indistinguishable to the user from the file having worked.

#### Scenario: A document arrives on an owned channel

- **WHEN** the user sends a file attachment to the bot
- **THEN** the plugin forwards its bytes and filename to the app over the shared-secret
  internal surface, and replies with the app's own answer

#### Scenario: The plugin carries no format knowledge

- **WHEN** the plugin's source is inspected
- **THEN** it contains no CSV parsing, no column layout, no transaction rules and no
  authorization decision — only detection that a file is present, the forward, and the
  rendering of the reply

#### Scenario: The app is unreachable when a document arrives

- **WHEN** the forward to the app fails
- **THEN** the user is told the file did not reach the app and why; the failure is never
  reported as an accepted upload, and never left silent

#### Scenario: A forwarded document does not become a model turn

- **WHEN** a document is forwarded and handled by the app
- **THEN** the agent is not invoked for that message, and no model tokens are spent on it

#### Scenario: The inbound path is asserted at deploy

- **WHEN** the deploy's preflight runs
- **THEN** it asserts the inbound-document path is live, on the same principle as the
  registered-command assertion — a path that silently stopped working must not be
  indistinguishable from a period in which no file was sent
