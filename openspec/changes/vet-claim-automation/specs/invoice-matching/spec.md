## ADDED Requirements

### Requirement: Search Gmail for an invoice matching a vet transaction
The system SHALL search the connected Gmail account for messages from/about the transaction's merchant within a ±3 day window of the transaction date, when a transaction is flagged vet-related.

#### Scenario: Matching invoice email found
- **WHEN** a Gmail message in the date window mentions the merchant and an amount within tolerance of the transaction amount
- **THEN** the transaction is linked to that email and Gemini extracts structured invoice fields (date, amount, itemized services) from it

#### Scenario: No matching email found
- **WHEN** no Gmail message in the window matches the merchant
- **THEN** the transaction is marked `pending_match` and surfaced on the dashboard as needing manual follow-up, not silently dropped

#### Scenario: Email found but amount mismatch
- **WHEN** a candidate email matches the merchant but its amount differs from the transaction amount by more than the tolerance
- **THEN** the transaction stays `pending_match` rather than being auto-linked to a possibly-wrong invoice

### Requirement: Request the invoice from the vet by email when none is found, then keep rechecking
The system SHALL draft (never auto-send) an email to the vet requesting the invoice when a vet-flagged transaction has been `pending_match` past the normal ±3 day window and Justin confirms sending the request, and SHALL keep checking for a matching reply/new email from that vet from the original transaction date through to each subsequent check — not just the original fixed window — until a match is found.

#### Scenario: Pending-match transaction ages past the normal window
- **WHEN** a `pending_match` transaction has no matching email after the normal match window elapses
- **THEN** the system drafts a Gmail message to the vet asking for the invoice, and surfaces it on the dashboard for Justin to review and send — never sent automatically

#### Scenario: Rolling recheck after the request is sent
- **WHEN** a vet-invoice-request has been sent (Justin sent the drafted email) and the transaction is still unmatched
- **THEN** each subsequent invoice-matching pass searches from the original transaction date through to the current check time (not a fixed ±3 day window), so a late reply from the vet still gets picked up

#### Scenario: Vet replies with the invoice
- **WHEN** a new email arrives from the vet's address after a request was sent
- **THEN** it's treated as a normal invoice-matching candidate (merchant + amount-tolerance check applies the same as any other match)
