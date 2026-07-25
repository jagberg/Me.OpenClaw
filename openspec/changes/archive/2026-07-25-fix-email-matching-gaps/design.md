# Design — fix-email-matching-gaps

## Context

See proposal.md for the five confirmed root causes. Constraints from CLAUDE.md/ADRs: bank charge is a ceiling per invoice (ADR-0007), failures must be visible as flags/logs, LLM use goes through `llm.py` (ADR-0009), live DB schema changes need manual DDL, Gmail drafts only. All fixes verified feasible against real Gmail/DB on 2026-07-21 (Groq works today; the exact-match invoices exist).

## Goals / Non-Goals

**Goals:** every invoice currently sitting in Gmail for a pending claim matches on the next healthy pipeline run; a failed extraction never starves the rest of the tick; extraction quota use is bounded per email, not per tick.

**Non-Goals:** provider fallback chains (env-var swap stays the mechanism per ADR-0009); re-architecting the query strategy beyond what the confirmed misses require; OCR for image-only PDFs (flag, don't solve); fixing the stray dashboard process in code (ops task).

## Decisions

1. **Provider seam**: `_extract_invoice` uses `llm.extract` (import swap, catch `llm.LLMUnavailableError`). No new abstraction — the seam already exists; this is the missed ADR-0009 call site.

2. **Multi-invoice extraction**: prompt returns `{"invoices": [...]}` (each with date/amount/services/items). Matcher loops invoices within an email, applying the existing ceiling + invoice-date gates per invoice; first passing invoice is stored in `invoice_data` in the current single-invoice shape (plus the shared `matched_email_id`). Downstream (`claim_forms`, dashboard, Telegram) untouched. Alternative — per-attachment extraction — rejected: more Gmail/PDF plumbing for the same result; the bulk replies list per-invoice amounts in one text blob already.

3. **Arrival window**: keep the cheap `merchant + txn±3d` query, and make the wide `after:txn_date` variant (today gated on `invoice_request_sent_at`) unconditional for BOTH the merchant and spouse queries. Both merchant queries carry `-from:me`, plus a SENT-label skip in the loop — found during live verification: the wide window surfaced Justin's own invoice-request emails, whose bodies list visit dates + amounts, and 12 claims false-matched them (exact amount + exact date). Own mail is never an invoice. Eligibility truth stays `_invoice_date_plausible` (invoice's own date vs txn date) — arrival date is only a search hint. `maxResults` stays 5 per query; with the extraction cache (below) re-scanning wide result sets is cheap. Alternative — reconcile the yearly bulk requests into `invoice_request_sent_at` — rejected: fragile subject matching, and the wide window subsumes it.

4. **Extraction cache**: new table `email_extractions (message_id TEXT PRIMARY KEY, extracted_json TEXT, extracted_at TEXT)` — one LLM call per email ever (bulk replies get re-tested against later claims from cache for free). A failed call stores nothing (retry next tick is correct once failures are per-claim). Negative-match memory falls out naturally: cached extraction + deterministic gates = no repeated LLM cost; `rejected_email_ids` continues to handle Justin's explicit unmatches. Live DDL: single `CREATE TABLE` (additive; `db.py` mirror included).

5. **Tick isolation**: in `run_once`, wrap the per-claim match/draft in try/except. On `LLMUnavailableError`: log, set claim flag `invoice extraction unavailable — <reason>` (cleared on next successful match attempt), and stop *matching* for the tick (quota is global); other stages still run. On any other exception: flag that claim, continue with the next. Alternative — retry queue table — rejected: the 15-min interval already is the retry loop.

6. **Unreadable attachments**: `full_message_text` short-circuit unchanged; in the matcher, if a candidate from a vet-addressed query yields extraction with no amount AND the email has a PDF attachment whose text came back empty, flag the claim `invoice attachment unreadable — <subject>` (persisting until matched). Justin then requests a readable copy; no OCR dependency. Alternative — swap/augment pypdf with `markitdown` — tested 2026-07-21 on the real failing PDFs and rejected: Kingsgrove's PDFs are pure scans (8 pages, one image each, no fonts) — markitdown also extracts 0 chars, and on text PDFs (MediPaws) pypdf already extracts everything markitdown does (245 amounts both). New dependency, zero gain; only OCR would change the outcome, and the flag-and-ask-the-vet path costs nothing.

7. **Spouse vet-confirm tightening**: accept when the known vet email appears, or when a merchant word of length ≥ 5 that is not a generic token (`vet`, `veterinary`, `animal`, `hospital`, city/suburb words already excluded by the AU-state strip) appears. Confirmed live false-positive ("Kings" matching a human colonoscopy forward via "Kingsford"-like tokens) dies; all three real forwards still pass (they contain the vet's email address).

## Risks / Trade-offs

- [Wide window pulls old unrelated vet emails] → invoice-date gate rejects them; cache means they cost one extraction each, once ever.
- [Multi-invoice JSON increases extraction output size] → Groq has no context cap; prompt keeps `[]` fallback for unreadable itemization.
- [`maxResults=5` may still miss a candidate in noisy threads] → acceptable now; revisit with pagination only if a confirmed miss appears (log result counts to see it).
- [Cache staleness if an email's PDF was re-fetched wrong once] → `unmatch` (existing) plus a manual row delete covers the rare case; not worth invalidation logic.

## Migration Plan

1. Merge code; run `CREATE TABLE email_extractions ...` against `app/data/openclaw.db`.
2. Ops: kill stray dashboard host process (PID 38572, `Me.OpenClaw-dashboard` worktree), delete empty `C:\data\openclaw.db`, `docker compose up -d --build --force-recreate` in the live worktree.
3. Watch one pipeline tick: claims #1 (Shire $407.56), #3 ($141.87), #13 ($944.50), #15 ($10.50) should match from existing Gmail content. Rollback = revert commit; the new table is additive and inert.

## Open Questions

- None blocking. (Whether to also auto-reconcile yearly bulk requests into `invoice_request_sent_at` is moot once the wide window is unconditional.)
