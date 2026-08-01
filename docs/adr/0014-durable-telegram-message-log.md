# ADR-0014: One durable table records every Telegram message, and doubles as the replay queue

**Date**: 2026-07-25
**Status**: accepted
**Deciders**: Justin

## Context

On 2026-07-25 Justin tapped several action buttons in Telegram (mark-sent, set-condition). Nothing happened, and **nothing in the system could say why**. Verified against a copy of the live DB: no `claim_status_events` rows for that date, #2/#13 still `drafted`, #6/#7/#12 still without `condition_text`. The container log held three inbound command lines and no record of a single tap, so "the callback never arrived" and "the handler failed silently" were indistinguishable.

Three separate gaps produced that:

- `on_callback` logged nothing, and no PTB error handler was registered, so a handler exception went into PTB's own logger with no user feedback (fixed same day in `3955826`).
- Nothing durable recorded inbound updates at all. `llm_calls` stores only `purpose/success/latency`; `docker compose up` destroys the prior container's logs. The only reason the earlier 2026-07-25 chat review was possible is that Justin still had the thread on his phone.
- The host slept 01:42→02:37 UTC. Telegram retains unconfirmed updates ~24h, so a suspend alone loses nothing — but an update that PTB has already fetched and is mid-handling when the process dies is gone.

Justin also wanted the message history as a reinforcement-learning dataset, tagged to a deploy version so behaviour can be attributed to a point in time.

## Decision

A single table, `telegram_messages`, records every message in and out, stamped with `config.APP_VERSION`. It serves three purposes at once: the RL dataset (raw `update.to_dict()` payload), the audit trail, and the replay queue (`processed_at IS NULL` = still owed).

Capture happens at two seams, both in `telegram_bot.py`:

- `LoggedApplication.process_update` writes the arrival row **before** calling `super()`, so a crash mid-handler leaves the row unprocessed and `message_log.replay_pending` re-runs it at startup.
- `LoggedBot(ExtBot)` overrides the five public senders (`send_message`, `send_photo`, `send_document`, `edit_message_text`, `edit_message_caption`).

`update_id` is `UNIQUE` and inserts use `INSERT OR IGNORE`, so replay and Telegram redelivery dedupe for free.

**Retention is asymmetric on purpose.** Justin's instruction was "after 24 hours the queue can be purged as it won't be relevant anymore". Implemented as: after `MESSAGE_QUEUE_TTL_HOURS` a row stops being replay-eligible (`processed_at` stamped, `error` = abandoned) but **is never deleted** — deleting it would destroy the dataset the table exists for. The queue expires; the log doesn't.

## Alternatives Considered

### Alternative 1: separate queue table, separate log table
- **Pros**: each table has one job; the queue stays small.
- **Cons**: every column duplicated; two writes per update; the audit question ("did my tap arrive?") needs a join or a guess about which table to trust.
- **Why not**: the queue *is* a view over the log (`processed_at IS NULL`). One table, one write.

### Alternative 2: hard-delete everything after 24h (the literal reading of "purge")
- **Pros**: simplest; bounded growth; exactly what was asked.
- **Cons**: no reinforcement-learning dataset survives — which is the reason the table was requested.
- **Why not**: the two requirements conflict, and the dataset is the more valuable one. Volume is a few thousand rows a year, so bounded growth buys nothing. Recorded here because it is a deliberate reinterpretation of the instruction, not an oversight.

### Alternative 3: log inside handlers, or in a `TypeHandler(Update)` at group -2
- **Pros**: no PTB subclassing.
- **Cons**: a handler-level hook can't mark completion (there is no post-handler hook), and per-handler logging means every new handler must remember to do it.
- **Why not**: `process_update` is the one seam every update passes through exactly once, and it brackets handler execution, which is what "processed" has to mean.

### Alternative 4: override `Bot._post` for outbound
- **Pros**: one method catches every API call including future ones.
- **Cons**: private API; a PTB upgrade can rename or restructure it silently.
- **Why not**: the five public senders cover everything actually used, and `reply_text` funnels through `bot.send_message` anyway.

## Consequences

### Positive
- "Did my tap register?" is a query, not a forensic exercise. Verified live the same day: tap `sent:2` logged, handler run, outbound reply logged, claim #2 → `sent`.
- Crash- and suspend-resilience for updates already fetched but not finished.
- The RL dataset carries the deploy identity, so a behaviour change can be attributed to a commit (`GET /messages.jsonl`, ADR-0015 covers the version stamping mechanics).
- `GET /health` can report queue depth and last-inbound time.

### Negative
- Replay is **at-least-once**, so every mutation it can re-trigger must be idempotent. `mark_sent` and `dismiss_mismatch` already were (verified against a copy of the live DB); `confirm_resolved` was **not** — two calls wrote two audit events for one decision, and confirming a claim with nothing outstanding invented an event from nothing. Fixed the same day; it now refuses both cases. **Any future tap-driven mutation inherits this obligation.**
- A DB write on the hot path of every message. Negligible at single-user volume.
- Anything that constructs a plain `telegram.Bot` bypasses the outbound log. The three `*_sync` senders were doing exactly that (so every proactive notification would have gone unrecorded) and now use `LoggedBot`.

### Risks
- **Known gap, accepted**: updates Telegram has already confirmed that sit in PTB's in-memory queue when the process dies never reach `process_update`, so they are neither logged nor replayable. The window is milliseconds; closing it means reaching into the updater's fetch loop, which is not worth the coupling.
- The payload is a verbatim Telegram update, so anything Justin types is stored in plaintext in the DB. The DB is already gitignored, local, and single-user; `/messages.jsonl` is bound to `127.0.0.1` like the rest of the dashboard. No bot token or media bytes are ever written (photos/documents are recorded as `<N bytes>`).

## Amendment (2026-07-27) — replay runs *after* startup completes, not as part of it

The decision stands; one mechanical detail in it was wrong. This ADR records that `message_log.replay_pending` "re-runs it at startup", and the implementation awaited it inside the FastAPI lifespan, immediately after `updater.start_polling()`.

That ordering is still right — replaying after polling starts is what makes Telegram's own redelivery dedupe by `update_id` — but **awaiting** it is not. Replaying a queued *chat* message runs a full LLM turn: on 2026-07-27 it held HTTP startup for ~30 seconds, so uvicorn had not bound, `/health` refused the connection, and `scripts/deploy.ps1` reported a failed deploy for a container that was fine. A provider that retries (or a queue with several chat messages in it) would hold it for minutes, with the dashboard down the whole time.

Replay is now a background task created after polling starts, with a module-level reference (an unreferenced task can be garbage-collected mid-replay) and a done-callback that logs failure at ERROR — a fire-and-forget task's death is otherwise silent, the same trap as the updater in ADR-0015.

**Trade-off accepted:** the app can serve HTTP and accept *new* updates while older queued ones are still being replayed, so the two can interleave. That is safe under the at-least-once contract this ADR already imposes (every replayable mutation must be idempotent) and it was already possible anyway — polling starts before replay either way.

**Caveat, unchanged by this:** a row that carries an `error` is refused by `mark_processed`, so a genuinely crashed update stays in the queue and replays on **every** restart until `MESSAGE_QUEUE_TTL_HOURS` expires it. Update `176928775` (the edited message that crashed the text handler) did exactly that across two deploys on 2026-07-27 — one LLM turn and one Telegram message each time. Intended, but worth knowing before wondering why a restart re-answers an old question.

---

## Amendment (2026-08-01) — retained deliberately under the gateway

The gateway keeps its own message records, so this table was put to the
fit-the-architecture audit and **kept** — decided by Justin, not defaulted.

The deciding reason is the one the gateway cannot cover: `telegram_messages` is
an RL training dataset keyed to *this app's* version. A general-purpose gateway
has no reason to store raw payloads stamped with `app_version`. The audit-trail
and replay jobs may well be duplicated by the gateway; that duplication is
accepted rather than investigated, because the dataset job alone justifies the
table.

Unchanged under the new transport: `record_inbound` still writes **before** the
handler runs, so a crash mid-handler leaves the row unprocessed and replayable,
and every outbound path writes too. Version stamping gains a wrinkle — two
runtimes means two versions, and the gateway's must be recorded separately
rather than displacing `app_version`, or the dataset lies about which code
produced a row.
