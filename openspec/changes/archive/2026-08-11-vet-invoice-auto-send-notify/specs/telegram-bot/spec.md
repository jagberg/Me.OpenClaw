## ADDED Requirements

### Requirement: Notification when the invoice-request email is sent
When `invoice-matching`'s send-invoice-request path sends an email to the vet, the system SHALL push a single Telegram notification per claim/batch stating that the request was sent, using the same once-per-state dedup as other claim notifications. The message SHALL NOT carry a warning marker and SHALL NOT attach an action button — there is nothing for Justin to do, this is informational.

This is a deliberate narrowing of the existing "flagged `pending_match` claims notify" behavior: a claim whose invoice-request is merely *drafted* (the legacy manual-send path) remains excluded as noise, since nothing is actionable until Justin sends it himself; a claim whose invoice-request has actually been *sent* is a distinct, notify-worthy event.

#### Scenario: Invoice request sent after a CSV upload triggers the scan
- **WHEN** a CSV upload's claim scan causes a `pending_match` claim to age past the match window and its invoice-request email is sent
- **THEN** a Telegram message is pushed stating the request was sent, with no warning marker and no button

#### Scenario: A legacy drafted request stays silent
- **WHEN** a `pending_match` claim is flagged `invoice_request_drafted` (created before this requirement existed)
- **THEN** no notification is pushed for that flag — Justin still has to find and send the draft himself, as before
