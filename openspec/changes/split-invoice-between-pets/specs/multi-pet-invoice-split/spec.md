## ADDED Requirements

### Requirement: One invoice's claimable amount can be apportioned between pets
A single vet invoice can treat more than one pet (confirmed live: The Shire Veterinary Caringbah, $407.56 on 2026-07-06, Aari $35 and Echo the remainder). A claim carries exactly one pet, so the system SHALL support splitting a matched claim into **one claim per pet**, each carrying its own share of the invoice's claimable subtotal.

The split SHALL preserve the invoice: every resulting claim references the same matched email, invoice number and invoice total, and each attaches the same invoice pages. Only the claimable share differs per claim. The bank charge stays the ceiling for the **sum** of the shares (ADR-0007).

Shares SHALL come from Justin. The system MUST NOT infer which pet a line item belongs to, even when the invoice itemization would allow a guess — that is the same class of inference the `condition_text` hard rule forbids.

#### Scenario: Two pets, one invoice, one amount given
- **WHEN** Justin says Aari's share of claim #1 is $35 and confirms the split
- **THEN** claim #1 keeps Aari with a claimable amount of $35, a sibling claim is created for Echo with the remaining claimable amount, both carry the same invoice and matched email, and both are reported with their claim ids

#### Scenario: Shares stated for every pet
- **WHEN** Justin states an explicit amount for each pet in the split
- **THEN** those amounts are used verbatim and no remainder arithmetic is applied

#### Scenario: More than one share left implicit
- **WHEN** three or more pets are named and more than one share is left unstated
- **THEN** nothing is split and Justin is asked for the missing amounts — a remainder is only derivable when exactly one is missing

#### Scenario: Shares exceed the invoice
- **WHEN** the stated shares sum to more than the invoice's claimable subtotal (beyond a 1-cent rounding tolerance)
- **THEN** the split is refused with a message naming the subtotal and the stated sum, and no claim is created or changed

#### Scenario: Shares fall short of the claimable subtotal
- **WHEN** the stated shares sum to less than the claimable subtotal
- **THEN** the split proceeds and each affected claim is flagged with the unapportioned remainder, in the same shape as the existing `possible additional invoice — unexplained $X` flag

### Requirement: A split is proposed and confirmed, never applied on the model's word
A split creates a claim row and changes a claimable amount, so it SHALL follow the existing confirm-before-commit harness: the agent records a *proposal*, the Telegram layer renders one Confirm button, and the write happens only on the tap.

#### Scenario: Split proposed but not confirmed
- **WHEN** a split proposal is shown and the Confirm button is not tapped
- **THEN** no claim is created, no pet is assigned and no claimable amount changes

#### Scenario: Split confirmed
- **WHEN** Justin taps Confirm
- **THEN** the split is applied through the shared `claim_forms` path — the same code the dashboard would use — and the reply names every resulting claim id, pet and share

### Requirement: A submitted claim cannot be split
Splitting a claim already with the insurer would silently contradict what Petcover was sent. The system SHALL refuse to split a claim whose status is `sent` or any later lifecycle status, and SHALL say that the correction has to go to the insurer.

#### Scenario: Split attempted on a sent claim
- **WHEN** a split is requested for a claim that has been marked sent, acknowledged, settled or declined
- **THEN** the request is refused with a message saying the claim is already with the insurer, and nothing changes

#### Scenario: Split of a drafted claim replaces the draft
- **WHEN** a split is confirmed for a claim that is `drafted` but not yet sent
- **THEN** the existing Gmail draft is superseded — the claim returns to the pre-draft state and is re-drafted with its share, so no draft can be sent stating the pre-split amount

### Requirement: A pet whose insurer has no defined process still gets its share recorded
The second pet may be insured elsewhere (live: Echo is with Bow Wow Insurance, `claim_process_defined = 0`). Its share SHALL still become a claim, which then falls into the existing blocked pool with the existing "claim process not yet defined" flag. The system MUST NOT drop the share for lack of a claim process — that is exactly the silent loss this change exists to stop.

#### Scenario: One pet is insurable, one is not
- **WHEN** a split assigns a share to a pet whose insurer has no defined claim process
- **THEN** a claim is created for that pet with its share, no form is filled and no draft is created, and it appears among the blocked items with the reason naming the insurer

#### Scenario: Blocked share is visible, not actionable
- **WHEN** the outstanding-actions view is built after such a split
- **THEN** the blocked claim is listed with its pet, share and reason, and is marked as one no button can clear
