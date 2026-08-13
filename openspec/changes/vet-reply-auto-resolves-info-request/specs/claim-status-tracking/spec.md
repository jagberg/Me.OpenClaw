## ADDED Requirements

### Requirement: A vet clinic's reply is interpreted and mapped to the state it actually represents
When a reply arrives from a clinic's email address that currently owes an open, unresolved information request, and the reply's subject or thread names exactly one of that clinic's open requests by Petcover reference and Sr, the system SHALL interpret the reply's content and map it to exactly one of:

- **Provided** — the vet has supplied what was asked (towards Justin/the app). The system SHALL resolve the claim's information request via the same path as Justin's explicit "confirm resolved" action, not a second one.
- **Sent to Petcover directly** — the vet states they already supplied it to Petcover, not to Justin. The system SHALL record a new `info_requested` event on the claim with `owed_by: "petcover"` rather than resolving it — the claim is no longer waiting on the vet, but it is not confirmed as done either.
- **Unavailable or declined** — the vet cannot find it or declines. The system SHALL leave the request owed by the vet exactly as before, and SHALL record the vet's stated reason visibly rather than silently.
- **Unclear** — the reply does not answer the request. The system SHALL leave the claim untouched.

Where the clinic currently owes more than one open request and the reply does not name which one (no reference/Sr match, or more than one matches), the system SHALL NOT interpret the reply's content at all, and SHALL NOT change any claim — correlation failure is handled before content, never guessed on top of an ambiguous match.

#### Scenario: Vet provides the document
- **WHEN** a clinic owing exactly one open request replies confirming it has supplied the requested document to Justin/the app
- **THEN** that claim's information request is resolved via the same path as an explicit "confirm resolved" tap

#### Scenario: Vet says it went straight to Petcover
- **WHEN** a clinic owing exactly one open request replies stating the requested notes were already sent directly to Petcover
- **THEN** the claim is not resolved; a new `info_requested` event is recorded with `owed_by: "petcover"`

#### Scenario: Vet can't find it
- **WHEN** a clinic owing exactly one open request replies that they cannot locate the requested document
- **THEN** the request remains owed by the vet, and the reply's content is recorded visibly on the claim rather than silently dropped

#### Scenario: Reply doesn't answer the request
- **WHEN** a clinic's reply doesn't address the open request at all (e.g. an unrelated question)
- **THEN** the claim is left completely untouched

#### Scenario: Clinic owes two open requests, reply names one
- **WHEN** a clinic owes open requests for claims #6 and #8, and a reply's subject names claim #6's reference and Sr only
- **THEN** claim #6's content is interpreted and acted on per the outcomes above; claim #8 remains untouched

#### Scenario: Clinic owes two open requests, reply is ambiguous
- **WHEN** a clinic owes open requests for claims #6 and #8, and a reply's subject/thread names neither (or both)
- **THEN** neither claim's content is interpreted, and neither claim changes

### Requirement: `owed_by` gains a third value — Petcover
The `info_requested` event's `owed_by` field SHALL accept `"petcover"` alongside its existing `"vet"`/`"justin"` values, meaning: the vet has said its part is done, and Justin needs to confirm with Petcover rather than chase the vet again. Every existing reader keyed on `owed_by` (dashboard/Telegram labels, the vet-nudge list) SHALL treat a claim whose latest `info_requested` event carries `owed_by: "petcover"` as excluded from the vet-owed list and labelled distinctly from both the vet-owed and Justin-owed cases.

#### Scenario: Label reflects the new value
- **WHEN** a claim's latest information request has `owed_by: "petcover"`
- **THEN** its dashboard/Telegram label names Petcover as who Justin should follow up with, distinct from "more vet info required" and "Petcover needs info from you"

#### Scenario: Vet-nudge list excludes it
- **WHEN** the weekly vet-nudge job runs
- **THEN** a claim whose latest information request has `owed_by: "petcover"` is not listed as an unanswered vet-owed request, since the vet is no longer who Justin needs to chase
