## MODIFIED Requirements

### Requirement: An invoice already carried by another claim is never re-matched
Invoice identity across claims SHALL be `invoice_number` where present, else amount + date. A claim SHALL NOT match an invoice another claim already carries, and where several candidates pass the gates the closest amount match SHALL win.

This rule governs **matching**, i.e. accidental duplication. It SHALL NOT be read as forbidding several claims from deliberately sharing one invoice: a confirmed per-pet split creates exactly that, one claim per pet each carrying the same invoice with its own claimable share. Split siblings are created already matched and never enter the pending pool, so they are outside this gate by construction — and a later charge that happens to match the same invoice is still correctly refused.

#### Scenario: Two claims, one invoice already assigned
- **WHEN** a candidate invoice is already carried by another claim
- **THEN** it is not matched again, and the claim keeps searching

#### Scenario: Deliberate per-pet split shares one invoice
- **WHEN** a confirmed split gives two pets' claims the same invoice number and total with different claimable shares
- **THEN** both claims keep the invoice, neither is flagged as a duplicate, and no re-match is attempted for either

#### Scenario: A different charge matches a split invoice
- **WHEN** an unrelated pending claim's search turns up an invoice already carried by split siblings
- **THEN** it is refused as already claimed, exactly as for any single-claim invoice
