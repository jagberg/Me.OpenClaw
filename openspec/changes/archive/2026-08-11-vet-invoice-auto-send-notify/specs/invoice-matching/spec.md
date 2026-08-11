## MODIFIED Requirements

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
