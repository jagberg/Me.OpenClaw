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
| `llm.py` | THE LLM seam (ADR-0009): `chat()` (tool loop), `extract()`, `extract_vision()` (Gemini-only, ADR-0010). No other module imports a provider SDK — except `gemini.py`, which is the Gemini implementation behind it. `_FALLBACK_MODELS` walks four Groq models on daily-budget exhaustion (per-model TPD, ADR-0017); `_is_daily_budget_exhausted` vs `_is_rate_limited` is the split that decides switch-model vs back-off-and-retry. |
| `gemini.py` | Gemini SDK calls, `_RateLimiter`, `llm_calls` logging (shared by all providers via import). |
| `gmail_client.py` | OAuth, `full_message_text` (includes PDF text — settlement breakdowns need it), attachment iteration. Read + drafts only; `send()` is forbidden (hard rule). |
| `db.py` | Schema (`CREATE TABLE IF NOT EXISTS` — live schema CHANGES to existing tables need manual DDL against `app/data/openclaw.db`), connections. |
| `telegram_bot.py` | Bot commands/callbacks (auth = single username), notify send helpers (`send_message_sync`, `send_document_sync`, `send_photo_sync`), 👍 ack on every incoming user message, `_append_result` (edits text OR caption — PDF alerts have no text). `/history` (paged claim-card images) and `/actions` (summary card + one tap-to-resolve card per outstanding action, capped at `ACTION_CARD_CAP`). |
| `message_log.py` | Durable record of every Telegram message in/out (`telegram_messages`): the RL dataset (raw payload + `app_version`), the audit trail for "did my tap register?", and the replay queue. `record_inbound` writes *before* handlers run, so a crash mid-handler leaves `processed_at IS NULL` and `replay_pending` re-runs it at startup. `expire_queue` drops rows from the queue after `MESSAGE_QUEUE_TTL_HOURS` but never deletes them. |
| `claim_card.py` | Pillow renderers for the Telegram cards: `render` (month-grouped claim history) and `render_actions_summary` (what's waiting on Justin, blocked items separated). Status labels/colours mirror `templates/index.html`'s `status_chip`. Fonts resolve DejaVu (Docker, installed by the Dockerfile) → Windows → `load_default`. |
| `main.py` + `templates/` | FastAPI dashboard: claims list, flags, CSV upload, condition entry. |
| `scheduler.py` | APScheduler wiring for ticks + Gmail ingest. |
| `config.py` | All env; `.env` loaded from cwd. Container paths are `/data/...` (compose binds host `app/data`). |
| `agent.py` | Telegram free-chat agent (LLM tool-calling; reads are direct, mutations are *proposals* confirmed by a tap). `system_prompt()` injects the real pet list **and today's date** — the model invented pet names when left to guess, and guessed at "July 2025" with no date to anchor on. It cannot browse or search mail, but it has **three named sweeps** that do touch Gmail: `reconcile_sent_invoice_requests`, `rematch_claims`, `poll_petcover_now`. They act directly (not proposals) because the tick already makes those exact calls unattended, and they're safe under ADR-0014 replay only because each is idempotent — ADR-0016. No code/spec/doc access: claim-level "why" is `claim_detail`, anything about the implementation it must decline. |
| `netbank_csv.py` | CSV upload parsing/dedupe into `bank_transactions`. |
| `tasks.py` / `reminders.py` / `gmail_ingest.py` | Assistant side (email → tasks/reminders), independent of claims. |
| `db_backup.py` | Drive backup of the SQLite DB. |
| `ssl_compat.py` | Windows strict-X.509 workaround (ADR-0005). |

## Gotchas that repeat

- Every notify message must carry the claim `#id` — Justin acts by id (`/mark`, `/pet`); regression test enforces it. This applies to the chat agent too: it used to be told the opposite ("never internal ids") and produced answers he couldn't act on.
- `notify_claim_states` is a **change-feed**, not a state list — it dedupes on `(telegram_notified_status, telegram_notified_flag)`, so a claim that stays outstanding is announced once and never again. `claim_status.pending_actions()` + the daily `pipeline.nudge_stale_actions` job are the state-based counterpart.
- `pending_actions()` is one entry per **claim** except for `SUBMISSION_LEVEL_ACTIONS` (currently `mark_sent` alone), which fold to one entry per submission carrying `claim_ids` + `group_id` + `members`. One Gmail draft is one email, so N cards asked for N-1 taps too many and taps 2..N hit an already-sent claim. The collapsed entry keeps `claim_id` = lowest member so `sent:{id}` tokens and `mark_sent` are untouched. Group id is `claim_status.submission_group_id` → `S6+7`, derived not stored (a column would need manual live DDL).
- `draft_id` is overloaded (claim drafts AND invoice-request drafts), so `invoice_request_sent_at IS NULL AND draft_id IS NOT NULL` matches almost every claim. Use `flag = 'invoice_request_drafted'` to find awaiting-invoice-request claims.
- Telegram messages with a PDF are documents: edit the **caption**, not text (`_append_result`).
- `message_log.mark_processed` refuses rows that already carry an `error`. PTB runs its error handler *inside* `process_update`, so a failed update otherwise reaches `mark_processed` looking successful and gets silently dropped from the replay queue.
- Outbound logging lives in `LoggedBot`, so anything that constructs a plain `telegram.Bot` bypasses the message log. The three `*_sync` senders use `LoggedBot` for exactly this reason.
- ERROR means *Justin must act*. Transient network failures are WARNING (`pipeline._is_transient`) — a Gmail `IncompleteRead` used to log a full traceback and read like a crisis.
- The updater task is fire-and-forget: nothing awaits it, so its death is silent. `polling_alive()` + `pipeline._watchdog_telegram_polling` (SIGTERM, compose restarts) is the safety net.
- Never replay a provider's whole assistant message back into `chat()`'s tool loop — `_assistant_turn` whitelists role/content/tool_calls. `model_dump()` echoed gpt-oss-120b's `reasoning` field and every following request 400'd (`reasoning is not supported with this model`). Whitelist, not a `reasoning` blacklist: the next output-only field would repeat it.
- Chat replies are sent with **no `parse_mode`**, so markdown arrives literally. The fallback models answer with pipe tables unless the prompt forbids it (`test_prompt_forbids_markdown_for_a_plain_text_channel`).
- Rate limits are two different failures: per-MINUTE tokens → back off, retry same model; per-DAY tokens → switch model, one attempt each (retrying can't free a daily cap). Only the 429 *body* names which — the headers never mention TPD.
- `email_extractions` caches successful extraction FOREVER; invalidate the row if you change what extraction must return.
- Vision attempts are refunded on `LLMUnavailableError` (provider outage ≠ unreadable scan).
- Invoice identity across claims: `invoice_number` first, else amount+date (`_already_claimed`).
