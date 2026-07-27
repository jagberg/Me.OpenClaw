## ADDED Requirements

### Requirement: One charge paying two invoices is apportioned into two claims
A single card charge can settle several invoices at once, most often one per pet, each its own document (confirmed live 2026-07-27: The Shire Vet's $407.56 charge on 2026-07-06 = SHV49c1622284e5 for Aari $35.00 + SHVd5b232905fdb for Echo $369.33, forwarded as two emails, the $3.23 balance being card surcharge). Where the matched invoice leaves an unexplained remainder and **exactly one** other candidate closes the charge within the surcharge margin, the system SHALL apportion: this claim keeps one invoice and a sibling claim on the same transaction carries the other, each with its own matched email, invoice, claimable subtotal and pet.

Both invoices MUST independently clear the ceiling, date-plausibility and already-claimed gates. Ambiguity SHALL be refused, not resolved: if more than one candidate would complete the sum, no apportionment happens. The pet on each claim comes from that invoice's printed patient field or a single pet named in its email — never inferred.

No confirmation tap is required, unlike the merge proposal: nothing is closed or overwritten, a claim is added, and a wrong one is reversible with the existing ❌ Wrong invoice button.

#### Scenario: Two invoices, one charge, one pet each
- **WHEN** a claim's charge matches one invoice and exactly one other candidate invoice brings the pair within the surcharge margin of the charge
- **THEN** both invoices end up claimed — this claim on one, a new claim on the same transaction on the other — each with its own pet, invoice number, itemization and claimable subtotal, and neither claim is flagged as unexplained

#### Scenario: Several candidates could complete the sum
- **WHEN** two or more candidate invoices would each close the charge
- **THEN** nothing is apportioned and the existing `possible additional invoice — unexplained $X` flag stands, because which invoice the charge paid is unknowable

#### Scenario: Nothing explains the remainder
- **WHEN** no candidate closes the gap between the matched invoice and the charge
- **THEN** behaviour is unchanged: the claim keeps its invoice and carries the unexplained-remainder flag

### Requirement: A receipt paid on the charge date is date-plausible whatever its service date
The service-date window (`INVOICE_MATCH_WINDOW_DAYS`, 3 days) SHALL NOT be the only route to date plausibility. An invoice SHALL also be plausible when a single line of its own document carries **both** the bank charge's date and that invoice's amount — the payment line a receipt prints.

Rationale: the two real invoices above bill visits on 19 Jun and 30 Jun and were charged on 6 Jul; each says `06/07/2026 Credit Card $35.00` / `$369.33`. Measured on the service date, both were rejected for the charge that paid them. The window is NOT widened — it exists because an open-ended one let one Shire Vet claim take another Shire Vet visit's invoice — and requiring both facts on one line stops a bulk email lending its payment dates to an unrelated invoice.

#### Scenario: Visit weeks before the payment
- **WHEN** an invoice's service date is outside the match window but its own text shows it paid on the charge's date for the charge's amount
- **THEN** it is eligible to match that charge

#### Scenario: Payment date belongs to another invoice
- **WHEN** the charge's date and the invoice's amount appear in the document but on different lines
- **THEN** the invoice is not made plausible by it

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
