## 1. Prerequisites (Justin, manual)

- [ ] 1.1 Create the bot via BotFather, obtain `TELEGRAM_BOT_TOKEN`
- [ ] 1.2 Send `/start` to the bot once it's running, to trigger self-service chat-ID registration (see section 5) — no manual `getUpdates` lookup needed

## 2. Dependencies and config

- [x] 2.1 Add `python-telegram-bot` to `app` dependencies
- [x] 2.2 Add `TELEGRAM_BOT_TOKEN`, `TELEGRAM_USERNAME` (default `jagberg`) to `config.py` and `.env.example`

## 3. Data layer

- [x] 3.1 Add `telegram_registrations` table: `username TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, registered_at TEXT`
- [x] 3.2 Add `telegram_notified_status TEXT`, `telegram_notified_flag TEXT`, `reviewed_at TEXT` columns to `vet_claims`
- [x] 3.3 Extend `db.init_db()` / schema for the new table and columns (existing `CREATE TABLE IF NOT EXISTS` won't add columns to an already-created DB — confirm migration path for the live `openclaw.db`)

## 4. Shared update functions (extracted from dashboard routes)

- [x] 4.1 Extract the condition-text update in `main.py`'s `set_condition` into a plain function in `claim_forms.py` (or new module), called by both the FastAPI route and the Telegram handler
- [x] 4.2 Extract the pet-assignment update (dashboard pet picker) into a plain function the same way
- [x] 4.3 Confirm both extracted functions return a result the Telegram handler can turn into a reply message (success / already-set / not-found / validation error)

## 5. Bot process

- [x] 5.1 New module `telegram_bot.py`: build `Application`, register command handlers, username authorization check (`update.effective_user.username == config.TELEGRAM_USERNAME`) on every update
- [x] 5.2 Add `/start` handler: on matching username, upsert `(username, chat_id, registered_at)` into `telegram_registrations`, reply with confirmation; on non-matching username, no-op
- [x] 5.3 Wire bot startup/shutdown into the FastAPI app lifespan (or scheduler startup, matching how `scheduler.start()` is wired) — long-polling task runs alongside the existing pipeline job
- [x] 5.4 Add `/mark <claim_id> <condition text>` command → calls the extracted condition-text function, replies with result
- [x] 5.5 Add pet-assignment command (final name TBD, e.g. `/pet <transaction_id> <name>`) → calls the extracted pet-assignment function, replies with result
- [x] 5.6 Add `/process <claim_id>` command → calls `claim_forms.process_claim(claim_id)` directly, replies with the resulting status/flag
- [x] 5.7 Add `/mark <claim_id> reviewed` handling → sets `reviewed_at` on a `drafted` claim only, rejects otherwise; no send call anywhere in this path
- [x] 5.8 Add `/help` command listing available commands (basic usability, not in spec but trivial)
- [x] 5.9 Outbound-send helper reads `chat_id` from `telegram_registrations`; if no row exists, logs the gap loudly and skips the send rather than raising or silently dropping

## 5b. Status-tracking integration (post-merge of petcover-claim-status-tracking)

- [x] 5b.1 Merge master (claim status tracking + bank-as-ceiling matching) into this branch, resolving db/main/pipeline/.env conflicts; `petcover_reference` added to the live-DB column migration
- [x] 5b.2 Add `/vetemail <merchant name> <email>` — upsert into `vet_contacts` (only write path to that table)
- [x] 5b.3 Extract `mark_sent` route body into `claim_status.mark_sent()` (batch-aware), add `/sent <claim_id>` command
- [x] 5b.4 Add `/resolved <claim_id>` — records `confirmed_resolved` via `claim_status.confirm_resolved`
- [x] 5b.5 Extend `notify_claim_states()` to the full lifecycle: urgent (`matched`-blocked, `info_requested`, `suspended`), informational (`drafted`, `acknowledged`, `settled` with claimed/paid amounts from the settlement event, `declined`)

## 6. Outbound notifications

- [x] 6.1 In `pipeline.run_once()`, after existing matching/drafting steps, diff each claim's current `(status, flag)` against `(telegram_notified_status, telegram_notified_flag)`
- [x] 6.2 For a claim newly at `matched` with a missing-field flag, send a Telegram message identifying the claim, transaction, and missing field(s); update the notified columns
- [x] 6.3 For a claim newly at `drafted`, send a Telegram message with claim details and the Gmail draft link; update the notified columns
- [x] 6.4 Confirm no duplicate notification fires on a subsequent tick where state hasn't changed

## 7. Automated tests (`tests/test_telegram.py`, runnable assert-style like `test_core.py` — `python tests/test_telegram.py`)

- [x] 7.1 `test_start_registers_matching_username`: fake update with `username == config.TELEGRAM_USERNAME` → asserts a row lands in `telegram_registrations` with the right `chat_id`
- [x] 7.2 `test_start_ignores_non_matching_username`: fake update with a different/missing username → asserts no row is written to `telegram_registrations`
- [x] 7.3 `test_command_rejected_for_unauthorized_user`: fake `/mark` update from a non-matching username → asserts the claim row is untouched (no condition_text/pet_id/reviewed_at change)
- [x] 7.4 `test_mark_condition_matches_dashboard_path`: call the extracted condition-text function directly (not via FastAPI or Telegram) → asserts identical DB state to calling the dashboard route with the same input
- [x] 7.5 `test_process_advances_ready_claim`: seed a `matched` claim with all required fields, call the extracted `/process` handler function → asserts status becomes `drafted`
- [x] 7.6 `test_process_leaves_incomplete_claim_matched`: seed a `matched` claim missing condition text, call the same handler → asserts status stays `matched` and the reply names the missing field
- [x] 7.7 `test_notification_dedup`: seed a claim, call the notify-diff function twice with unchanged `(status, flag)` between calls → asserts exactly one send is attempted (fake/spy send function, no real Telegram call)
- [x] 7.8 `test_notification_fires_on_new_state`: change a claim's `(status, flag)` between two notify-diff calls → asserts a second send is attempted
- [x] 7.9 `test_reviewed_mark_requires_drafted`: call the reviewed-mark function against a `matched` (not `drafted`) claim → asserts `reviewed_at` stays unset and the call reports rejection
- [x] 7.10 `test_reviewed_mark_sets_timestamp_on_drafted`: call the reviewed-mark function against a `drafted` claim → asserts `reviewed_at` is set and no Gmail-send function is called (spy/mock the send path)
- [x] 7.11 `test_notification_skipped_when_unregistered`: no row in `telegram_registrations`, call the outbound-send helper → asserts it returns without raising and without attempting a network call
- [x] 7.12 `test_vetemail_upserts_contact`: /vetemail twice for the same merchant → one row, latest email wins
- [x] 7.13 `test_vetemail_rejected_for_unauthorized_user`: non-matching username → no `vet_contacts` row
- [x] 7.14 `test_notification_fires_on_info_requested`: `info_requested` status notifies once (urgent wording), dedups on repeat
- [x] 7.15 `test_settled_notification_includes_amounts`: settled event detail `{claimed_amount, paid_amount}` → both figures in the message
- [x] 7.16 `test_sent_command_advances_batch`: two drafted claims sharing a draft_id, `/sent` on one → both `sent`
- [x] 7.17 `test_resolved_records_event`: `/resolved` → `confirmed_resolved` event row for the claim

## 7b. Interactive actions + refinements (live iteration)

- [x] 7b.1 Interactive condition entry: prompt shows invoice line items; past conditions as tap buttons; "Other" → free-text reply; no dashboard mention
- [x] 7b.2 Per-item condition split: assign a condition per line item; group into one form row per condition (summed amounts); guard when items lack amounts
- [x] 7b.3 One-tap pet-assignment buttons on unassigned matched claims
- [x] 7b.4 "Wrong invoice" unmatch button: record rejected email (`rejected_email_ids`), reset to `pending_match`, matcher skips rejected
- [x] 7b.5 Submissions identified by Petcover reference / pet name, never internal claim ids; batched drafted claims notify once with a self-contained summary + ✅ Mark sent button
- [x] 7b.6 Plain-language "why" summary for blocked/suspicious matched claims (date, vet, line items)
- [x] 7b.7 Invoice-request email uses Justin's template (visit date dd-MMM-yyyy, pet + surname, amount, sign-off)
- [x] 7b.8 Persistent Docker service (`restart: unless-stopped`); async-SSL `wrap_bio` fix; case-insensitive username auth
- [x] 7b.9 Tests for all of the above in `tests/test_telegram.py` (22 checks, incl. the split-callback path that caught a missing-json regression)

## 8. Live verification (manual, real Telegram + Gmail)

- [ ] 8.1 Unauthorized username sending a command has no effect against the running bot
- [ ] 8.2 `/mark` on a real `matched` claim missing condition text advances it to `drafted` on the next pipeline tick (or via `/process`)
- [ ] 8.3 A real claim reaching `drafted` produces a Telegram message with a working Gmail draft link
- [ ] 8.4 `/mark <claim_id> reviewed` on a real `drafted` claim sets `reviewed_at` and does not touch the Gmail draft; same command on a non-`drafted` claim is rejected
- [ ] 8.5 Confirm no command or code path can trigger a Gmail send
- [ ] 8.6 `/vetemail` for a real merchant with no prior invoice history, then confirm the invoice-request draft addresses it
- [ ] 8.7 `/sent` on a real drafted claim, then confirm Petcover's acknowledgement reply produces a Telegram push (and `/resolved` clears a real info request)
