# Tasks — fix-email-matching-gaps

## 1. Provider seam + tick isolation (unblocks everything else)

- [x] 1.1 `invoice_matching._extract_invoice`: swap `gemini.extract` → `llm.extract`; drop the `gemini` import; propagate `llm.LLMUnavailableError` — DONE (also: `llm.extract` now wraps `GeminiUnavailableError` into `LLMUnavailableError` so callers handle one type)
- [x] 1.2 `pipeline.run_once`: per-claim try/except — generic exception → flag claim and continue; `LLMUnavailableError` → flag `invoice extraction unavailable — <reason>`, skip remaining matching, still run claim forms / Petcover polling / notifications — DONE (covered by 2 new tests)
- [x] 1.3 Clear the `invoice extraction unavailable` / `invoice matching error` flags on the next match attempt — DONE (covered by test)
- [ ] 1.4 Verify live post-deploy: one container `run_once` completes end-to-end and `poll_petcover_status` executes (blocked on 6.3 — matching itself verified live from host, 2026-07-21)

## 2. Extraction cache

- [x] 2.1 `email_extractions` table in `db.py` + manual `CREATE TABLE` against live `app/data/openclaw.db` — DONE (verified table exists)
- [x] 2.2 `match_claim` consults cache before `llm.extract`; stores successful extractions only — DONE
- [x] 2.3 Verified live: full 12-claim pass costs 2–9 LLM calls first time, cached afterwards (llm_calls counted before/after); 11 emails cached

## 3. Multi-invoice extraction

- [x] 3.1 `EXTRACTION_PROMPT` returns `{"invoices": [...]}`; parser accepts legacy single-object; salvages truncated replies (confirmed live: 12k-char PDF reply cut mid-array) — DONE
- [x] 3.2 `match_claim` iterates contained invoices via `_pick_invoice` (ceiling + invoice-date per invoice) — DONE
- [x] 3.3 Verified live: claim #1 ($407.56) AND claim #3 ($141.87) both matched the same Shire bulk reply (`19f7c8844bdac573`, 3 invoices), each to its own invoice with correct dates

## 4. Arrival-window fix (late forwards)

- [x] 4.1 `_build_queries`: open-ended `after:txn_date` unconditional for merchant AND spouse queries; narrow ±window kept; **`-from:me` on merchant queries + SENT-label skip** — found live: the wide window surfaced Justin's own invoice-request emails (bodies list visit dates+amounts) and 12 claims false-matched them; all reset and excluded
- [x] 4.2 Verified live: #13 ($944.50, inv 23/02) matched `19f7ce72da83efeb`, #15 ($10.50, inv 20/01) matched `19f7ce6c519637b7`; claim #3 matched via the Shire bulk reply (its receipt-forward also confirmed passing vet-check). Extraction prompt now demands the visit/service date — the model initially returned the PDF print date and the date gate rejected the right invoice
- [x] 4.3 MediPaws outcome (differs from expectation, correctly): the "Individual Invoices" PDF genuinely bills 13 Apr as ONE $2,521.46 invoice = claims #11 ($551.06) + #12 ($1,970.40) paid as two charges. Not matchable without guessing a split → both flagged `invoice dated 2026-04-13 totals $2521.46 — exceeds this charge; likely one invoice paid over several charges, split/confirm manually`

## 5. Visibility + confirmation tightening

- [x] 5.1 Unreadable-attachment flag — DONE; verified live: all 6 Kingsgrove claims (#6/7/8/17/20/22) flagged `invoice attachment unreadable — Re: Invoice request (past 12 months)…` (their reply's PDFs are pure scans: 8 pages, one image each, no fonts — markitdown tested, also 0 chars, dependency rejected)
- [x] 5.2 `_forward_confirms_vet` tightened (word-boundary, ≥5 chars, generic tokens excluded); verified live: colonoscopy forward (`1989bccb1ab2a56a`) now FAILS, real Shire forward (`19f7ce6988ebfb1a`) still PASSES
- [x] 5.3 Tests: 10 added to `tests/test_core.py` (multi-invoice pick incl. real Shire numbers, truncated-reply salvage, oversized-invoice detection, open-ended+`-from:me` queries, cache hit/no-second-call, failed-parse-not-cached, vet-confirm word boundary, tick isolation ×2) — all 37 pass

## 7. Split-bill picker (added 2026-07-22, Justin's ask)

- [x] 7.1 `split_proposals` table (db.py + live DDL applied); `_propose_split` pairs the claim with the one same-vet sibling whose charge completes the invoice total; dedupes open proposals
- [x] 7.2 `resolve_split_proposal`: chosen claim matched with full invoice (ceiling = charges combined, validated), sibling → status `absorbed`, proposal resolved; refusals for wrong/moved/closed cases
- [x] 7.3 Telegram: `notify_split_proposals` pushes picker once (invoice + both charges + Use-#N buttons); `usebill:` callback wired
- [x] 7.4 Tests: proposal create/dedupe/resolve/absorb, no-proposal-when-sum-wrong, notify-once (40 tests total, all pass)
- [ ] 7.5 Dashboard view of open split proposals — deferred ("at some stage")

## 8. Split-bill rework: merge-confirm, not pick (2026-07-22 — "not clear how I should match it")

- [x] 8.1 Verified against the real PDF: MediPaws invoice #411193 is ONE invoice, ONE pet (Aari), $2,521.46 — its payment section lists both card payments (−1,970.40, −551.06). The two bank charges are two payments of the same invoice; which claim carries it is bookkeeping (Petcover sees the invoice, never the charges), so a per-claim pick was meaningless
- [x] 8.2 `merge_split_proposal` (auto-primary = larger charge) + `reject_split_proposal` (flags both claims, pair never re-proposed, manual flag not overwritten next tick); `resolve_split_proposal` kept for the legacy Use-# buttons
- [x] 8.3 Telegram message rewritten: invoice + both charges + sum, "payment records list both charge amounts" evidence line when detected (`_text_amounts` captured at extraction time), ✅ Merge / ❌ Not the same invoice buttons
- [x] 8.4 Tests: auto-primary merge, reject + never-re-propose + flag preserved, payments-confirmed detection (43 tests, all pass)

## 9. Invoice validation + per-visit splitting + batch chunking (2026-07-22, approved plan)

- [x] 9.1 `find_invoice_segment` (claim_forms): segments PDFs at invoice headers (`INVOICE #NNN` / `Tax Invoice`), matches by claim's invoice total + patient name; the account statement fails by construction (verified: 0 headers/patients vs 11 in the individual-invoices bundle). Dry-run on real PDFs: Aari p1 found, Echo cross-pet refused, statement rejected
- [x] 9.2 `ensure_invoice_file`: auto-extracts the claim's pages to `INVOICE_OUTPUT_DIR` (`/data/invoices` = host `app\data\invoices`), sets `invoice_file_path` (previously NOTHING wrote it — claims stalled forever on manual paths); auto-assigns pet from the invoice's printed `Patient name:`; inadequate/unreadable attachment → flag naming the vet (`matched`+flag ⇒ Telegram + dashboard). Never overwrites manual paths
- [x] 9.3 Pipeline `_draft_matched_claims`: ready claims batched per pet, ≤4 per Petcover form via previously-uncalled `process_claim_batch` (overflow → next draft); not-ready claims keep per-field flagging via `process_claim`
- [x] 9.4 Telegram: flagged `pending_match` claims now push too (transient LLM flags + `invoice_request_drafted` excluded), grouped by (merchant, flag) — the 6 Kingsgrove unreadable claims = one message
- [x] 9.5 Tests: segmentation (real page texts incl. statement), inadequate flag, never-overwrite, 4+2 chunking order, grouped pending notify (49 tests, all pass)
- [ ] 9.6 Dashboard list of inadequate-invoice/split-proposal items — deferred with 7.5

## 6. Ops (record what was actually done)

- [x] 6.1 Kill the stray dashboard host process — DONE 2026-07-21: killed PIDs 38572 + 28480 (same `uvicorn --port 8787` tree from `C:\Code\Me.OpenClaw-dashboard`, stale pre-edit env → Gemini provider, shared Gmail quota burn); verified no python/uvicorn processes remain
- [x] 6.2 Stray empty `C:\data\openclaw.db` deleted — DONE 2026-07-21
- [ ] 6.3 After merge to the live worktree branch: `docker compose up -d --build --force-recreate` in `C:\Code\Me.OpenClaw-telegram-claimquery`; confirm the container's first tick keeps the 4 new matches, polls Petcover (first `claim_status_events` rows), and Telegram-notifies the matched/flagged claims

## Live results summary (2026-07-21, host run of new matcher)

- **Matched (4)**: #1 $407.56, #3 $141.87 (Shire bulk reply, multi-invoice); #13 $944.50, #15 $10.50 (Gabi's late SAH forwards)
- **Flagged for Justin (8)**: #11/#12 one-invoice-two-charges (MediPaws $2,521.46); #6/7/8/17/20/22 unreadable scanned PDF (ask Kingsgrove for text invoices)
- **Awaiting vet reply (2)**: #4/#5 Bankstown — invoice requests drafted, no reply exists in Gmail yet
