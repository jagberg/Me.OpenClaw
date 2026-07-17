## Why

Once a claim is drafted and sent, Petcover's replies (acknowledgement, requests for more info, suspensions, settlements, declines) arrive as separate, loosely-threaded emails with no consistent subject format. Surveyed 201 real emails from `petcovergroup.com` going back to 2024 — Justin currently has no way to see, at a glance, which claims are outstanding, which need him to act (info request), and which have settled or been declined. This makes claims easy to lose track of, especially requests for information that have a response deadline.

## What Changes

- Ingest Petcover reply emails (`claims.au@petcovergroup.com`, `requiredinfo.au@petcovergroup.com`, `accounts.au@petcovergroup.com`) on the existing polling cycle, classify each into a status event, and correlate it to the right `vet_claims` row via Petcover's own claim reference number (e.g. `DC1-27-5628`, `GABR-0306`) — distinct from the policy number and from our internal `vet_claims.id`.
- Track a claim's full lifecycle, not just current state: `drafted` (already exists) → `sent` → `acknowledged` → `info_requested` / `suspended` → `settled` (with paid amount) or `declined` (with reason).
- Persist a status history (append-only event log per claim) so a claim that gets suspended, responds, then settles keeps every step, not just the latest.
- Surface "needs your action" claims (open info requests, suspensions) and settlement reconciliation (claimed amount vs. paid amount) on the dashboard.
- Marketing/newsletter emails from `marketing.au@petcovergroup.com` are explicitly out of scope — filtered out, not tracked.

## Capabilities

### New Capabilities
- `claim-status-tracking`: ingests Petcover reply emails, classifies them into claim lifecycle events, correlates them to the originating `vet_claims` row via Petcover's claim reference number, and persists an append-only status history per claim.

### Modified Capabilities
- `claim-form-automation`: claims advance past `drafted` — needs a `petcover_reference` field captured once Justin sends the draft and Petcover's acknowledgement reply includes their claim number, plus new terminal/interim statuses (`sent`, `acknowledged`, `info_requested`, `suspended`, `settled`, `declined`) beyond today's `matched`/`drafted`.
- `invoice-matching`: the bank charge becomes a ceiling rather than an ≈-equality target — an invoice matches when its total ≤ the charge; extraction returns line items; the claim carries the claimable subtotal (routine-care items excluded), with unexplained remainders flagged (added 2026-07-18 at Justin's direction).

## Impact

- `app/openclaw/db.py`: new `claim_status_events` table; `vet_claims` gains `petcover_reference` and an expanded `status` enum.
- `app/openclaw/pipeline.py`: new polling step alongside existing invoice-matching pass.
- New module (`claim_status.py` or similar): email classification + correlation logic.
- Dashboard (`templates/index.html` / `main.py`): surface open action items and settlement reconciliation.
- No changes to Gmail send/draft behavior — this is read-only ingestion of Petcover's replies, still never auto-sends anything.
