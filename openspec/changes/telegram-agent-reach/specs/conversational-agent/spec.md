## MODIFIED Requirements

### Requirement: The agent never claims mailbox access it does not have
The agent SHALL NOT browse, search, or read arbitrary mail, and SHALL NOT imply that it has. It MAY run the named sweeps that exist (`rematch_claims`, `poll_petcover_now`) and report what those sweeps found, presenting each as a specific check with a stated scope — never as "I looked through your email".

Rationale for the original rule is unchanged and still governs: an early live session had the agent answer "I checked your sent mail" when it had no such capability. This change gives it two real, bounded sweeps, so the rule is narrowed to what is now true rather than deleted. The capability moved; the risk did not.

#### Scenario: Asked to search the mailbox
- **WHEN** the user asks the agent to look through or search his email generally
- **THEN** the agent says plainly that it cannot browse or search the mailbox, and offers the named sweeps that do exist

#### Scenario: A named sweep is run
- **WHEN** the agent runs `rematch_claims` or `poll_petcover_now`
- **THEN** its reply states what that sweep actually covered (which claims, or which senders since when) rather than implying a general mailbox read

## ADDED Requirements

### Requirement: Date-scoped questions
The agent SHALL know the current date, and SHALL support restricting both the outstanding-actions list and claim queries to a transaction-date range.

#### Scenario: Actions for a named month
- **WHEN** the user asks what actions exist for July 2025 transactions
- **THEN** the agent reports only actions whose claim's transaction date falls in that month, and says so

#### Scenario: Relative dates resolve correctly
- **WHEN** the user asks about "last month" or "this year"
- **THEN** the agent resolves it against the real current date supplied to it, not a guess

#### Scenario: A range with nothing in it
- **WHEN** the requested range contains no matching claims
- **THEN** the agent says the range is empty, and does not silently widen it

### Requirement: On-demand claim rematch, scoped to a vet or claim
The agent SHALL be able to re-run invoice matching for `pending_match` claims on demand, optionally narrowed to one merchant or one claim, and report the per-claim outcome. This acts immediately rather than as a proposal.

Acting directly is deliberate: this is the identical call the pipeline makes unattended every 15 minutes, it cannot send anything (Gmail is read + drafts only), and a wrong match is reversible through the existing unmatch path. Per-claim confirmation would also defeat the purpose, since the request is inherently a sweep over several claims.

#### Scenario: Sweep one vet's claims
- **WHEN** the user asks whether the emails from a named vet can be processed
- **THEN** the agent re-runs matching for that merchant's `pending_match` claims and reports each claim's outcome by id

#### Scenario: Already-matched claims are untouched
- **WHEN** the sweep runs
- **THEN** claims not in `pending_match` are not considered and their state does not change

#### Scenario: Sweep runs twice
- **WHEN** the same sweep executes a second time (e.g. at-least-once update replay, ADR-0014)
- **THEN** the second run changes nothing, because claims matched by the first are no longer `pending_match`

### Requirement: On-demand Petcover status poll
The agent SHALL be able to run the Petcover reply poll on demand and report what it recorded — how many messages were checked and which claims changed. It SHALL state that the poll covers unprocessed mail only.

Already-processed messages are never re-read: replaying a seen email against the append-only event log risks re-applying a status transition. "Nothing new" must therefore be distinguishable from "nothing there".

#### Scenario: Poll finds new replies
- **WHEN** the user asks the agent to check for Petcover replies
- **THEN** the agent runs the poll and reports the claims whose status changed, by id

#### Scenario: Poll finds nothing new
- **WHEN** no unprocessed Petcover mail exists
- **THEN** the agent says nothing new arrived, and states that it only checks unprocessed mail — it does not report this as "no replies exist"

#### Scenario: Poll runs twice
- **WHEN** the poll executes a second time (at-least-once replay)
- **THEN** no duplicate status events are recorded

### Requirement: Submissions awaiting a reply
The agent SHALL be able to report what has been sent to Petcover and whether a reply has come back, one entry per Submission (claims sharing a `draft_id`), with how long it has been waiting and the most recent status event.

Nothing else answers this: `reconcile_sent_invoice_requests` covers invoice-request drafts only, and the dashboard's event rollup covers only the event-driven slice.

#### Scenario: Ask what is awaiting a response
- **WHEN** the user asks which claim emails were sent and whether Petcover responded
- **THEN** the agent lists one entry per Submission with its claim ids, days waiting, and last recorded event — or states plainly that no reply is recorded for it

#### Scenario: A Submission is one entry, not several
- **WHEN** several claims share a `draft_id`
- **THEN** they appear as a single Submission entry, since claims sharing a draft move together

### Requirement: Full claim detail including recorded figures
The agent SHALL be able to report one claim in full by id: its transaction, invoice line items, claimable subtotal, current flag, and every status event **with the dollar figures recorded on it** (claimed, paid, stated fixed excess, stated age contribution).

This is the agreed ceiling for "ask about the system": claim-level *why* is answerable; explaining code, docs, or specs is not, and the agent has no access to them.

#### Scenario: Ask why a claim is flagged
- **WHEN** the user asks why a given claim is flagged
- **THEN** the agent reports the flag text together with the events and figures that produced it

#### Scenario: Asked to explain the code
- **WHEN** the user asks how a module or the pipeline works internally
- **THEN** the agent says it can explain claim state but cannot read the code, rather than guessing at implementation
