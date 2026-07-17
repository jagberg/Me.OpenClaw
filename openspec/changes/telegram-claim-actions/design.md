## Context

OpenClaw runs as a single local FastAPI process (uvicorn) plus an APScheduler background job (`pipeline.run_once`, every `VET_CLAIM_PIPELINE_INTERVAL_MINUTES`) — no exposed port beyond localhost, no existing outbound-push channel (ADR-0003). Claims currently stall at `matched` waiting for Justin to visit the dashboard and fill condition text or assign a pet. This change adds Telegram as a second, phone-reachable entry point into the same update paths the dashboard already uses.

## Goals / Non-Goals

**Goals:**
- Push a message to Justin's phone the moment a claim needs input or reaches `drafted`.
- Let him supply the missing input, or force an immediate re-check of one claim, by replying in Telegram.
- Reuse the existing dashboard update logic exactly — no parallel business logic to keep in sync.

**Non-Goals:**
- Sending the Gmail claim email from Telegram — stays human-only, from the Gmail draft itself.
- Replacing the dashboard — it stays the primary read surface (side-by-side transaction/invoice view); Telegram is action-only.
- General-purpose chat/NLU — commands are fixed-format (`/mark`, `/process`), not free-text parsing. Free-text condition capture with pick-list history (named as deferred in `claim-form-automation`) stays deferred; this change only wires the *transport*, using the same explicit-command shape as the dashboard form.
- Reminders/tasks notification (ADR-0003's original scope) — out of scope for this change; claims only.

## Decisions

### Decision: Long-polling (`getUpdates`), not a webhook
OpenClaw has no exposed port and no public HTTPS endpoint (local-only posture, ADR-0004-adjacent). A webhook would require exposing a public URL (ngrok/reverse proxy) purely to receive Telegram callbacks — new attack surface for a single-user tool. Long-polling runs entirely outbound from OpenClaw's process, same trust model as the existing Gmail poller.

**Alternatives considered:**
- **Webhook** — Pros: lower latency, no polling loop. Cons: requires a public HTTPS endpoint, TLS cert, and firewall exposure for a single-user bot. **Why not**: exposure isn't worth it for a personal tool with no existing inbound surface.

### Decision: `python-telegram-bot` (async, `Application`/`CommandHandler`) run as a background task alongside APScheduler
Mature, actively maintained, handles long-polling and command routing without hand-rolling Telegram's HTTP API. Runs as an `asyncio` task started alongside the FastAPI app's lifespan, not a separate process — keeps deployment identical to today (`docker-compose.yml`, one container).

**Alternatives considered:**
- **Raw `requests` calls to Telegram's Bot API** — Pros: zero new dependency. Cons: hand-rolled long-polling loop, offset tracking, retry/backoff — python-telegram-bot already solves this correctly. **Why not**: reinventing a solved, small problem for no benefit at this scale.
- **`aiogram`** — Pros: also solid. Cons: less commonly paired with FastAPI examples, no material advantage over `python-telegram-bot` here. **Why not**: no reason to prefer it.

### Decision: Command handlers call the existing update functions directly, not HTTP self-calls
`/mark` and pet-assignment commands call the same Python functions the dashboard routes call (extract the update logic out of `main.py`'s route bodies into small functions in `claim_forms.py`/a new module, called from both the FastAPI route and the Telegram handler) rather than the bot making an HTTP request back to its own FastAPI server. Avoids a pointless network round-trip to itself and keeps one code path per update, not two.

**Alternatives considered:**
- **Telegram handler calls `POST /claims/{id}/condition` over HTTP (self-loopback)** — Pros: literally zero duplicate logic, route stays the single source of truth. Cons: adds a real HTTP call (auth-less, since it's localhost-only, but still an odd pattern) for what's a plain function call. **Why not**: unnecessary indirection; extracting a shared function is simpler and just as DRY.

### Decision: Track last-notified state on `vet_claims` directly (new columns), not a separate notification-log table
Add `telegram_notified_status TEXT` and `telegram_notified_flag TEXT` to `vet_claims`. A notification fires when `status` or `flag` differs from what's stored, then the columns are updated. Matches the existing schema style (flat columns on the claim row, e.g. `invoice_request_sent_at`) rather than introducing a new table for what's a two-field diff.

**Alternatives considered:**
- **Separate `telegram_notifications` event-log table** — Pros: full history of what was sent when, matches the append-only pattern used for Petcover status history in the sibling `petcover-claim-status-tracking` change. Cons: overkill for "did we already notify about this exact state" — no product need for a full history here, unlike claim lifecycle events. **Why not**: adds a table for a check two columns answer.

### Decision: "Reviewed" is a timestamp column, not a status transition
Add `reviewed_at TEXT` to `vet_claims`, set once via `/mark <claim_id> reviewed`. Deliberately not a new `status` value (e.g. `reviewed`) — status still only tracks the fill/draft/send pipeline stage; review is an orthogonal, Justin-only signal layered on top of `drafted`, same relationship `invoice_request_sent_at` already has to `status`.

**Alternatives considered:**
- **New `reviewed` status between `drafted` and a hypothetical `sent`** — Pros: reviewed-ness visible in the same field the rest of the pipeline already keys off. Cons: nothing currently reads a `sent` status (send is manual, outside OpenClaw's tracking) so inserting `reviewed` into the status enum has no consumer and risks other status-based logic (e.g. dashboard `drafted` list) needing to also match `reviewed`. **Why not**: adds a status value nothing acts on; a flat column is simpler and matches the existing pattern.

### Decision: Authorize by Telegram username, register chat ID via `/start` — not a manually copied `TELEGRAM_CHAT_ID`
Telegram's Bot API can only push a message to a numeric chat ID it has already seen in an inbound update; it cannot message a username directly for a private chat. Rather than have Justin manually call `getUpdates` to find that ID and paste it into `.env`, the bot ships with `TELEGRAM_USERNAME=jagberg` as the only identity config. On `/start`, the bot checks `update.effective_user.username == config.TELEGRAM_USERNAME`; if it matches, it upserts the numeric `chat_id` into a new one-row-per-user `telegram_registrations` table (schema: `username TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, registered_at TEXT`). Every later inbound command re-checks the sender's username against `TELEGRAM_USERNAME` (cheap, avoids trusting a stale stored chat ID alone); outbound notifications read the stored `chat_id`.

**Alternatives considered:**
- **Manually captured `TELEGRAM_CHAT_ID` in `.env`** — Pros: no registration flow, no new table. Cons: requires Justin to send a throwaway message, call `getUpdates` (or use a helper bot) to find the numeric ID, then edit `.env` and restart — a manual step for information the bot can capture itself the first time Justin talks to it. **Why not**: `/start` registration removes a manual step for no added complexity — one small table, standard Telegram bot pattern.

### Decision: Automated tests follow the existing `tests/test_core.py` convention, not a new framework
That file is runnable, assert-based, no pytest/fixtures (`python tests/test_core.py`, ponytail-style — smallest thing that fails if the logic breaks). Telegram-specific logic (authorization check, notification-state diffing, reviewed-mark guard) is pure enough to unit test the same way: call the function directly with a fake `Update`/`Context` or plain dict, assert the outcome — no live Telegram network calls in the automated suite. Live-network scenarios (a real `/mark` round-trip, a real push arriving) stay manual/live verification, listed separately in tasks.md.

**Alternatives considered:**
- **pytest + `pytest-asyncio` + mocked `Application`** — Pros: more idiomatic for testing an async Telegram bot library. Cons: new test dependency and fixture machinery for a single-user personal tool that has deliberately stayed framework-free so far. **Why not**: the logic worth testing (authorization, dedup, guards) doesn't need the bot framework in the loop at all — test the plain functions it calls.

## Risks / Trade-offs

- **[Risk] Long-polling loop dies silently (network blip, unhandled exception) and Justin stops getting notified without knowing** → Mitigation: log polling errors loudly (same visibility pattern as existing Gemini/Gmail failure logging); `python-telegram-bot`'s `Application.run_polling` has built-in retry/backoff on transient errors.
- **[Risk] `/process <claim_id>` run concurrently with the scheduled pipeline tick on the same claim** → Mitigation: both paths go through the same `claim_forms.process_claim(claim_id)` function, which is already idempotent per the existing `matched`→`drafted` guard (only advances if not already drafted) — no new locking needed.
- **[Risk] Command typos (`/mark abc ...`, non-numeric claim_id, unknown claim_id)** → Mitigation: handler validates and replies with a plain error message in Telegram rather than throwing; no claim state changes on invalid input.
- **[Risk] Anyone who discovers the bot's username could message it** → Mitigation: every inbound update (including `/start`) is checked against `TELEGRAM_USERNAME=jagberg`; a non-matching sender is never registered and can never be granted a stored chat ID, per the `telegram-bot` spec's authorization requirement.
- **[Risk] Justin's Telegram username changes after registration** → Mitigation: re-running `/start` re-registers against the new username as long as it still matches config; if `TELEGRAM_USERNAME` itself needs updating, that's a one-line `.env` change, same as any other config value.

## Open Questions

- Exact wording/format for the pet-assignment Telegram command (e.g. `/pet <transaction_id> <Aari|Echo>`) — needs a real command name decided during tasks/implementation, proposal only fixes `/mark` and `/process`.
- Should `/process` also be allowed to run against a claim still at `pending_match` (i.e. also force an invoice-matching retry), or strictly `matched`→`drafted` only as scoped here? Currently scoped to the latter; revisit if Justin wants a "check now" for stuck `pending_match` claims too.
