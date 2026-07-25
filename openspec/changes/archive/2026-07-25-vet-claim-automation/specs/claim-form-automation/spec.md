## ADDED Requirements

### Requirement: Fill the pet-specific insurance claim template from matched transaction+invoice data
Justin has two pets on two different insurers — Aari on Petcover (process known: real fillable PDF, `pypdf`, claims.au@petcovergroup.com), Echo on Bow Wow Insurance (process not yet defined — Justin to clarify with them). The system SHALL look up the claim process by the pet assigned to the transaction (see `vet-payment-detection`'s pet-attribution requirement) and SHALL only proceed to fill/draft for pets with a defined process, once a transaction reaches `matched` status.

#### Scenario: Matched claim ready to fill (Aari / Petcover — known process)
- **WHEN** a `vet_claims` row for Aari moves to `matched` (transaction + invoice both present)
- **THEN** the Petcover PDF is filled and stored as a generated file, and the claim status becomes `drafted`

#### Scenario: Matched claim for Echo (Bow Wow Insurance — process undefined)
- **WHEN** a `vet_claims` row for Echo moves to `matched`
- **THEN** the claim stays at `matched` and is flagged "Bow Wow Insurance claim process not yet defined" — no attempt to fill or draft anything until Justin supplies their actual process (template format, submission method, required fields)

#### Scenario: Required claim field missing from extracted data
- **WHEN** the invoice extraction is missing a field the template requires (e.g. itemized services)
- **THEN** the claim is left in `matched` (not auto-advanced to `drafted`) and flagged on the dashboard for Justin to fill manually

### Requirement: Condition field defaults to manual entry, with a reusable condition history as a deferred enhancement
The system SHALL leave the claim's "condition being claimed for" field unset and flag the claim for manual entry, since it cannot be reliably derived from invoice line items alone (confirmed: real invoices list procedures/medication/totals, not a diagnosis). A future change SHALL let Justin supply the condition via a chat channel (Telegram/WhatsApp, same infrastructure as the deferred chat-review-and-send capability) and record it so it can be offered as a pick-list option on the next claim for the same pet, instead of re-entering free text each time — deferred, not built in this change.

#### Scenario: Claim reaches matched status with no condition source
- **WHEN** a `vet_claims` row has transaction + invoice matched but no condition text was supplied
- **THEN** the claim stays at `matched`, is flagged on the dashboard for manual entry, and is NOT auto-advanced to `drafted`

#### Scenario: Justin manually supplies the condition (v1 interim path)
- **WHEN** Justin enters the condition text via the dashboard for a `matched` claim
- **THEN** the claim proceeds through the normal fill/draft flow using that text

#### Scenario: Recognized condition offered as a pick-list option (deferred, not built in this change)
- **WHEN** a future claim for the same pet is flagged for manual condition entry, and a prior condition has already been recorded for that pet
- **THEN** the (future) chat-based flow offers the prior condition(s) as selectable options alongside "enter a new condition" — out of scope for this change, recorded here so the interim manual-entry design doesn't foreclose it (e.g. store condition text against the pet, not just the single claim, once built)

### Requirement: Draft, never auto-send, the claim email
The system SHALL create a Gmail draft (using the `gmail.send`-scoped draft API) addressed to the insurer with the filled claim form attached, and SHALL NOT call Gmail's send endpoint on Justin's behalf.

#### Scenario: Claim drafted
- **WHEN** a claim reaches `drafted` status
- **THEN** a Gmail draft exists with the filled form attached, and the dashboard links to it for Justin to review and send himself

#### Scenario: Draft creation fails
- **WHEN** the Gmail draft-create call fails
- **THEN** the claim stays at `matched` and the failure is surfaced visibly, consistent with the existing Gemini-failure visibility requirement
