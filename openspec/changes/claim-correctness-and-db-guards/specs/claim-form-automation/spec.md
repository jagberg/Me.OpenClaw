## ADDED Requirements

### Requirement: "Redo" is one named operation, chosen deliberately

The system SHALL implement a redo operation for a claim, and the phrase "redo claim #N" SHALL resolve to exactly one of three operations, chosen by Justin and recorded in this change's `design.md`:

1. **Rebuild the draft** — same `invoice_data`, regenerate the form PDF and Gmail draft, delete the old draft. For "the draft is wrong or missing".
2. **Re-extract the invoice** — discard `invoice_data`, re-read the source PDF. For "the figures are wrong".
3. **Full reset** — back to `pending_match` and re-hunt the email. For "wrong invoice entirely".

Until that choice is recorded, the operation SHALL NOT be built. Picking one from the phrase alone would fabricate a decision, and the two prior uses of the phrase are consistent with more than one reading.

The operation SHALL name which of the three it performed in its confirmation, so a wrong choice is visible immediately rather than discovered later.

#### Scenario: Redo is requested and the semantics are recorded

- **WHEN** redo is invoked on a claim and the chosen semantics are recorded
- **THEN** the system SHALL perform that operation
- **AND** the confirmation SHALL state which operation ran and what it changed

#### Scenario: Redo is requested on a sent claim

- **WHEN** redo is invoked on a claim already `sent` to the insurer
- **THEN** the system SHALL refuse and explain, because rebuilding a submission already with Petcover changes what they were sent without telling them

#### Scenario: Redo is requested and the draft is intact

- **WHEN** redo is invoked because a draft appears missing, but the draft exists
- **THEN** the system SHALL say so and name the draft, rather than rebuilding on a false premise

### Requirement: A claim draft's subject names its claims

`claim_forms` SHALL include the claim ids in the Gmail draft subject, e.g. `Vet claim — Aari (#7, #6)`. Two submissions for the same pet are otherwise indistinguishable in Gmail, and `pipeline.DRAFT_SEARCH_LINK` searches on exactly that subject — which is how an existing draft was read as deleted.

This SHALL NOT affect reply correlation, which runs against Petcover's own reply subject and never against the subject sent.

#### Scenario: Two drafts exist for the same pet

- **WHEN** two claim drafts exist for one pet
- **THEN** each subject SHALL name its own claim ids
- **AND** `DRAFT_SEARCH_LINK` SHALL resolve to the intended draft
