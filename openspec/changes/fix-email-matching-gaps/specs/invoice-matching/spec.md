# invoice-matching — delta

## ADDED Requirements

### Requirement: Extraction uses the provider-agnostic LLM seam
Invoice extraction SHALL call `llm.extract` (ADR-0009 seam), never a provider SDK directly, so provider/quota problems are solved by the `LLM_PROVIDER` env swap without code changes.

#### Scenario: Provider swap
- **WHEN** `LLM_PROVIDER` is changed and the app restarted
- **THEN** invoice extraction uses the new provider with no code change

### Requirement: Multi-invoice emails match per contained invoice
An email MAY contain several invoices (confirmed live: a vet's bulk reply to a yearly invoice request listed three invoices totalling $1,134.82). Extraction SHALL return every invoice found in the email; the matcher SHALL test each invoice individually against the ceiling and invoice-date gates, and SHALL match a claim to the passing invoice — never to the email's grand total.

#### Scenario: Bulk vet reply covering several visits
- **WHEN** a claim for $407.56 is matched against an email containing invoices for $141.87, $585.39 and $407.56
- **THEN** the claim matches the $407.56 invoice; the email remains available to match other claims' amounts

#### Scenario: No contained invoice fits
- **WHEN** every invoice in the email exceeds the claim's bank charge or fails the invoice-date gate
- **THEN** the claim does not match that email

#### Scenario: Extraction reply truncated by the model's output budget
- **WHEN** a long bulk email's extraction reply is cut mid-array (confirmed live on a 12k-char invoice PDF)
- **THEN** the complete invoice objects are salvaged and the partial one is dropped

### Requirement: One invoice paid over several charges merges into one claim on Justin's confirm — never a pick, never guessed
One vet invoice can be paid in several card swipes (confirmed live: MediPaws invoice #411193, $2,521.46 for Aari, whose own payment section lists the two payments −$1,970.40 and −$551.06 = the two bank charges). Which claim row carries the invoice is internal bookkeeping — Petcover sees the invoice, never the bank charges — so the system SHALL NOT ask Justin to choose between claims. When this claim plus exactly one other pending claim at the same vet sum to the invoice's total (ceiling tolerance), the system SHALL record a merge proposal and push a Telegram message showing the invoice, both charges and their sum — stating additionally when the invoice's own payment records list both charge amounts — with two actions: ✅ Merge (the larger charge's claim carries the invoice, ceiling validated against the charges combined; the other claim closes as `absorbed`/"second payment") and ❌ Not the same invoice (proposal rejected, both claims flagged for manual matching, the pair never re-proposed). Nothing merges without the confirm tap. When no sibling explains the total, the claim SHALL be flagged for manual review. (A dashboard view of open proposals is deferred.)

#### Scenario: One invoice, two charges — confirm merge
- **WHEN** a date-plausible invoice equals this claim's charge plus one sibling claim's charge and Justin taps Merge
- **THEN** the larger charge's claim is matched with the full invoice, the other becomes `absorbed` with a flag naming the carrier, and the proposal resolves

#### Scenario: Justin rejects the merge
- **WHEN** Justin taps "Not the same invoice"
- **THEN** the proposal is rejected, both claims are flagged for manual matching, and the pair is never proposed again

#### Scenario: Proposal notified exactly once
- **WHEN** a merge proposal is created
- **THEN** the message is pushed once (not re-sent every tick) and remains actionable until resolved or rejected

#### Scenario: No sibling explains the total
- **WHEN** the only date-plausible invoice exceeds the charge and no pending sibling claim completes the sum
- **THEN** the claim is flagged for manual review and no match is recorded

### Requirement: Candidate eligibility is governed by the invoice's own date, not email arrival
Forwarded invoices arrive long after the visit (confirmed live: February/January invoices forwarded in July). The Gmail search SHALL always include a query with no upper arrival bound (from the transaction date onward) for both the merchant and spouse-forward queries; the invoice-date plausibility gate (invoice date within the match window of the transaction date) SHALL remain the eligibility test.

#### Scenario: Months-late spouse forward
- **WHEN** an invoice dated 23/02/2026 matching a 23/02/2026 transaction is forwarded by the spouse in July
- **THEN** the claim matches it, regardless of `invoice_request_sent_at`

#### Scenario: Wide window catches an unrelated old invoice
- **WHEN** the wide query returns a real invoice whose own date is outside the match window of the transaction date
- **THEN** it does not match

### Requirement: The owner's own outgoing mail is never an invoice candidate
Justin's outgoing invoice-request emails list visit dates and charge amounts — extraction reads them as invoices with exact amount+date fits (confirmed live: 12 claims matched his own requests the moment the wide arrival window surfaced them). Candidate searches SHALL exclude self-sent mail (`-from:me`), and any message carrying Gmail's SENT label SHALL be skipped as a second layer.

#### Scenario: Own invoice-request email in the search window
- **WHEN** the merchant query returns Justin's own "Invoice request" email whose body lists the visit's exact date and amount
- **THEN** it is never matched — the claim keeps searching for the vet's actual invoice

### Requirement: Each email is extracted at most once
Extraction results SHALL be cached per Gmail message id and reused across claims and pipeline ticks; a candidate email SHALL cost at most one LLM extraction ever. A failed extraction SHALL NOT be cached (retried next tick).

#### Scenario: Rejected candidate reappears next tick
- **WHEN** a candidate email was extracted and rejected by the gates on a previous tick
- **THEN** the next tick re-evaluates it from cache with no LLM call

### Requirement: Unreadable invoice attachments are flagged, not skipped silently
When a candidate email from the claim's vet has a PDF attachment but yields no extractable amount (confirmed live: pypdf returned no text for a real invoice PDF), the claim SHALL be flagged `invoice attachment unreadable — <subject>` so Justin can request a readable copy. The flag SHALL clear when the claim matches.

#### Scenario: Vet reply with unparseable PDF
- **WHEN** the vet's reply carries an invoice PDF whose text extraction returns nothing
- **THEN** the claim is flagged as unreadable-attachment and remains `pending_match`

### Requirement: Spouse-forward vet confirmation resists generic word overlap
A spouse-forwarded candidate SHALL be accepted only when the known vet email address appears in the forwarded content, or a distinctive merchant name word (length ≥ 5, excluding generic tokens such as `veterinary`/`animal`/`hospital`) appears. Confirmed live: a human-hospital forward passed the previous check on a short generic word and burned extraction quota.

#### Scenario: Human-medical forward
- **WHEN** the spouse forwards a non-vet medical email that shares only a short/generic word with the merchant descriptor
- **THEN** it is not treated as a vet-invoice candidate and no extraction is spent on it
