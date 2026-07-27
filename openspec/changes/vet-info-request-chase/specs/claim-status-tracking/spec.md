## MODIFIED Requirements

### Requirement: Classify Petcover reply emails into lifecycle events
The system SHALL poll `claims.au@petcovergroup.com`, `requiredinfo.au@petcovergroup.com`, and `accounts.au@petcovergroup.com` on the existing pipeline cycle (paginating past Gmail's page size so no reply is dropped, oldest-first so statuses never regress) and classify each new email into one of: `acknowledged`, `approved` (Petcover has assessed and approved the claim — carries the only dollar breakdown in the lifecycle; a dollar-less "payment processed" confirmation follows as `settled`), `info_requested`, `suspended`, `settled`, `declined`, `below_excess` (the condition is covered but the amount claimed is under the fixed excess — non-terminal, invoice retained), `ignore` (recognized noise, e.g. "Automatic reply:" instant receipts — dropped without review), or `unclassified` (a real reply we couldn't classify — queued for manual review, and never written to the claim's status). `approved` and `below_excess` are recognized from body text (confirmed live phrases: "Your claim has been approved", "Claim assessment outcome: Under excess") since their real subject is a generic template, not a distinct keyword. Emails from `marketing.au@petcovergroup.com` SHALL be excluded at the query level, not classified; emails older than the configured `PETCOVER_STATUS_SINCE` date SHALL be excluded at the query level (first-run backfill guard — historical replies about long-settled claims must not be ingested or mis-correlated).

`info_requested` SHALL be tested **before** `suspended`, and any email from `requiredinfo.au@petcovergroup.com` SHALL classify as `info_requested` on the strength of its sender alone. Both rules exist because of live misclassifications found 2026-07-27:

- The information-request letter contains the sentence *"Your claim will be suspended until we have the required information"* — a statement about its own future, not a suspension. With `suspended` ordered first, every request was recorded as a suspension: the live database held **zero** `info_requested` events and two `suspended` ones, both of which were requests. A genuine suspension letter also exists (`Petcover Claim DC1-27-5628 SR1 - Claim suspended`, 29 Jan 2026), so conflating the two destroys the distinction between "a document is missing" and "we have stopped assessing".
- The vet-addressed cover note carries one sentence of body, no reference in the body at all, and detail only in an attachment whose text does not come back through `full_message_text`. It matched nothing and was recorded as `unclassified` — the one classification that produces no action. `requiredinfo.au@` is a dedicated single-purpose channel, so the sender is a stronger and stabler signal than any phrase.

Classification therefore requires the sender, which SHALL be passed to the classifier along with the subject and body.

#### Scenario: Subject matches a known pattern
- **WHEN** a reply's subject contains a recognized keyword (e.g. "Acknowledgement Letter", "suspended", "Request for information", "Settlement EFT", "Declined")
- **THEN** it is classified accordingly without needing to read the body

#### Scenario: Generic-subject letter classified from body content
- **WHEN** a reply's subject is a generic template (e.g. "Petcover Insurance Claim for Ari") but its body states the claim has been approved, or that it is under the fixed excess
- **THEN** it is classified `approved` or `below_excess` respectively, not left `unclassified`

#### Scenario: Subject is ambiguous or generic
- **WHEN** a reply's subject doesn't match any known keyword (e.g. templated subjects reused across claim types)
- **THEN** the body text is checked as a fallback before falling back further to `unclassified`

#### Scenario: Information-request letter that mentions its own future suspension
- **WHEN** a letter titled "Further Information Required" states that the claim will be suspended until the information is received
- **THEN** it is classified `info_requested`, not `suspended`

#### Scenario: A genuine suspension letter
- **WHEN** a letter states the claim has been suspended and carries no information-request wording
- **THEN** it is classified `suspended`

#### Scenario: One-sentence cover note to a vet
- **WHEN** an email from `requiredinfo.au@petcovergroup.com` carries no recognizable phrase in its subject or body
- **THEN** it is classified `info_requested` on the sender alone, and is never left `unclassified`

#### Scenario: Marketing email arrives
- **WHEN** an email from `marketing.au@petcovergroup.com` is polled
- **THEN** it is excluded from classification entirely and never appears as a claim status event

#### Scenario: below_excess is non-terminal
- **WHEN** a `below_excess` event correlates to a claim
- **THEN** the claim's status becomes `below_excess`, its invoice/`invoice_data` is retained, and it is never treated as `declined` (terminal)

### Requirement: Surface action items and settlement reconciliation on the dashboard
The system SHALL show, on the dashboard: claims with an open `info_requested` or `suspended` event that Justin has not confirmed resolved (needs Justin's action), and for `settled` claims, the paid amount alongside the originally claimed amount.

An open information request SHALL additionally state **who owes the document** and **how long the claim has left**: the clinic name and email address when a vet owes it, and the days remaining until one year from the treatment date. Without the first, Justin cannot tell whether the next move is his or a phone call to the vet; without the second, an item three weeks from expiry looks identical to one with ten months left.

#### Scenario: Open info request with no response yet
- **WHEN** a claim's latest event is `info_requested` or `suspended` and it has not been confirmed resolved
- **THEN** it appears in a "needs your action" list on the dashboard

#### Scenario: Open info request the vet owes
- **WHEN** an open information request was addressed to a known clinic
- **THEN** the dashboard entry names that clinic and its email address, and states the days remaining before the one-year deadline

#### Scenario: Settled claim with a different paid amount
- **WHEN** a claim's `settled` event includes a paid amount that differs from the originally claimed amount
- **THEN** both amounts are shown side by side on the dashboard (e.g. after an excess deduction) rather than showing only one

## ADDED Requirements

### Requirement: A reply's recipients are recorded so the obliged party is known
`process_reply` SHALL receive the email's `To:` and `Cc:` recipients in addition to its subject and body, and SHALL record on the status event which party owes any requested information, resolved as specified in the `vet-info-request-register` capability.

Today the function takes only `(email_id, subject, body)`, so nothing in the system can distinguish "the vet owes consult notes" from "you owe a completed claim form" — the distinction that decides Justin's next action. The sender cannot substitute: `claims.au@` sends both kinds.

#### Scenario: Request addressed to the vet
- **WHEN** an information request is polled whose recipient is a vet clinic
- **THEN** the recorded event states that the vet owes the document, naming the clinic

#### Scenario: Request addressed to Justin
- **WHEN** an information request is polled addressed only to Justin
- **THEN** the recorded event states that Justin owes the document

### Requirement: An outstanding information request outranks every other pending action
Information-request actions SHALL sort ahead of all other action kinds, and SHALL be ordered among themselves by **days remaining until one year from the treatment date**, soonest first — not by charge age. A vet-owed request SHALL surface as its own action kind, distinct from the Justin-owed confirm-resolved action, and SHALL carry the clinic's name and email address.

Ordering today is by transaction date with `confirm_resolved` third in priority, so an information request competes with a missing condition on the age of the charge. The deadline that actually ends the claim is one year from treatment, stated in Petcover's own letter and realized live as `ELD-25-2728 - Declined - Invoices over 12 months`.

#### Scenario: Info request alongside other actions
- **WHEN** pending actions include an information request and other kinds
- **THEN** the information request is listed first

#### Scenario: Two info requests with different deadlines
- **WHEN** two information requests are outstanding and one is closer to its one-year deadline
- **THEN** the one closer to expiry is listed first, regardless of which charge is older

#### Scenario: Vet-owed request card
- **WHEN** a vet-owed information request is surfaced as an action
- **THEN** it is a distinct action kind naming the clinic and its email address, so Justin knows who to chase

### Requirement: The daily nudge reports information requests regardless of the stale-action threshold
The once-daily stale-action nudge SHALL include every outstanding information request irrespective of `ACTION_NUDGE_DAYS`, and SHALL name the request closest to its one-year deadline rather than the oldest action.

These are the actions with a documented history of being ignored until the claim was lost; suppressing them until a generic age threshold passes is the behavior this change exists to remove.

#### Scenario: Information request younger than the nudge threshold
- **WHEN** the daily nudge runs and an information request is outstanding but younger than `ACTION_NUDGE_DAYS`
- **THEN** the nudge still reports it

#### Scenario: Nudge names the expiring item
- **WHEN** the daily nudge reports outstanding information requests
- **THEN** it names the one closest to its one-year deadline and how many days remain
