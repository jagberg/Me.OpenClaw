# invoice-matching Specification

<!-- NOTE: Partial spec. Base requirements for this capability are still pending sync from the active `vet-claim-automation` change; only the requirement modified by `petcover-claim-status-tracking` is captured here. -->

## Purpose
TBD — full purpose lands when the base `vet-claim-automation` change syncs.

## Requirements

### Requirement: Match invoices against the bank charge as a ceiling, and claim only the claimable subtotal
The bank charge is the MAXIMUM possible claim — it can exceed the invoice total via card surcharge (confirmed live: $580.74 invoice charged as $585.39) or cover several invoices at once (confirmed live: one $177.50 charge = a $35 + a $142.50 invoice for different pets). The system SHALL accept a candidate invoice when its total is at most the charged amount (plus a 1-cent float-rounding tolerance) and SHALL reject invoices exceeding the charge. Invoice extraction SHALL return per-line-item amounts; the claimable amount — the sum of line items not matching the routine/preventive-care exclusion list (`NON_CLAIMABLE_KEYWORDS`: vaccination, desexing, worming, flea/tick prevention, etc.) — SHALL be stored on the claim and used as the claim form's charge, never the bank amount.

#### Scenario: Invoice below the charge by a card surcharge
- **WHEN** a candidate invoice's total is slightly below the bank charge (within ~2%)
- **THEN** it matches, with no additional flag

#### Scenario: Invoice above the charge
- **WHEN** a candidate invoice's total exceeds the bank charge
- **THEN** it does not match — you cannot have paid less than the invoice you're claiming

#### Scenario: Charge covers more than the matched invoice
- **WHEN** the matched invoice's total is below the charge by more than a plausible surcharge (>2%)
- **THEN** the claim still matches but is flagged `possible additional invoice — unexplained $X` for manual follow-up (a second invoice may exist for the same charge)

#### Scenario: Invoice contains routine-care line items
- **WHEN** a matched invoice mixes claimable treatment with routine/preventive items (e.g. a consultation plus a vaccination)
- **THEN** the claim form's charge is the claimable subtotal only, with the routine items excluded

#### Scenario: Invoice is routine care only
- **WHEN** a matched invoice's claimable subtotal is zero (every line item is routine/preventive)
- **THEN** no claim document is drafted; the claim is flagged `routine care only — not claimable`

#### Scenario: Extraction returns no itemization
- **WHEN** the invoice's line items can't be read (extraction returns no items)
- **THEN** the invoice total is used as the claimable amount — a whole invoice is never silently dropped for lacking itemization
