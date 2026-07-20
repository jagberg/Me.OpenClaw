# 2026-07-20 — Vet claim pipeline: findings and fixes

Investigation triggered by: "have the vet emails from last night/today been processed?" Answer was no — the pipeline had been silently stuck since 22:40 the previous night. Four separate issues found and fixed; one real limitation found and left open.

## Context: multiple worktrees, one shared DB

The repo is checked out as several git worktrees sharing one bind-mounted data directory (`C:/code/Me.OpenClaw/app/data` → `/data` in Docker):

| Worktree | Branch | Role |
|---|---|---|
| `Me.OpenClaw` | `master` | ahead of the deployed branch in some ways (has the "unmatch" feature); this investigation's fixes were mirrored here |
| `Me.OpenClaw-telegram-claimquery` | `feature/telegram-claim-query` | **actually deployed** — the Docker container running the live Telegram/claims service |
| `Me.OpenClaw-telegram` | `feature/telegram-comms` | not touched this session, status not re-verified |
| `Me.OpenClaw-dashboard` | `feature/dashboard` | separate, unrelated dashboard redesign work |

`master` and the deployed branch have diverged in both directions — worth reconciling (merge one into the other, then redeploy from a single branch) before this happens again with something more dangerous than a UI feature gap.

## Issue 1 — Gmail task-capture had zero filtering

`gmail_ingest.poll_once()` turned literally every inbox email into an "open task" — 78 accumulated, nearly all noise (Amazon deliveries, bank/PayPal notifications, marketing, cold outreach, German spam). Two real signals cover essentially all of it, confirmed against the real 78-row sample:
- `List-Unsubscribe` header present, or
- sender local-part matches a generic automated pattern (`no-reply@`, `service@`, `notifications@`, `info@`, `hello@`, ...).

Genuine human replies (e.g. a vet clinic's reception replying to an invoice request) matched neither signal. Fix is keyword/header-based, not an LLM call — the 20/day Gemini cap can't absorb classifying every inbox email. The 78 existing noise rows were deleted (plus 2 orphaned reminders) after confirming every one was noise.

**Fixed in:** `gmail_ingest.py` (`_is_noise`, applied in `poll_once`).

## Issue 2 — `invoice_request_sent_at` never reconciled when Justin sends manually

By design (CLAUDE.md: never auto-send), invoice-request drafts are created but never sent by the app — Justin sends them himself, then is expected to click "mark invoice-request sent" on the dashboard. In practice that click gets missed. Missing it keeps `invoice_request_sent_at` NULL, which keeps the Gmail search window pinned to the original narrow `±INVOICE_MATCH_WINDOW_DAYS` (default 3 days) around the transaction date — so a reply arriving weeks or months later is never searched for at all.

Confirmed live: two real vet replies (Kings Vet, 2+ months after the transaction; Shire Vet, ~2 weeks after) sat unmatched for exactly this reason.

**Fix:** `pipeline._reconcile_sent_invoice_requests()`, run at the start of every `run_once()` tick. Detects a manually-sent draft via Gmail's own `SENT`/`DRAFT` labels on the stored message id (not by guessing from Sent-folder text) and sets `invoice_request_sent_at` once confirmed sent.

**Caveat:** if the stored `draft_id` message was discarded/replaced rather than sent directly (confirmed for two specific claims — the id 404'd, meaning the message no longer exists under that id), this reconciliation can't detect it. Those two were fixed by hand this session; the general case is handled going forward.

## Issue 3 — Spouse-forward fallback had no content verification

`invoice_matching`'s Gmail queries: a merchant-name search first, then (if no match) a fallback searching all emails from the configured spouse address, with **no merchant filtering at all** in that fallback query (rationale in the old code comment: a forward rarely repeats the merchant's bank-descriptor string verbatim). That was fine when the fallback's date window was narrow; it became dangerous once Issue 2's reconciliation widens the window to open-ended `after:`.

**Confirmed live:** two claims for two *different* vets both matched the *same* unrelated forwarded invoice, purely because its amount happened to fit under both claims' ceilings.

**Fix:** `_forward_confirms_vet()` — before accepting a spouse-fallback candidate, require the vet's own name (individual significant words, not the full bank-descriptor phrase) or their known contact email to actually appear in the fetched message text. A forward's quoted content reliably carries one or the other (the quoted `From:` line, or the vet's own signature). Verified against the real false-positive case (rejected) and the real Kingsgrove/Shire Vet forwards (accepted).

## Issue 4 — No date-plausibility check on the extracted invoice

Even after Issue 3's fix, a *second* false match was found: a claim matched a real invoice from the *correct* vet, but for a *different* visit — the invoice's own extracted date was 19 days off the transaction it got attached to. `match_claim()` only ever checked the ceiling (amount), never whether the invoice's own date made sense for that specific transaction. With an open-ended search window and a vet who's billed the same family multiple times, the first candidate under the ceiling isn't necessarily the right one.

**Fix:** `_invoice_date_plausible()` — reject a candidate whose extracted invoice date is more than `INVOICE_MATCH_WINDOW_DAYS` away from the transaction date. Missing/unparseable invoice date is allowed through unchanged (can't check absence of evidence, and this is a pre-existing extraction gap, not something this fix should start rejecting on).

Both false matches were caught and manually reverted (`status` back to `pending_match`, `matched_email_id`/`invoice_data`/`flag` cleared) before propagating into a claim form or draft.

## Known limitation found, not fixed — scanned PDF invoices

After all four fixes, Kings Vet's and Shire Vet's claims still correctly return no match — not a bug. Their invoice-reply email has two real PDF attachments, but `pypdf` extracts empty text from both: almost certainly scanned/photographed invoices with no embedded text layer, not real digital PDFs. `gmail_client.full_message_text` already documents "no OCR support" for image attachments; a scanned PDF hits the identical wall without being labeled as one.

This is a real, separate feature gap (OCR on invoice attachments) worth a deliberate decision later — not patched here.

## Also this session (unrelated to the above, same investigation window)

- Added a twice-daily Google Drive DB backup (`db_backup.py`, `scripts/backup_db.py`), using sqlite's own backup API (safe under concurrent writes) and a new `drive.file` OAuth scope on the existing Gmail credentials. Verified end-to-end (real upload, real log file in a `logs/` subfolder). Scheduled via Windows Task Scheduler (`OpenClawBettyVet-Backup`, 10am/10pm daily) rather than the app's own APScheduler, since nothing here guarantees the Docker container is always running.
- Cleared the dead `CEREBRAS_API_KEY` from `.env` (Cerebras returns 402 on every model as of 2026-07); left `GEMINI_API_KEY` in place — verified it's a genuinely wired fallback for `extract()`-only call sites (invoice matching, vet detection, follow-up-date extraction), though **not** for the Telegram bot's `chat()` tool-calling loop, which has no Gemini path at all.
- The pipeline's LLM calls were failing with a Groq 403 ("Access denied — check your network settings") for most of this session; root cause was a VPN (PIA) on the host, not a code or account issue.

## Outstanding / next steps

1. **Consolidate worktrees onto one deploy branch.** Master and the deployed branch have diverged both ways; pick one, merge the other in, redeploy from it, retire the stale branch.
2. **Run `tests/test_core.py`** in both touched checkouts before treating any of this as done — it was not run against tonight's changes, only verified via ad-hoc live-data checks against the real Gmail/DB.
3. **Decide on OCR** for scanned invoice attachments, or make the "stuck on unreadable invoice" state visible on the dashboard instead of an indistinguishable `pending_match`.
4. **Tag Docker images** (not just `latest`) so a bad rebuild has a rollback path.
5. Don't hand-edit the live DB via a host-side script while the container is running — a `disk I/O error` was hit this session from exactly that (host + container both holding connections to the same sqlite file concurrently, over a Windows↔Docker-Desktop bind mount). Stop the container for direct surgery, or route through the app's own endpoints.
