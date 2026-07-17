## Context

Pulled 201 real emails from `petcovergroup.com` (2024–2026) to ground this design. Findings:

- **Claim reference format has changed over time.** Older emails (2024–early 2025) use refs like `GABR-0305`, `ELD-24-2146`, `ELD-25-2728`. Newer emails (2025–2026) use `DC1-26-4751`, `DC1-27-5628`. Both formats coexist in history and must both be handled — Petcover's own system evidently changed, not something we control.
- **The reference is first assigned in the acknowledgement reply**, not known to us at submission time. A real acknowledgement body reads: `"Claim Received — Claim Number ELD-24-2146"` alongside the pet name and policy number, in plain text (no attachment needed). This is the natural point to *learn* the reference and attach it to our `vet_claims` row.
- **Later replies (info requests, suspensions, settlements, declines) are inconsistent about repeating the reference.** Some subjects carry it directly (`"Petcover Claim DC1-27-5628 SR1 Request for information"`); some don't and only mention the pet's name and a treatment date in the body (`"...claim submitted for treatment provided to Ari... consult notes dated 09/01/2025"`).
- **Settlement notices (`"PetCover Letter - Claim Settlement EFT Template"`) and acknowledgement letters (`"PetCover - Acknowledgement Letter"`) are template-named identically across all claims** — the actual claim/amount/pet details are only in the body text or a PDF attachment, not the subject. Subject alone cannot disambiguate which claim a settlement belongs to.
- **Marketing mail (`marketing.au@petcovergroup.com`) and admin/billing mail (`accounts.au@petcovergroup.com` direct-debit issues) are unrelated to claim status** and must be filtered out rather than mis-classified.

## Goals / Non-Goals

**Goals:**
- Automatically learn and store Petcover's claim reference once assigned (from the acknowledgement reply).
- Classify every subsequent Petcover reply into one of: acknowledged, info_requested, suspended, settled, declined — or "unclassified" if none match (never silently drop an email we can't classify).
- Correlate a reply to the right `vet_claims` row via (in order of confidence): claim reference match → pet name + date proximity to the claim's transaction date, when reference is absent or not yet learned.
- Append every classified event to a history log — never overwrite, so the full back-and-forth is visible.
- Surface unresolved action items (open info requests/suspensions with no later resolving event) and settlement reconciliation (paid vs. claimed amount) on the dashboard.

**Non-Goals:**
- Auto-responding to info requests or suspensions — Justin still handles the actual reply himself; this only tracks state.
- Parsing PDF attachments (settlement/acknowledgement letters) in v1 — start with body-text extraction only; if amounts/refs turn out to live only in the attached PDF for a meaningful fraction of real emails, that's a fast follow using the PDF-text extraction already built for invoice matching (`invoice_matching._pdf_attachment_text`).
- Historical backfill of all 201 emails is a nice-to-have, not required for v1 — the pipeline only needs to work going forward from claims drafted after this ships. (Backfill is cheap to add later using the same classifier since it's just a bounded Gmail search over history.)

## Decisions

- **The claims service is a logical boundary, not a separate deployable** (confirmed with Justin 2026-07-18). The claim modules (`vet_detection`, `invoice_matching`, `claim_forms`, `claim_status`, plus the `pipeline` orchestrator) form the claims service inside the one FastAPI app; the assistant side (tasks/reminders/gmail_ingest) calls it only via `pipeline.run_once()` and the dashboard routes. One user + one SQLite file + a shared APScheduler — a second process would add IPC and DB contention for zero payoff. Revisit only if multi-user or independent deploys become real.
- **Bank charge = ceiling, invoice = what's claimable** (Justin, 2026-07-18). Matching accepts an invoice when its total ≤ the charge (+1c); the claim form carries the claimable subtotal (line items minus the routine-care exclusion list), never the bank amount. A gap beyond a plausible surcharge (>2%) flags "possible additional invoice" instead of blocking. See specs/invoice-matching/spec.md.

- **New `claim_status_events` table (append-only) rather than mutating `vet_claims.status` in place.** A single mutable status column can't represent "suspended, then later resolved" — an event log can, and the dashboard reads the *latest* event for current status while keeping full history for free.
- **Reference correlation is regex-based, not LLM-based.** Both known ref formats (`GABR-####`, `ELD-##-####`, `DC1-##-####`) are simple fixed patterns — using Gemini here would burn quota (already hit the 20/day free-tier cap once this session) for something regex handles reliably. Reserve Gemini only for the fallback path (classifying reply *type* when subject keywords don't match cleanly, and pet-name extraction when no reference is present).
- **Classification is subject-first, body-fallback.** Every subject sample maps cleanly to a type via keyword match (`"Acknowledgement Letter"` → acknowledged, `"suspended"` → suspended, `"Request for information"` / `"Request for Invoice"` / `"Request for consult note"` / `"Request for completed Claim Form"` → info_requested, `"Settlement EFT"` → settled, `"Declined"` → declined). Only fall back to body text when subject keywords don't match.
- **`vet_claims.status` enum extends rather than replaces** existing values (`pending_match`, `matched`, `drafted`) — adds `sent`, `acknowledged`, `info_requested`, `suspended`, `settled`, `declined`. `sent` is set when Justin actually sends the draft (detected the same way — polling for the claim leaving Drafts, or simplest: Justin marks it sent on the dashboard, since OpenClaw never auto-sends and has no other reliable signal of send-time).
- **Filter senders explicitly**: only `claims.au@`, `requiredinfo.au@`, `accounts.au@` (claims-relevant) are polled for status events; `marketing.au@` is excluded at the query level, not classified-then-discarded.
- **"Needs your action" stays open until Justin explicitly confirms it, not until a new email arrives.** An `info_requested`/`suspended` claim doesn't drop off the action list just because a later event (even `settled`) comes in — the new event is surfaced alongside the old one, and Justin clicks "confirm resolved" on the dashboard to close it out. Prevents a claim silently falling off the radar because Petcover's own follow-through was inconsistent (confirmed real pattern: some claims in the 201-email survey had 2-3 "request for X" emails in a row before resolving). "Alerted on a regular basis" is satisfied by ADR-0003 (dashboard-only, no push) — the item simply persists on every dashboard check until confirmed, rather than a new notification channel.

## Risks / Trade-offs

- **[Risk]** A reply with no claim reference AND an ambiguous pet name (e.g. two open claims for the same pet at once) can't be correlated confidently. → **Mitigation**: flag as `unclassified`/`needs manual link` on the dashboard rather than guessing wrong; Justin links it manually once.
- **[Risk]** Petcover may change their template/subject wording again (already happened once, 2024→2026). → **Mitigation**: classification keyword list lives in one place (`claim_status.py`), easy to extend; unmatched subjects fall into `unclassified` (visible, not silently lost) rather than crashing or mis-tagging.
- **[Risk]** Settlement amount may only be in a PDF attachment for some claims (not confirmed either way — see Non-Goals). → **Mitigation**: if body-text extraction returns no amount, flag `settled — amount unknown, check attachment manually` rather than guessing.

## Open Questions

- Should `sent` status require Justin to explicitly mark it on the dashboard, or can we detect "no longer in Drafts" via a periodic Gmail check? Simpler to start manual; revisit if it's annoying in practice.

## Dry-Run Findings (2026-07, Loki claim ELD-24-2146, full real lifecycle)

Traced one complete real lifecycle end to end: submitted (23 Jun 2025) → automatic acknowledgment-of-receipt (same day) → formal Acknowledgement Letter (24 Jun 2025, learns `ELD-24-2146`) → Settlement EFT (4 Jul 2025). Confirms the design works, with two corrections:

- **Settlement amount is PDF-only, not deferrable.** The body text/HTML for the settlement email cuts off right before the numbers ("...how this has been calculated are below:") — the actual breakdown (`Amount Claimed $624.89`, `Non-Claimable Amount $124.94`, `Age Contribution 35% = $174.98`, `Total Payable $324.97`) exists only in the attached PDF. Since the whole point of tracking `settled` is the paid-vs-claimed reconciliation, PDF text extraction (already built for invoice matching, `invoice_matching._pdf_attachment_text`) is now **required for v1**, not a fast-follow. Resolves the "Non-Goals" PDF deferral above for the settlement path specifically — acknowledgement/info-request/suspended emails still don't need it (confirmed those carry everything needed in plain body text).
- **"Automatic reply: ..." emails are noise, not a status event.** Sent instantly on submission from `claims.au@`, before the real Acknowledgement Letter (which follows 1-2 business days later per its own boilerplate). Classifying this as `unclassified` would wrongly flag it for manual review. Needs an explicit `ignore` bucket (subject starts with "Automatic reply:") — distinct from `unclassified`, which should mean "real reply we couldn't classify," not "noise we recognize and skip."
- **Reference-format regex confirmed working** against real text: `Claim Number\s+([A-Za-z0-9-]+)` cleanly extracted `ELD-24-2146` from the acknowledgement body.
- **Fallback correlation (pet + date) confirmed necessary and sufficient** for the very first event on a claim (acknowledgement), since no reference is known yet at that point — worked here because only one open Loki claim existed at the time.
- **Loki is a third real historical pet, not in the `pets` table** (only Aari/Petcover and Echo/Bow Wow are seeded). Confirmed with Justin: Loki passed away Oct 2024 — no ongoing claims, not adding to `pets`. Echo joined the family ~May 2025 (14 months before this change), consistent with Echo having no claim history before that in the survey.
