# PRD: Vet Claim Automation

**Status**: Draft — interview in progress
**Owner**: Justin
**Last updated**: 2026-07-11
**Source**: derived from OpenSpec change `vet-claim-automation` (proposal/design/specs at `openspec/changes/vet-claim-automation/`)

> Every requirement below is tagged **[Confirmed]** (Justin answered directly), **[Assumed — unconfirmed]** (recommended default used because Justin was away mid-interview; needs a yes/no), or **[Blocked]** (waiting on something from Justin). Nothing here should be read as decided just because it's written down.

## Overview

Automate the vet-visit-to-insurance-claim chore: detect a vet card payment, find its invoice email, fill the pet insurance claim form, and prep the claim email — so the only manual step left is hitting send.

## Problem

Three separate manual steps today: notice the card payment, dig up the invoice email, fill and email the claim form. Easy to forget any one of them, especially the invoice-hunting step which has no reminder trigger at all.

*[Assumed — unconfirmed]: this problem statement itself hasn't been directly validated with Justin yet — it's inferred from the original request.*

## Goals

- Detect vet-related Commbank credit card transactions automatically.
- Match a detected transaction to its invoice email.
- Auto-fill the real pet-insurance claim template from that data.
- Produce a ready-to-send claim email for human review.

## Non-Goals

- **[Confirmed]** Just vet claims for now — not the first slice of a broader "watch my spending" system.
- **[Confirmed]** Fully autonomous send — Justin wants a review step, though the review *channel* is still open (see below).
- **[Confirmed]** Notion integration — separate initiative, not designed here.
- Multi-bank support — Commbank only (not directly asked, follows from scope being Commbank-specific).

## Users / Stakeholders

- Justin — sole user, sole reviewer/approver of every generated claim.

## Requirements

### Bank transaction feed
- **CORRECTION**: Commbank's Transaction Notifications only deliver via app push or SMS — no email option. The earlier "parse Commbank alert emails via Gmail" plan doesn't work as designed and is retracted.
- **[Assumed — unconfirmed]** Source: **manual CSV export from NetBank**, uploaded by Justin into OpenClaw periodically. Chosen as the default because it's the only option confirmed genuinely free with zero new dependencies (no phone automation, no third-party account, no paid aggregator). Every other option checked either gated the real feed behind payment (PocketSmith, Basiq, Fiskil, illion, YNAB) or requires a delivery channel Commbank doesn't support by email (SMS-only alerts). Needs Justin's yes/no — asked, no response yet.
- **[Confirmed]** No genuinely free personal/self-serve structured API exists for this — checked PocketSmith, Basiq, Fiskil, illion, YNAB, and Frollo. Frollo's developer portal is confirmed business/partner-only (Contact Us onboarding, Client ID/Secret issued after that, OAuth 2.0 flows) — not something an individual with a personal Frollo account can self-register for. Frollo's in-app CSV export (emailed to verified address) is real and free, but manual-trigger only, confirmed by Justin directly using it.
- **[Assumed — unconfirmed]** Default given no clean automatic-and-free option exists: **reminder nudge**. OpenClaw pings Justin at 07:30/15:00/20:00 (his requested schedule) to tap Frollo's export button himself; the emailed CSV then flows through the existing Gmail poller. Zero new credential/security surface, reuses the reminder capability already built. Not hands-off — that's the explicit trade-off for zero new risk. Needs Justin's yes/no.
- **[Confirmed]** Justin is building his own Playwright script to produce the transaction export as a deterministic process, independent of this design. OpenClaw's responsibility is decoupled and unchanged either way: accept whatever CSV lands via the dashboard upload endpoint (spec'd in `bank-transaction-feed`), parse it, dedupe. Whether that file arrives from a manual tap, Justin's own script, or something else entirely is his call and outside this spec's concern.
- **[Confirmed]** Must stay on a free tier.
- No bank credentials or third-party account of any kind under the manual-CSV default. Store transaction metadata locally (date, amount, merchant); source is a user-uploaded file, not an API/email.

### Vet payment detection
- Classify each transaction vet-related or not via a cheap keyword/allowlist heuristic first, LLM fallback only for ambiguous cases.
- **[Assumed — unconfirmed]** Who maintains the vet-merchant allowlist: recommended default is Justin manually seeds/edits it (simplest, no guessing needed for his specific vet). Needs a yes/no.

### Invoice matching
- **[Assumed — unconfirmed]** Match window: ±3 days around the transaction date. Needs confirmation this matches how quickly his vet actually sends invoices.
- Extract structured invoice fields (date, amount, itemized services) from the matched email via LLM.
- If amount doesn't match within tolerance, or nothing is found, leave the transaction `pending_match`.
- **[Assumed — unconfirmed]** How Justin gets nudged about a `pending_match` transaction: recommended default is dashboard-only, no active reminder. Needs a yes/no — alternative is wiring it into OpenClaw's existing reminder/task system so it actually surfaces at a specific time.

### Claim form automation
- **[Confirmed]** Insurer is **Petcover** (Petcover Aust Pty Ltd), confirmed directly from the real claim form. Claim submission is by email with the filled form attached, to **claims.au@petcovergroup.com** — both previously open questions, now resolved from the source document itself, not inferred.
- **[Confirmed]** Claim template is a real fillable PDF (AcroForm), 2 pages, 38 form fields — inspected directly (`pypdf`, field names + positions extracted and mapped to the visible labels: policy no., name, contact, email, address/postcode/state, pet name/DOB, other-insurer Y/N, continuation-claim Y/N, a 4-row condition/date/date/charge table, payment method (bank account vs "pay my vet" direct), bank account name/BSB/number, declaration checkbox + date). Fill-library decision is settled: **pypdf** form-fill, not docxtpl — no Word-template path needed at all, that branch of the earlier design decision is dropped.
- **[Assumed — unconfirmed, real constraint found]** The "Condition being claimed for" field can't be reliably auto-filled from the vet invoice alone — checked a real invoice example and it lists line items (procedures, medication, pathology) and totals, but no diagnosis/condition text. Petcover's own claim checklist asks for clinical notes/history separately from the invoice for this reason. Realistic default: Gemini drafts a best-effort summary from invoice line items, but this field likely needs Justin's manual check/edit before every submission — not a clean auto-fill scenario like the dollar amounts are.
- **[Confirmed]** Review/send gate: draft-only for v1, human reviews and sends manually. Dashboard is the review surface for now.
- **[Confirmed]** Justin's real end-state preference is reviewing/approving via **Telegram or WhatsApp chat** ("is it ok to send?") rather than a dashboard. Explicitly deferred out of this change to keep scope tight (consistent with the existing dashboard-only decision, ADR-0003) — but recorded here as the named next follow-up change, not a maybe. If built, Telegram is the easier starting point (free bot API, no business verification) vs WhatsApp Business API (Meta business verification + per-message cost).
- If a required field is missing from extracted data, stop short of drafting an incomplete claim and flag for manual completion — the "Condition" field above is the expected common case for this, not an edge case.

## Success Metrics

*[Assumed — unconfirmed]: none of these have been checked with Justin directly yet — drafted from the stated goals, need his sign-off or correction.*

- Time from vet payment to claim email drafted: hours, not "whenever Justin remembers."
- Zero incorrect auto-matches sent without review (draft-only gate holds).
- No claim silently lost — every transaction ends in `matched`/`drafted`/`submitted_by_user`, or visibly `pending_match`.

## Key Decisions

| Decision | Status | Why |
|---|---|---|
| Frollo (free CDR app) + manual export, nudged by an OpenClaw reminder 3x/day | Assumed — unconfirmed | Every option checked for a free *automatic* feed failed (PocketSmith/Basiq/Fiskil/illion/YNAB gated behind payment or business partnership; Commbank's own alerts are app/SMS only, no email; Frollo's API is business-only). Reminder-nudged manual export is the only option with zero new credential/security surface. |
| Free tier only | Confirmed | Personal project, not a subscription for one card. |
| Two-stage vet detection (heuristic → LLM fallback) | Carried over, not challenged | Avoids burning a rate-limited free-tier LLM quota on obvious cases. |
| Draft-only claim email, never auto-send in v1 | Confirmed | Money + an external party — human gate stays; review *channel* (dashboard vs chat) still to be built out separately. |
| Dashboard-only review surface for v1, chat review as a named follow-up | Confirmed | Keeps this change scoped; chat-based review is real intent, just sequenced later. |
| Amount-tolerance matching, no forced match | Carried over, not challenged | Wrong invoice attached to a claim is worse than a claim sitting unmatched a bit longer. |

## Open Questions / Blocked Items

- Reminder-nudge vs other bank-feed automation trade-off — needs Justin's yes/no (Justin's own Playwright export script resolves *how* the CSV is produced either way, doesn't resolve whether OpenClaw should also nudge him).
- Match window (±3 days ok?), pending-match nudge behavior, vet-allowlist ownership — all unconfirmed, asked but no answer yet (see Requirements above).
- How should Justin want to handle the "Condition being claimed for" field given it can't be reliably auto-extracted from the invoice alone — always manual, or attempt a Gemini draft from line items with mandatory review?
- ~~Which insurer / claim template format / claim submission address~~ — resolved: Petcover, fillable PDF, claims.au@petcovergroup.com.

## Risks

- **Manual CSV means no automatic detection** → the core "notice the payment without being told" goal is only partially met; Justin still has to remember to export/upload. If this proves too manual in practice, revisit SMS-forwarding automation or a paid aggregator.
- **False-positive vet detection** → mitigated by draft-only + dashboard side-by-side review before send.
- **Wider Gmail scope (read + send/draft)** increases what a compromised local process could do → mitigated by enforcing draft-only at the application code level regardless of granted scope.

## Rough Milestones

1. Prerequisites — confirm manual-CSV workflow acceptable (or pick an alternative), claim template supplied, Gmail re-consent with widened scope.
2. Data layer — new tables for transactions and claims.
3. Bank transaction feed — polling + storage.
4. Vet payment detection — heuristic + LLM fallback.
5. Invoice matching — Gmail search + extraction.
6. Claim form automation — template fill + Gmail draft.
7. Dashboard — review surfaces for pending/matched/drafted claims.
8. Verification — smoke tests, then live tests against real data.
9. *(Named follow-up, not this change)* Chat-based review/approval via Telegram or WhatsApp.

Full technical detail (alternatives considered, schema, task breakdown) lives in the OpenSpec change: `openspec/changes/vet-claim-automation/`.

Sources for aggregator research: [Current providers | Consumer Data Right](https://www.cdr.gov.au/find-a-provider), [Fiskil](https://www.fiskil.com/), [Adatree — CDR intermediary](https://adatree.com.au/), [15 Best Basiq Alternatives (2026)](https://www.openbankingtracker.com/api-aggregators/basiq-io/alternatives).
