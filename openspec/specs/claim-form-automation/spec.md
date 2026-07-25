# claim-form-automation Specification

## Purpose
Turn a matched claim into a ready-to-send insurer submission: fill the pet's claim template, attach the right invoice pages, create a Gmail draft — and never send it. `claim_forms.py`.

See ADR-0006 (logical boundary), ADR-0012 (continuation box).

*(Consolidated 2026-07-25 from the `vet-claim-automation` and `telegram-claim-actions` deltas. This file previously said base requirements were "pending sync"; they never were.)*

## Requirements

### Requirement: Fill the pet-specific claim template, and only for pets with a defined process
The system SHALL look up the claim process by the pet assigned to the transaction and SHALL only fill and draft for pets whose process is defined, once a claim reaches `matched`.

Aari is on Petcover — process known: a real fillable PDF via `pypdf`, submitted to their claims address. Echo is on Bow Wow Insurance — **process still undefined** as of 2026-07-25.

#### Scenario: Matched claim ready to fill (known process)
- **WHEN** a claim for a pet with a defined process moves to `matched` with transaction and invoice both present
- **THEN** the insurer's PDF is filled and stored, and the claim status becomes `drafted`

#### Scenario: Matched claim for a pet with no defined process
- **WHEN** a claim for Echo moves to `matched`
- **THEN** it stays `matched` and is flagged "Bow Wow Insurance claim process not yet defined" — nothing is filled or drafted, and no process is guessed

#### Scenario: Required claim field missing from extracted data
- **WHEN** extraction is missing a field the template requires
- **THEN** the claim stays `matched` and is flagged for Justin to supply, never auto-advanced

### Requirement: The condition is supplied by Justin, never derived
The system SHALL leave "condition being claimed for" unset and flag the claim for manual entry. It cannot be derived from invoice line items — real invoices list procedures, medication and totals, not a diagnosis. This is a hard rule: never guess a required claim field.

#### Scenario: Claim reaches matched with no condition
- **WHEN** transaction and invoice are matched but no condition was supplied
- **THEN** the claim stays `matched`, is flagged for entry, and is NOT advanced to `drafted`

#### Scenario: Justin supplies the condition
- **WHEN** the condition text is entered via dashboard or Telegram
- **THEN** the claim proceeds through the normal fill/draft flow using that text

### Requirement: Previously-used conditions are offered as choices
Conditions already claimed for a pet SHALL be offered as one-tap options when a claim needs one, alongside free-text entry.

**This was recorded as a deferred enhancement in the original change and has since shipped** (`prior_conditions` + the Telegram condition keyboard). The original delta's forward-looking note — store condition text against the pet, not only the single claim, so a pick-list stays possible — is what made it cheap to add.

#### Scenario: Repeat condition for the same pet
- **WHEN** a claim for a pet with prior recorded conditions needs one
- **THEN** those conditions are offered as selectable options alongside entering a new one

### Requirement: One invoice spanning multiple conditions fills multiple form rows
When per-item condition assignments are recorded, the fill SHALL emit one form row per distinct condition, each charged the sum of that condition's item amounts, skipping items marked not-claimable — instead of one condition and charge for the whole claim.

#### Scenario: Grouped rows from per-item assignments
- **WHEN** a claim has item assignments of $390 to one condition and $135 to another
- **THEN** the form has two condition rows charged $390 and $135

### Requirement: Draft, never auto-send, the claim email
The system SHALL create a Gmail draft addressed to the insurer with the filled claim form attached, and SHALL NOT call Gmail's send endpoint on Justin's behalf. Once Justin sends it himself, the claim SHALL advance to `sent` so `claim-status-tracking` has something to attach Petcover's replies to.

*Correction (2026-07-25): earlier text here said "using the `gmail.send`-scoped draft API". The scope is `gmail.compose`. Worth being exact, because `gmail.compose` does grant send capability — the no-send guarantee is enforced by our code, not by the token. See `email-ingestion`.*

#### Scenario: Claim drafted
- **WHEN** a claim reaches `drafted`
- **THEN** a Gmail draft exists with the filled form attached, and both dashboard and Telegram surface it for Justin to review and send

#### Scenario: Draft creation fails
- **WHEN** the Gmail draft-create call fails
- **THEN** the claim stays `matched` and the failure is surfaced visibly, consistent with the project's failure-visibility rule

#### Scenario: Justin sends the draft
- **WHEN** Justin marks a `drafted` claim sent (dashboard or Telegram — no reliable automatic signal exists that a draft was sent)
- **THEN** every `drafted` claim sharing that draft advances to `sent`, since a batch submission is one email

### Requirement: A submission carries at most four invoices
A batch submission SHALL group at most four invoices for the same pet into one draft with one form. Claims sharing a `draft_id` move together thereafter.

#### Scenario: More than four claims ready for one pet
- **WHEN** five claims for the same pet are ready to draft
- **THEN** they are split across submissions of at most four, each with its own draft

### Requirement: The right invoice pages are attached, not the whole bundle
The system SHALL extract the specific invoice's pages from a multi-invoice document and attach those, so the insurer receives the visit being claimed rather than an unrelated bundle.

#### Scenario: Bulk document covering several visits
- **WHEN** the matched invoice came from a document containing several invoices
- **THEN** only that invoice's pages are attached to the submission

### Requirement: The continuation box defaults to ticked
Every generated claim form SHALL have the "continuation of a previously claimed condition" box ticked (ADR-0012). Justin flips it during draft review for a genuinely new condition. The successor behaviour — deriving it from Condition Thread existence — is recorded in ADR-0012 and remains unbuilt.

#### Scenario: Any claim form is generated
- **WHEN** a form is filled for a single claim or a batch
- **THEN** the continuation field is ticked

### Requirement: The invoice-request email carries the visit details
The system SHALL draft (never send) an invoice-request email to the vet using Justin's template: visit date (`dd-MMM-yyyy`), pet name and surname (falling back to a generic placeholder when unassigned), the charged amount, and a sign-off.

#### Scenario: Invoice request for a known pet
- **WHEN** a request is drafted for an Aari transaction on 2025-08-08 charged $44.75
- **THEN** the body names the visit as 08-Aug-2025, the pet as Aari Goldberg, and the amount as $44.75

### Requirement: Condition and pet input accepted from either surface
Condition text and pet assignment SHALL be accepted from Telegram as well as the dashboard, applying the identical update, so a claim can be unblocked from either. Pet may additionally be assigned automatically from patient facts printed on the matched invoice.

#### Scenario: Condition supplied via Telegram
- **WHEN** condition text is sent for a claim at `matched` missing only that
- **THEN** it is set exactly as the dashboard route would set it, and the claim becomes eligible to advance

#### Scenario: Pet named on the invoice
- **WHEN** the matched invoice prints a patient name matching a known pet, including the nickname table
- **THEN** the pet is assigned from that document rather than asked for

### Requirement: On-demand advance for a single claim
The system SHALL allow triggering the matched→drafted advance for one claim on demand, independent of the scheduled interval, reusing the same fill/draft logic and validation.

#### Scenario: Process an already-complete claim
- **WHEN** the advance is triggered for a `matched` claim with all required fields
- **THEN** it is filled and drafted immediately, without waiting for the next tick

#### Scenario: Process a claim still missing a field
- **WHEN** the advance is triggered for a claim still missing condition or pet
- **THEN** it stays `matched` and the response names the missing field
