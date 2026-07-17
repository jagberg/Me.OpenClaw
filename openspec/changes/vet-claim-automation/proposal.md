## Why

Vet visits generate a card payment, then an invoice email arrives separately, then Justin manually fills a pet insurance claim form and emails it back. All three steps are manual and easy to forget. Automating detect → match → fill → send removes that chore.

## What Changes

- Add a bank-transaction feed: **manual CSV export from NetBank**, uploaded by Justin into OpenClaw. Commbank's Transaction Notifications turned out app/SMS-only (no email option), ruling out the email-parsing plan. PocketSmith (free-plan bank feeds gated behind paid feed credits in practice) and direct CDR aggregators — Basiq, Fiskil, illion (none had a free live-data tier) — were evaluated and rejected. Direct bank scraping was ruled out from the start as against Commbank's ToS.
- Filter incoming transactions for vet-like merchants (merchant name/category heuristics + Gemini for ambiguous cases).
- On a vet-transaction match, search Gmail (existing read-only integration) for a corresponding invoice email by vendor/amount/date proximity.
- **BREAKING**: extend Gmail OAuth scope from read-only to include send (`gmail.send`), required to email the filled claim form. This widens what OpenClaw can do on Justin's behalf and needs fresh consent.
- Fill a pet-insurance claim template (PDF/DOCX, Justin-supplied) with transaction + invoice details.
- Draft the claim email to the insurer for Justin to review/send from the dashboard before anything goes out automatically — no fully-autonomous send in v1, given money and an external party are involved.
- Notion integration for broader personal-life management: explicitly **out of scope** for this change — noted as a possible future change, not designed here.

## Capabilities

### New Capabilities
- `bank-transaction-feed`: connect to a CDR-accredited aggregator, poll/receive Commbank credit card transactions, store them locally.
- `vet-payment-detection`: classify stored transactions as vet-related or not.
- `invoice-matching`: given a vet transaction, find the corresponding invoice email and extract its details (vendor, amount, itemized services if present).
- `claim-form-automation`: populate the pet-insurance claim template from matched transaction+invoice data and produce a reviewable draft claim email.

### Modified Capabilities
- none — this reuses OpenClaw's existing Gmail plumbing as an implementation dependency, but no existing spec requirements change except the OAuth scope noted above, which belongs to a capability (`email-ingestion`) not yet promoted to `openspec/specs/`. Treated as a new dependency here, not a delta.

## Impact

- **No new external account or dependency needed**: transactions come from a NetBank CSV export Justin uploads himself, not a live feed.
- **Gmail OAuth scope widened**: read-only → read + send. Re-run of the consent flow required.
- **New DB tables**: `bank_transactions`, `vet_claims` (or similar) alongside the existing OpenClaw SQLite schema.
- **New credential/config surface**: aggregator API key/secret, claim template file path, insurer claim-submission email address.
- Requires Justin to supply the actual pet-insurance claim template file before this can be implemented for real.
