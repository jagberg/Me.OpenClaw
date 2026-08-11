# invoice-matching Specification

## Purpose
Find the vet's invoice for a bank charge, and decide what of it is claimable. `invoice_matching.py`.

Almost every requirement here exists because a plausible assumption was disproved by real mail. The "confirmed live" notes are load-bearing — they are why each rule is shaped the way it is.

See ADR-0007 (charge as ceiling), ADR-0009 (LLM seam), ADR-0010 (vision-OCR fallback).

*(Consolidated 2026-07-25 from the `vet-claim-automation` and `fix-email-matching-gaps` deltas. This file previously carried a note saying base requirements were "pending sync" — they never were, so the baseline described one requirement out of the capability's real behaviour.)*
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
- **THEN** the claim still matches but is flagged `possible additional invoice — unexplained $X` for manual follow-up

#### Scenario: Invoice contains routine-care line items
- **WHEN** a matched invoice mixes claimable treatment with routine/preventive items (e.g. a consultation plus a vaccination)
- **THEN** the claim form's charge is the claimable subtotal only, with the routine items excluded

#### Scenario: Invoice is routine care only
- **WHEN** a matched invoice's claimable subtotal is zero
- **THEN** no claim document is drafted; the claim is flagged `routine care only — not claimable`

#### Scenario: Extraction returns no itemization
- **WHEN** the invoice's line items can't be read
- **THEN** the invoice total is used as the claimable amount — a whole invoice is never silently dropped for lacking itemization

### Requirement: Search Gmail for the invoice, bounded by the invoice's own date rather than arrival
When a transaction is flagged vet-related, the system SHALL search Gmail for the merchant, including a query with **no upper arrival bound** (from the transaction date onward), for both the merchant and spouse-forward queries. Eligibility SHALL be governed by the **invoice's own date** falling within the match window of the transaction date — not by when the email arrived.

**This supersedes the original ±3-day arrival window.** Forwarded invoices arrive long after the visit (confirmed live: February and January invoices forwarded in July), so an arrival-based window silently loses real invoices.

#### Scenario: Months-late spouse forward
- **WHEN** an invoice dated 23/02/2026 matching a 23/02/2026 transaction is forwarded in July
- **THEN** the claim matches it, regardless of `invoice_request_sent_at`

#### Scenario: Wide window catches an unrelated old invoice
- **WHEN** the wide query returns a real invoice whose own date is outside the match window of the transaction date
- **THEN** it does not match

#### Scenario: No matching email found
- **WHEN** no message matches the merchant
- **THEN** the claim stays `pending_match` and is surfaced as needing follow-up, not silently dropped

### Requirement: The owner's own outgoing mail is never an invoice candidate
Candidate searches SHALL exclude self-sent mail (`-from:me`), and any message carrying Gmail's SENT label SHALL be skipped as a second layer.

Justin's own invoice-request emails list visit dates and charge amounts, so extraction reads them as invoices with *exact* amount+date fits — the most convincing possible false positive. Confirmed live: 12 claims matched his own requests the moment the wide arrival window surfaced them.

#### Scenario: Own invoice-request email in the search window
- **WHEN** the merchant query returns Justin's own "Invoice request" email listing the visit's exact date and amount
- **THEN** it is never matched — the claim keeps searching for the vet's actual invoice

### Requirement: Extraction uses the provider-agnostic LLM seam
Invoice extraction SHALL call `llm.extract` (ADR-0009), never a provider SDK directly, so provider and quota problems are solved by configuration rather than code changes.

**Supersedes** the original "Gemini extracts structured invoice fields", which named the only backend that existed at the time.

#### Scenario: Provider swap
- **WHEN** `LLM_PROVIDER` is changed and the app restarted
- **THEN** invoice extraction uses the new provider with no code change

### Requirement: Each email is extracted at most once
Extraction results SHALL be cached per Gmail message id and reused across claims and ticks; a candidate email SHALL cost at most one LLM extraction ever. A **failed** extraction SHALL NOT be cached, so it retries.

The cache is permanent — invalidate the row if what extraction must return ever changes.

#### Scenario: Rejected candidate reappears next tick
- **WHEN** a candidate email was extracted and rejected by the gates on a previous tick
- **THEN** the next tick re-evaluates it from cache with no LLM call

### Requirement: Multi-invoice emails match per contained invoice
An email MAY contain several invoices (confirmed live: a vet's bulk reply to a yearly invoice request listed three invoices totalling $1,134.82). Extraction SHALL return every invoice found; the matcher SHALL test each individually against the ceiling and invoice-date gates and match the passing one — **never the email's grand total**.

#### Scenario: Bulk vet reply covering several visits
- **WHEN** a claim for $407.56 is matched against an email containing invoices for $141.87, $585.39 and $407.56
- **THEN** the claim matches the $407.56 invoice, and the email remains available to other claims

#### Scenario: No contained invoice fits
- **WHEN** every invoice in the email exceeds the charge or fails the invoice-date gate
- **THEN** the claim does not match that email

#### Scenario: Extraction reply truncated by the model's output budget
- **WHEN** a long bulk email's extraction reply is cut mid-array (confirmed live on a 12k-char invoice PDF)
- **THEN** the complete invoice objects are salvaged and the partial one dropped

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

### Requirement: One invoice paid over several charges merges on confirmation — never a pick, never guessed
One vet invoice can be paid in several card swipes (confirmed live: invoice #411193, $2,521.46, whose own payment section lists −$1,970.40 and −$551.06 = the two bank charges). Which claim row carries the invoice is internal bookkeeping — Petcover sees the invoice, never the bank charges — so the system SHALL NOT ask Justin to choose between claims.

When this claim plus exactly one other pending claim at the same vet sum to the invoice total (within ceiling tolerance), the system SHALL record a merge proposal and push one Telegram message showing the invoice, both charges and their sum — stating additionally when the invoice's own payment records list both amounts — offering Merge or Not-the-same-invoice. **Nothing merges without the tap.** Where no sibling explains the total, the claim SHALL be flagged for manual review.

#### Scenario: One invoice, two charges — confirm merge
- **WHEN** a date-plausible invoice equals this claim's charge plus one sibling's, and Justin taps Merge
- **THEN** the larger charge's claim carries the full invoice (ceiling validated against the charges combined), the other becomes `absorbed` with a flag naming the carrier, and the proposal resolves

#### Scenario: Justin rejects the merge
- **WHEN** Justin taps "Not the same invoice"
- **THEN** the proposal is rejected, both claims are flagged for manual matching, and the pair is never proposed again

#### Scenario: Proposal notified exactly once
- **WHEN** a merge proposal is created
- **THEN** the message is pushed once, not re-sent every tick, and stays actionable until resolved

#### Scenario: No sibling explains the total
- **WHEN** the only date-plausible invoice exceeds the charge and no pending sibling completes the sum
- **THEN** the claim is flagged for manual review and no match is recorded

### Requirement: Image-only scans fall back to vision OCR, hard-capped
When a candidate PDF has no text layer, the system SHALL fall back to vision OCR page-by-page, capped at 3 attempts per email. Attempts SHALL be refunded on provider outage — a provider being down is not evidence the scan is unreadable. Successful vision results are cached like any extraction. See ADR-0010.

#### Scenario: Scanned invoice with no text layer
- **WHEN** text extraction returns nothing for a PDF from the claim's vet
- **THEN** vision OCR reads it page-by-page and the result is cached

#### Scenario: Provider outage during a vision attempt
- **WHEN** the vision call fails because the provider is unavailable
- **THEN** the attempt is refunded rather than counted against the 3-attempt cap

### Requirement: Unreadable invoice attachments are flagged, not skipped silently
When a candidate email from the claim's vet has a PDF attachment but yields no extractable amount, the claim SHALL be flagged `invoice attachment unreadable — <subject>` so Justin can request a readable copy. The flag SHALL clear when the claim matches.

#### Scenario: Vet reply with unparseable PDF
- **WHEN** the vet's reply carries an invoice PDF whose extraction returns nothing
- **THEN** the claim is flagged unreadable-attachment and stays `pending_match`

### Requirement: Spouse-forward vet confirmation resists generic word overlap
A spouse-forwarded candidate SHALL be accepted only when the known vet email address appears in the forwarded content, or a distinctive merchant-name word (length ≥ 5, excluding generic tokens such as `veterinary`/`animal`/`hospital`) appears.

Confirmed live: a human-hospital forward passed the previous check on a short generic word and burned extraction quota.

#### Scenario: Human-medical forward
- **WHEN** the spouse forwards a non-vet medical email sharing only a short or generic word with the merchant descriptor
- **THEN** it is not treated as a candidate and no extraction is spent on it

### Requirement: Request the invoice from the vet when none is found, then keep rechecking
When a vet-flagged transaction has been `pending_match` past the match window, the system SHALL send an email to the vet requesting the invoice, using the `vet_contacts` override address where present. This is the one email type this capability is permitted to send directly rather than draft (CLAUDE.md hard-rule exception, scoped to this call site only, override confirmed by Justin 2026-08-11). Rechecking SHALL continue from the original transaction date onward on every later pass.

Where no vet email is on file the claim SHALL be flagged as such rather than silently skipped — that flag is actionable via the vet-email command. Where the send itself fails (network, auth, API error), the claim SHALL be flagged with the failure reason rather than left looking untouched — a failed send is never a silent no-op.

Claims already sitting at the legacy `invoice_request_drafted` flag (created before this requirement changed) are unaffected: they keep going through the pre-existing draft-and-manually-send flow until Justin sends or the flag changes some other way.

#### Scenario: Pending-match transaction ages past the window
- **WHEN** a `pending_match` transaction has no matching email after the match window elapses
- **THEN** the invoice-request email is sent directly to the vet, and the claim is flagged and timestamped as sent — no draft is created, nothing is left for Justin to review or send

#### Scenario: The send itself fails
- **WHEN** an invoice-request email is due and the Gmail API call to send it raises
- **THEN** the claim is flagged with the failure reason, not left silently unflagged, and no partial/successful state is recorded

#### Scenario: Vet replies with the invoice
- **WHEN** a new email arrives from the vet after a request was sent
- **THEN** it is treated as a normal candidate, with the same merchant and ceiling checks as any other

#### Scenario: No vet address on file
- **WHEN** an invoice request is due but the merchant has no contact address
- **THEN** the claim is flagged that no vet email is on file, rather than failing silently

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

### Requirement: An invoice's line items may carry their own date
An invoice's header date is not always the date of the treatment on it — a vet's statement can bill several visits, and a consultation on the 18th can appear on an invoice dated the 30th. Extraction SHALL capture an optional date per line item, null when the document does not state one, so a date named elsewhere (an insurer's request for "consultation notes dated 18/05/2026") can be matched against the treatment rather than only against the invoice header.

Because successful extractions are cached indefinitely, changing what extraction must return SHALL be accompanied by invalidating the cached rows, with the number of re-extractions stated up front — the re-read spends real tokens against the daily budget.

#### Scenario: Multi-visit invoice
- **WHEN** an invoice dated the 30th lists a consultation dated the 18th
- **THEN** that line item carries its own date, distinct from the invoice's date

#### Scenario: No item dates printed
- **WHEN** an invoice's line items state no dates
- **THEN** their dates are null and matching falls back to the invoice's own date

#### Scenario: Cache invalidated deliberately
- **WHEN** the extraction schema changes to include item dates
- **THEN** the cached extractions are cleared as one deliberate step with the count of re-extractions stated, not left to expire silently

### Requirement: A date named by the insurer resolves to a visit we already hold
When Petcover names a date in a request (the treatment whose notes it wants), the system SHALL find the visit that date belongs to and report it — claim id where one exists, plus merchant, invoice number and amount — searching claims' stored invoices first and the extraction cache second. It SHALL NOT fall back to the nearest date: an adjacent visit is a different consultation, and chasing the wrong one wastes the request.

The resolved invoice is context for the chase, never a substitute for the document requested. Consultation notes are clinical records held by the practice; nothing resolved here is attached or offered in their place.

#### Scenario: The date belongs to a claim we hold
- **WHEN** a request names a date matching a claim's invoice (real: 18/05/2026 → Kings Vet invoice 1000229, $351.50, claim #6)
- **THEN** the request reports that claim, merchant, invoice number and amount, even though the request itself sits on a different claim

#### Scenario: The date belongs to a visit with no claim
- **WHEN** the date matches only an invoice in the extraction cache (a visit predating the bank-transaction coverage)
- **THEN** the invoice's own details are reported with no claim id

#### Scenario: No match
- **WHEN** no held invoice carries the requested date
- **THEN** the request states the date and that the visit is unknown, and no nearest-date guess is made

#### Scenario: Two visits share the date
- **WHEN** two invoices carry the requested date (e.g. one charge covering two pets)
- **THEN** both are reported rather than one being chosen

