# claim-status-tracking Specification

## Purpose
Track the lifecycle of submitted Petcover vet claims by polling Petcover's reply mailboxes, classifying replies into status events, correlating them to the originating claim submissions, and surfacing action items and settlement reconciliation on the dashboard.
## Requirements
### Requirement: Learn and store Petcover's claim reference number
Petcover assigns its own claim reference (e.g. `DC1-27-5628`, `GABR-0305`, `ELD-24-2146` — format has changed over time) once it acknowledges a submitted claim; this reference is distinct from the policy number and from the internal `vet_claims.id`. The system SHALL extract this reference from the acknowledgement reply and store it against the originating `vet_claims` row.

#### Scenario: Acknowledgement reply contains a claim reference
- **WHEN** an email from `claims.au@petcovergroup.com` matches the acknowledgement pattern and contains a claim reference in a recognized format
- **THEN** the reference is stored on the corresponding `vet_claims` row and used for all future correlation of replies about that claim

#### Scenario: Reference format not recognized
- **WHEN** an acknowledgement reply's claim reference doesn't match any known pattern
- **THEN** the claim is flagged `unclassified — reference format not recognized` rather than guessing or discarding the email

### Requirement: Classify Petcover reply emails into lifecycle events
The system SHALL poll `claims.au@petcovergroup.com`, `requiredinfo.au@petcovergroup.com`, and `accounts.au@petcovergroup.com` on the existing pipeline cycle (paginating past Gmail's page size so no reply is dropped, oldest-first so statuses never regress) and classify each new email into one of: `acknowledged`, `approved` (Petcover has assessed and approved the claim — carries the only dollar breakdown in the lifecycle; a dollar-less "payment processed" confirmation follows as `settled`), `info_requested`, `suspended`, `settled`, `declined`, `below_excess` (the condition is covered but the amount claimed is under the fixed excess — non-terminal, invoice retained), `ignore` (recognized noise, e.g. "Automatic reply:" instant receipts — dropped without review), or `unclassified` (a real reply we couldn't classify — queued for manual review, and never written to the claim's status). `approved` and `below_excess` are recognized from body text (confirmed live phrases: "Your claim has been approved", "Claim assessment outcome: Under excess") since their real subject is a generic template, not a distinct keyword. Emails from `marketing.au@petcovergroup.com` SHALL be excluded at the query level, not classified; emails older than the configured `PETCOVER_STATUS_SINCE` date SHALL be excluded at the query level (first-run backfill guard — historical replies about long-settled claims must not be ingested or mis-correlated).

#### Scenario: Subject matches a known pattern
- **WHEN** a reply's subject contains a recognized keyword (e.g. "Acknowledgement Letter", "suspended", "Request for information", "Settlement EFT", "Declined")
- **THEN** it is classified accordingly without needing to read the body

#### Scenario: Generic-subject letter classified from body content
- **WHEN** a reply's subject is a generic template (e.g. "Petcover Insurance Claim for Ari") but its body states the claim has been approved, or that it is under the fixed excess
- **THEN** it is classified `approved` or `below_excess` respectively, not left `unclassified`

#### Scenario: Subject is ambiguous or generic
- **WHEN** a reply's subject doesn't match any known keyword (e.g. templated subjects reused across claim types)
- **THEN** the body text is checked as a fallback before falling back further to `unclassified`

#### Scenario: Marketing email arrives
- **WHEN** an email from `marketing.au@petcovergroup.com` is polled
- **THEN** it is excluded from classification entirely and never appears as a claim status event

#### Scenario: below_excess is non-terminal
- **WHEN** a `below_excess` event correlates to a claim
- **THEN** the claim's status becomes `below_excess`, its invoice/`invoice_data` is retained, and it is never treated as `declined` (terminal)

### Requirement: Correlate a reply to the originating claim submission
A Petcover reference identifies a Condition Thread — one (pet, condition) pairing whose reference is reused for the life of the condition — not a Submission. The system SHALL correlate each classified reply using, in order of confidence: (1) an exact (reference, Sr) match — the event attaches to that single claim; (2) a reference-only match — the event attaches to the thread's non-terminal claims only (never `settled`/`declined`); (3) for replies with no stored reference (acknowledgements learning it): candidates are un-referenced claims in a submitted-and-awaiting-reply status for the printed pet (nickname-tolerant), narrowed by the reply's printed condition matching the claim's condition text (case-insensitive) — Petcover's printed condition is authoritative in their letters; if condition matching does not decide it, the reply SHALL be assumed to belong to the most recently sent matching submission, and multiple same-day replies SHALL map newest-reply→newest-sent working backwards. Transaction-date proximity SHALL NOT be required: a claim's transaction can be a year older than its submission (confirmed real case), so date windows reject genuine matches.

#### Scenario: Letter cites reference and serial
- **WHEN** a reply contains a stored reference and an Sr held by one claim
- **THEN** the event is attached to that claim only

#### Scenario: Reference present and known, no serial
- **WHEN** a reply contains a claim reference already stored on `vet_claims` rows and cites no Sr
- **THEN** the event is attached to that thread's non-terminal claims only; settled and declined claims are untouched

#### Scenario: Acknowledgement resolved by condition content
- **WHEN** an un-referenced acknowledgement prints pet "Ari" and condition "Arthritis", and exactly one awaiting submission holds claims with condition text "Arthritis"
- **THEN** the reference and Sr are learned onto the matching submission's claims

#### Scenario: Condition decides nothing — recency fallback
- **WHEN** an acknowledgement's printed condition matches no awaiting claim's condition text (Petcover re-conditioned the document)
- **THEN** the reply is attributed to the most recently sent awaiting submission for that pet, and the claim's own condition text is left unchanged

#### Scenario: Two acknowledgements the same day
- **WHEN** two un-referenced acknowledgements for one pet arrive the same day and two submissions are awaiting
- **THEN** each acknowledgement attaches to a distinct awaiting submission (learning a reference removes that submission from the un-referenced pool, so the second ack cannot collide onto the first's submission); the recency rule orders which is tried first, and any residual mis-pairing when conditions are indistinguishable is correctable via manual linking

#### Scenario: Acknowledgement without an extractable reference
- **WHEN** an acknowledgement correlates to a submission but no claim reference could be extracted from it
- **THEN** the claim is flagged `unclassified — reference format not recognized` rather than silently proceeding without one

#### Scenario: Manually linking an unattached reply
- **WHEN** Justin links an unattached event to a claim from the dashboard
- **THEN** the event is attached to that claim only — the claim's status is NOT rewritten (a late-linked old email must not regress a settled claim), and linking to a nonexistent claim is refused

### Requirement: Persist an append-only status history per claim
The system SHALL record every classified event to a `claim_status_events` log rather than overwriting the claim's current status, so the full sequence (e.g. suspended → info supplied → settled) remains visible.

A transition the state machine refuses SHALL still be recorded as an event, with the detail it carries today. Whether that refusal is also written to the claim's `flag` column depends on why the mail was being read:

- When mail is polled **normally**, a refused transition is genuinely surprising — something arrived out of order — and the system SHALL flag the claim, naming both states, so it is visible.
- When mail is **replayed deliberately** (`poll_petcover_status(reread=True)`, which re-applies a classifier or extraction fix to mail already ingested), a refused transition is the expected outcome for every claim whose state has moved on since the mail was first read. The system SHALL NOT write those refusals to the claim's `flag`. The event, and its detail, are recorded unchanged.
- During a replay, a settlement finding the re-read genuinely produces SHALL reach the `flag` column, instead of losing precedence to a refusal that was not written.

Live evidence, 2026-08-05: a recovery replay of five approval letters left `refused settled -> acknowledged` text on six claims (#1, #2, #6, #7, #8, #13). On claim #2 that text displaced the finding the replay existed to produce — `claimable subtotal not recorded` — because `process_reply` prefers a refusal over a settlement flag. Six expected consequences of asking for a replay read as six new failures, and hid one real one.

The knowledge that a replay is in progress SHALL be passed explicitly from the poller, not inferred from the event already existing: a genuinely late-arriving letter is indistinguishable by that test, and inferring it would silence the case the flag exists for.

#### Scenario: Claim receives multiple events over time
- **WHEN** a claim is acknowledged, then later suspended, then later settled
- **THEN** all three events exist in the history, each with its own timestamp and source email, and the claim's current status reflects the latest event

#### Scenario: A refused transition during ordinary polling
- **WHEN** a letter arrives out of order and its event is not a declared transition from the claim's current state
- **THEN** the event is recorded, the status is left alone, and the claim is flagged naming both states

#### Scenario: A refused transition during a deliberate replay
- **WHEN** `poll_petcover_status(reread=True)` re-reads an acknowledgement against a claim that has since settled
- **THEN** the event is recorded with its full detail, the status is left alone, and the claim's `flag` is NOT overwritten with the refusal

#### Scenario: A replay produces a genuine finding
- **WHEN** a replay re-reads an approval letter whose transition is refused, and the settlement check raises a finding for that claim
- **THEN** the finding is written to the claim's `flag`, rather than being suppressed behind the unwritten refusal

### Requirement: Surface action items and settlement reconciliation on the dashboard
The system SHALL show, on the dashboard: claims with an open `info_requested` or `suspended` event that Justin has not confirmed resolved (needs Justin's action), and for `settled` claims, the paid amount alongside the originally claimed amount.

#### Scenario: Open info request with no response yet
- **WHEN** a claim's latest event is `info_requested` or `suspended` and it has not been confirmed resolved
- **THEN** it appears in a "needs your action" list on the dashboard

#### Scenario: Settled claim with a different paid amount
- **WHEN** a claim's `settled` event includes a paid amount that differs from the originally claimed amount
- **THEN** both amounts are shown side by side on the dashboard (e.g. after an excess deduction) rather than showing only one

### Requirement: An info-requested or suspended claim stays flagged until Justin explicitly confirms it resolved
A new event arriving on a claim (even `settled` or `declined`) SHALL NOT automatically clear its "needs your action" status. The claim SHALL only leave the action list when Justin explicitly confirms it resolved via the dashboard, so a claim isn't silently dropped when Petcover's own follow-through is inconsistent (real pattern observed: repeated "request for X" emails on the same claim before resolution).

#### Scenario: New event arrives on an already-flagged claim
- **WHEN** a claim already in the "needs your action" list (e.g. `suspended`) receives a new event (e.g. `settled`)
- **THEN** the claim remains visible on the action list, now showing both events, until Justin confirms it resolved

#### Scenario: Justin confirms a claim resolved
- **WHEN** Justin clicks "confirm resolved" on a flagged claim
- **THEN** the claim is removed from the "needs your action" list; this confirmation is itself recorded as an event in the claim's status history

### Requirement: An information request records the document it asked for
Petcover's letter names the document in a fixed template phrase (confirmed live: *"To assess your claim, we need a copy of / Consultation notes dated 18/05/2026"*). The system SHALL extract that phrase and record it on the `info_requested` event alongside who owes it, using pattern matching only — no LLM. When no recognized phrase is present the system SHALL record no document rather than inferring one.

#### Scenario: The letter names the document
- **WHEN** an information-request letter states `we need a copy of Consultation notes dated 18/05/2026`
- **THEN** the event records the requested document as that phrase

#### Scenario: No recognized phrase
- **WHEN** an information-request letter carries no recognized "we need a copy of" / "please provide the following" phrasing
- **THEN** no requested document is recorded, and the claim's handling is otherwise unchanged

#### Scenario: The trailing template boilerplate is not part of the document
- **WHEN** the requested-document phrase is followed by the letter's standard boilerplate (`Please note we cannot process the claim…`, `You can reach us on…`)
- **THEN** the recorded document stops at the requested item and excludes the boilerplate

### Requirement: Unanswered vet-directed requests are identifiable
A vet's reply to Petcover never reaches Justin's mailbox, so "unanswered" SHALL mean the claim still carries an unresolved information request owed by the vet — the same unresolved determination the dashboard's needs-action list uses. The system SHALL be able to list those claims with the clinic, the requested document, the age of the request, and the days remaining against the treatment-anchored one-year submission deadline. A request whose deadline has passed SHALL be excluded from that list.

The deadline SHALL be anchored on the date the pet was **treated**, not on the bank charge: Petcover's own wording is *"within one year of your pet receiving treatment"*, and the two dates differ by an unbounded amount (confirmed live: treated 19 Jun and 30 Jun 2026, both charged 06/07/2026 — over-granting 17 and 6 days). The treatment date SHALL be the **earliest** date the attached invoice states, its own or any line item's, because an invoice billing several visits expires on its oldest one. With no invoice attached the system SHALL fall back to the transaction date and SHALL disclose that the anchor was assumed rather than presenting it as known.

#### Scenario: A vet-owed request is outstanding
- **WHEN** a claim's latest unresolved information request is owed by the vet
- **THEN** it appears in the unanswered list with the clinic name and email, the requested document, days outstanding, and days remaining to the deadline

#### Scenario: Justin confirms it resolved
- **WHEN** Justin confirms the information request resolved
- **THEN** the claim leaves the unanswered list

#### Scenario: Past the treatment deadline
- **WHEN** an unanswered request's claim is past the one-year treatment deadline
- **THEN** it is excluded from the unanswered list rather than nudged indefinitely

#### Scenario: The request was addressed to Justin
- **WHEN** a claim's outstanding information request is owed by Justin, not the vet
- **THEN** it does not appear in the vet-unanswered list

#### Scenario: The vet has already sent it to Petcover directly
- **WHEN** a claim's latest information request has `owed_by: "petcover"` (vet-reply-auto-resolves-info-request)
- **THEN** it does not appear in the vet-unanswered list — the vet is no longer who Justin needs to chase

### Requirement: A claim awaiting Petcover clarification is a distinct pending-action state
The system SHALL support an `awaiting_petcover_clarification` pending-action state, entered only when a claim is queued into an open clarification draft to Petcover (see `settlement-clarification-email`) — never while it is merely showing the pre-send settlement-review card, since at that point nothing has been asked of Petcover yet. It is distinct from `info_requested`/`suspended` on the dashboard's "needs your action" list, since this state means Justin is waiting on Petcover rather than needing to act himself. It follows the same persistence rule as `info_requested`/`suspended`: a new unrelated event SHALL NOT clear it — only an exact-match auto-resolved reply or an explicit Acceptable/dismiss action does.

#### Scenario: Claim enters the clarification state
- **WHEN** a claim is queued into an open clarification draft via "More Info"
- **THEN** its pending-action state becomes `awaiting_petcover_clarification` and it appears on the dashboard as waiting on Petcover, not as needing Justin's action

#### Scenario: Not yet entered while only the review card is showing
- **WHEN** a claim carries an open Check B or unrecorded-subtotal flag and its settlement-review card is showing, but "More Info" has not been clicked
- **THEN** the claim is NOT in `awaiting_petcover_clarification` — no email has been asked for yet

#### Scenario: Distinct from needs-your-action
- **WHEN** the dashboard renders pending-action cards
- **THEN** `awaiting_petcover_clarification` claims are visually/semantically distinguished from `info_requested`/`suspended` claims, since the latter need Justin to do something and the former do not

#### Scenario: Cleared only by resolve or explicit action
- **WHEN** a claim in `awaiting_petcover_clarification` receives any event other than an exact-match clarification reply
- **THEN** it remains in that state until either an exact match resolves it or Justin explicitly clicks Acceptable

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

