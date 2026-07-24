# Fix email→claim matching gaps

## Why

14 of 21 claims sit in `pending_match` while the matching invoices are verifiably sitting in Gmail right now (confirmed live 2026-07-21): vets replied to the yearly bulk invoice requests, and Gabi forwarded exact-match invoices (e.g. SAH $944.50 dated 23/02/2026 for claim #13). None matched, and Petcover status tracking has recorded zero events ever. Diagnosis against the real DB and Gmail found five compounding defects — one config/ops layer problem and four matcher logic gaps.

## Root causes (all confirmed live)

1. **`invoice_matching._extract_invoice` still calls `gemini.extract` directly** — the one caller ADR-0009's migration missed. Gemini free tier is ~20 requests/day; 310 invoice-extraction 429s logged since 17 Jul. Matching is dead for most of every day.
2. **Extraction failure kills the whole pipeline tick**: `match_claim` raises out of `run_once` at the first pending claim, so claim-form drafting, Petcover status polling, and Telegram notifications are starved too (`claim_status_events` is empty despite 3 sent claims).
3. **Bulk "past 12 months" vet replies can't match**: extraction returns one grand-total amount (Shire reply: $1,134.82 = three invoices, one of which is claim #1's exact $407.56) → ceiling rejects it for every claim. Kingsgrove's reply additionally yields no text at all — its PDF attachment fails pypdf extraction silently (212 chars, no amounts).
4. **Late-arriving forwards are never searched**: Gmail queries window on email *arrival* date (txn ±3 days) unless `invoice_request_sent_at` is set, which only reconciliation of that claim's own per-claim draft can set. Gabi's July forwards of January/February invoices (claims #3, #13, #15 — exact amounts and dates) are simply never fetched.
5. **Same candidates re-extracted every 15-min tick, forever** — no per-email extraction cache and no negative-match memory, so even a healthy provider's quota burns on emails already rejected (or that can never match, e.g. a human-hospital forward passing the weak word-overlap vet check).

Operational (not code, but recorded here because it reproduces the symptom): a stray host process from the `Me.OpenClaw-dashboard` worktree has been running since 20 Jul with a pre-edit `.env` (Gemini provider), polling the same Gmail every 5 min and burning the shared 20/day Gemini quota (655 follow-up 429s). It must be stopped; the container alone should run.

## What Changes

- Route invoice extraction through `llm.extract` (provider-agnostic, Groq default — verified working live today).
- Isolate per-claim match failures: one claim's extraction error is flagged and logged, the tick continues; an `LLMUnavailableError` skips remaining *matching* (quota is global) but never blocks claim forms, Petcover polling, or notifications.
- Extraction returns a **list of invoices** per email; the matcher tests each invoice against the ceiling + invoice-date gates, so one bulk reply can satisfy several claims.
- Surface unreadable invoice attachments: when an email that passed the query gates yields no extractable text/amount from its PDF, flag the claim (`invoice attachment unreadable — …`) instead of silently skipping.
- Match on the invoice's own date, not the email's arrival date: late forwards become candidates regardless of when they arrive (arrival window stays only as a query optimization, with an always-on wide fallback for the spouse/vet queries).
- Cache extraction results per email id and remember per-claim rejected candidates, so no email is re-extracted every tick.
- Tighten the spouse-forward vet confirmation (known vet email, or vet-name word match that can't fire on generic words) — stops burning extraction on human-medical forwards.

**BREAKING**: none — single-user app; `invoice_data` JSON shape gains a list-of-invoices extraction step but stored matched-invoice shape is unchanged.

## Capabilities

### New Capabilities
- `claims-pipeline-resilience`: pipeline tick isolation and LLM-quota discipline — extraction failures are per-claim visible flags/logs, never tick-fatal; no unbounded re-extraction loops.

### Modified Capabilities
- `invoice-matching`: multi-invoice emails match per contained invoice; candidate eligibility is governed by the invoice's own date (arrival date is not evidence); unreadable attachments produce a visible flag; extraction uses the provider-agnostic LLM seam.

## Impact

- Code: `app/openclaw/invoice_matching.py` (extraction, queries, gates), `app/openclaw/pipeline.py` (`run_once` isolation), small schema addition for the extraction cache (manual `ALTER TABLE`/`CREATE TABLE` against the live DB per CLAUDE.md).
- Specs: `openspec/specs/invoice-matching` delta; new `claims-pipeline-resilience` spec.
- Ops (tasks checklist, no code): kill the stray dashboard host process (PID 38572), recreate the container after merge, delete stray empty `C:\data\openclaw.db`.
- ADRs: consistent with 0006 (single process — reinforced by removing the duplicate instance), 0007 (ceiling rule unchanged, now applied per invoice), 0008 (status events unblocked, logic untouched), 0009 (completes the migration it prescribed).
