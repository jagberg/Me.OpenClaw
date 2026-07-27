## ADDED Requirements

### Requirement: Every Petcover information request is recorded in a claim-optional register
The system SHALL maintain an `info_requests` register with one row per Petcover information-request email, keyed on the Gmail message id, carrying: the claim reference, the Sr, the request date, the recipient who owes the document, the pet named in the letter, the document requested (verbatim from the letter where available), the derived outcome, and a **nullable** `claim_id`.

`claim_id` is nullable because most historical requests can never be linked: `GABR-0305`, `GABR-0306`, `DC1-26-4751`, `DC1-27-5631` and `DC1-27-5628 Sr1` all predate every bank transaction on file (CSV coverage starts 2025-07-17). Recording them as `claim_id IS NULL` status events instead would put them in the "needs manual link" review queue, which assumes a claim exists to link them to — that queue could never drain.

#### Scenario: Request names a claim the system knows
- **WHEN** an information request cites a reference and Sr held by a claim
- **THEN** the register row carries that `claim_id`

#### Scenario: Request predates the transaction history
- **WHEN** an information request cites a reference no claim holds and none ever will
- **THEN** the register row is created with `claim_id = NULL` and is not queued for manual linking

#### Scenario: The same email is processed twice
- **WHEN** the register is populated again over mail it has already seen
- **THEN** no duplicate row is created, because rows are keyed on the Gmail message id

### Requirement: The register records who owes the document
Each request SHALL record the party obliged to supply the information, resolved from the email's recipients: a recipient matching a known `vet_contacts` address means that clinic owes it (clinic name and address recorded); any other non-Justin recipient means an unidentified vet owes it, recorded with the raw address; only Justin's own address means Justin owes it.

The obligation MUST NOT default to Justin when a recipient cannot be identified — silently reassigning a vet's obligation to Justin is the failure mode that loses claims. Sender address SHALL NOT be used to infer the party: `claims.au@petcovergroup.com` sends both kinds (`GABR-0305-Request for consult note` went to a vet, `GABR-0306 First Request for CF` went to Justin).

#### Scenario: Addressed to a known clinic
- **WHEN** a request is addressed to `info@kingsvet.com.au`, which `vet_contacts` maps to a merchant
- **THEN** the register records that the vet owes it, naming the clinic and its email

#### Scenario: Addressed to Justin
- **WHEN** a request is addressed only to Justin's own address
- **THEN** the register records that Justin owes it

#### Scenario: Addressed to an unrecognized address
- **WHEN** a request is addressed to a non-Justin address absent from `vet_contacts`
- **THEN** the register records that a vet owes it and shows the raw address, and does not attribute it to Justin

### Requirement: Outcome is inferred from later Petcover mail, never from a vet reply
A vet's reply goes to Petcover, not to Justin — all ten vet-addressed request threads in the mailbox contain exactly one message. The system SHALL therefore derive each request's outcome from whether later Petcover mail cites the same reference and Sr: `resolved` when a later `acknowledged`, `approved` or `settled` letter cites it; `suspended` when a later suspension letter cites it and nothing follows; `open` when nothing cites it afterwards and the treatment is under one year old; `expired` when nothing cites it afterwards and the treatment is over one year old.

The deadline is anchored on the **treatment date**, per the letter's own terms ("your claim must be submitted within one year of your pet receiving treatment") and the realized loss `ELD-25-2728 - Declined - Invoices over 12 months`.

#### Scenario: Claim moved on after the request
- **WHEN** a settlement letter later cites the same reference and Sr as a request
- **THEN** that request's outcome is `resolved` and it needs no action

#### Scenario: Request escalated to suspension and stopped
- **WHEN** a request is followed by a suspension letter citing the same reference and Sr, and nothing after that
- **THEN** the outcome is `suspended` and it is actionable

#### Scenario: Nothing ever followed
- **WHEN** no later Petcover email cites a request's reference and Sr, and the treatment date is under a year old
- **THEN** the outcome is `open` and it is actionable

#### Scenario: Past the one-year deadline
- **WHEN** no later Petcover email cites a request's reference and Sr, and the treatment date is over a year old
- **THEN** the outcome is `expired`

### Requirement: Expired requests are listed for manual handling, never presented as actionable
Requests whose outcome is `expired` SHALL appear as a plain list — reference, date, pet, vet, what was requested — with no action buttons and no place in the pending-actions derivation, so they cannot be mistaken for recoverable work.

#### Scenario: Expired request on the dashboard
- **WHEN** the register is displayed and a request is `expired`
- **THEN** it appears in a separate manual-handling list with no action control

#### Scenario: Expired request and pending actions
- **WHEN** pending actions are derived
- **THEN** no `expired` request contributes an action

### Requirement: Backfilling history never mutates claim state
A one-off backfill SHALL sweep the Petcover senders back two years and write **only** to `info_requests`. It MUST NOT write `claim_status_events`, MUST NOT update `vet_claims`, MUST NOT mark messages in `processed_emails`, and MUST NOT call the live reply-processing path.

`PETCOVER_STATUS_SINCE` exists because replaying old mail through the live path fabricates status events on current claims and mis-correlates acknowledgements. A backfill that ignored this would corrupt working claims in order to inventory dead ones.

#### Scenario: Backfill over two years of mail
- **WHEN** the backfill runs over mail predating `PETCOVER_STATUS_SINCE`
- **THEN** register rows are created and no claim's status, flag or event history changes

#### Scenario: Backfill interrupted
- **WHEN** the backfill fails partway (Gmail rate limit or connection reset, both observed live)
- **THEN** re-running it resumes rather than restarting, and creates no duplicate rows
