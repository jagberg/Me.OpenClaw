# app/openclaw — module map

Root rules live in the repo-root `CLAUDE.md` (hard rules, domain rules, working style). This file is the "where does X live" index.

## Pipeline (runs every tick — `pipeline.run_once`, order matters)

| Module | Owns |
|---|---|
| `pipeline.py` | Orchestration: match → extract invoice files → draft claims (≤4/batch) → reconcile sent requests → poll Petcover → Telegram notifies. Per-claim error isolation (`_TRANSIENT_MATCH_FLAGS`); one claim's failure never kills the tick. |
| `vet_detection.py` | NetBank CSV rows → is-this-a-vet (keywords first, LLM fallback, `non_vet_merchants` denylist) → `vet_claims` rows. |
| `invoice_matching.py` | Gmail search per claim (`_build_queries`: merchant narrow+wide, spouse fallback, `-from:me` + SENT-label guards), LLM invoice extraction (cached in `email_extractions`), vision-OCR fallback for image-only scans (ADR-0010, `vision_ocr_attempts` 3-cap), `_pick_invoice` gates (ceiling ADR-0007, date plausibility, `_already_claimed`, closest-amount ranking), split/merge proposals (one invoice paid over several charges), invoice-request drafting when nothing found. |
| `claim_forms.py` | Petcover PDF form filling, per-visit invoice page extraction (`find_invoice_segment` text path / vision `page` slice path), `ensure_invoice_file`, pet auto-assign from printed patient facts, Gmail draft creation (`process_claim` single / `process_claim_batch` ≤4 same-pet), condition/pet setters used by Telegram + dashboard. |
| `claim_status.py` | Petcover reply polling → append-only `claim_status_events` (ADR-0008), reference learning from acknowledgements, mark-sent, correlation (pet + submitted pool, NO date windows), settlement parsing (amounts live in the PDF attachment). |

## Infrastructure

| Module | Owns |
|---|---|
| `llm.py` | THE LLM seam (ADR-0009): `chat()` (tool loop), `extract()`, `extract_vision()` (Gemini-only, ADR-0010). No other module imports a provider SDK — except `gemini.py`, which is the Gemini implementation behind it. |
| `gemini.py` | Gemini SDK calls, `_RateLimiter`, `llm_calls` logging (shared by all providers via import). |
| `gmail_client.py` | OAuth, `full_message_text` (includes PDF text — settlement breakdowns need it), attachment iteration. Read + drafts only; `send()` is forbidden (hard rule). |
| `db.py` | Schema (`CREATE TABLE IF NOT EXISTS` — live schema CHANGES to existing tables need manual DDL against `app/data/openclaw.db`), connections. |
| `telegram_bot.py` | Bot commands/callbacks (auth = single username), notify send helpers (`send_message_sync`, `send_document_sync`, `send_photo_sync`), 👍 ack on every incoming user message, `_append_result` (edits text OR caption — PDF alerts have no text). `/history` (paged claim-card images) and `/actions` (summary card + one tap-to-resolve card per outstanding action, capped at `ACTION_CARD_CAP`). |
| `claim_card.py` | Pillow renderers for the Telegram cards: `render` (month-grouped claim history) and `render_actions_summary` (what's waiting on Justin, blocked items separated). Status labels/colours mirror `templates/index.html`'s `status_chip`. Fonts resolve DejaVu (Docker, installed by the Dockerfile) → Windows → `load_default`. |
| `main.py` + `templates/` | FastAPI dashboard: claims list, flags, CSV upload, condition entry. |
| `scheduler.py` | APScheduler wiring for ticks + Gmail ingest. |
| `config.py` | All env; `.env` loaded from cwd. Container paths are `/data/...` (compose binds host `app/data`). |
| `agent.py` | Telegram free-chat agent (LLM tool-calling; reads are direct, mutations are *proposals* confirmed by a tap). `system_prompt()` injects the real pet list — the model invented pet names when left to guess. It has NO mailbox access: `reconcile_sent_invoice_requests` (Gmail SENT labels) is the only thing that touches mail, and the prompt requires saying so rather than implying it looked. |
| `netbank_csv.py` | CSV upload parsing/dedupe into `bank_transactions`. |
| `tasks.py` / `reminders.py` / `gmail_ingest.py` | Assistant side (email → tasks/reminders), independent of claims. |
| `db_backup.py` | Drive backup of the SQLite DB. |
| `ssl_compat.py` | Windows strict-X.509 workaround (ADR-0005). |

## Gotchas that repeat

- Every notify message must carry the claim `#id` — Justin acts by id (`/mark`, `/pet`); regression test enforces it. This applies to the chat agent too: it used to be told the opposite ("never internal ids") and produced answers he couldn't act on.
- `notify_claim_states` is a **change-feed**, not a state list — it dedupes on `(telegram_notified_status, telegram_notified_flag)`, so a claim that stays outstanding is announced once and never again. `claim_status.pending_actions()` + the daily `pipeline.nudge_stale_actions` job are the state-based counterpart.
- `draft_id` is overloaded (claim drafts AND invoice-request drafts), so `invoice_request_sent_at IS NULL AND draft_id IS NOT NULL` matches almost every claim. Use `flag = 'invoice_request_drafted'` to find awaiting-invoice-request claims.
- Telegram messages with a PDF are documents: edit the **caption**, not text (`_append_result`).
- `email_extractions` caches successful extraction FOREVER; invalidate the row if you change what extraction must return.
- Vision attempts are refunded on `LLMUnavailableError` (provider outage ≠ unreadable scan).
- Invoice identity across claims: `invoice_number` first, else amount+date (`_already_claimed`).
