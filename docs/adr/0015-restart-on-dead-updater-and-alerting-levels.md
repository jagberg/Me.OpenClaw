# ADR-0015: A dead Telegram updater restarts the process; ERROR means Justin must act

**Date**: 2026-07-25
**Status**: accepted
**Deciders**: Justin

## Context

`main.py`'s lifespan calls `await application.updater.start_polling()` and never awaits the resulting task. Nothing supervises it. When it dies, inbound Telegram messages stop arriving and **not one line is logged** — the exception has no owner. On 2026-07-25 the host slept 01:42→02:37 UTC and the bot went deaf; the only visible symptom was that Justin's taps did nothing (see ADR-0014).

Worse, liveness was genuinely unknowable from outside: probing Telegram's `getUpdates` returns 409 only if it happens to collide with an in-flight long poll, so a `200` proves nothing. An early diagnosis this session wrongly concluded "polling is dead" from exactly that signal and had to be retracted.

Two adjacent problems surfaced in the same investigation:

- **Alert plumbing already existed but was undiscoverable.** Rate-limited Telegram ops alerting (≤5 per rolling 24h, state in `ops_alerts` so a restart can't re-spam) was built on 2026-07-23 for Gmail auth death. It was never given an ADR, and `pipeline.py` / `db.py` cited it as "ADR-0011 ops-alerting" — ADR-0011 is *Petcover correlation is per Condition Thread* and contains no mention of auth or alerting. The reasoning was recorded, but in `openspec/changes/archive/2026-07-23-condition-thread-tracking/proposal.md`: *"when the Gmail token dies every Gmail-dependent step fails silently in logs — Telegram (which still works) says nothing."* This ADR gives that mechanism a home and corrects the citations.
- **Log levels carried no meaning.** A dropped Gmail socket (`IncompleteRead` on claim 5) emitted a full traceback at ERROR and read like a crisis, while messages being thrown away for want of a registered chat ID were only WARNING.

## Decision

**A dead updater takes the process down.** `pipeline._watchdog_telegram_polling`, called each tick from `run_once`, checks `telegram_bot.polling_alive()`. On `False` it sends a rate-limited Telegram alert and then `os.kill(os.getpid(), signal.SIGTERM)`. Compose already declares `restart: unless-stopped`, so the container returns with a fresh event loop, updater and Gmail service. Sending still works while receiving is dead — they are separate HTTP calls — so the alert genuinely reaches the phone before the process goes down.

**`polling_alive()` is the only honest liveness signal**, since it reads `updater.running` in-process. It is exposed at `GET /health` alongside `app_version`, queue depth and last-inbound time.

**The rate-limited alert mechanism is generalised**, extracted from `_ensure_gmail_auth` into `pipeline._alert_rate_limited(kind, message, cap)`. New alert kinds reuse it rather than adding a second alerting path.

**Log levels now mean something:**

- **ERROR** — Justin must do something. Code bugs (unhandled `callback_data`), handler failures, dead polling, a CSV that won't parse, output being dropped for want of a chat ID or token, a failed Drive backup.
- **WARNING** — self-healing or informational. Everything `pipeline._is_transient` matches (`IncompleteRead`, socket timeouts, HTTP 429/5xx) is logged without a traceback, because the next tick retries it unaided.
- **INFO** — routine progress.

**Deploy identity is baked at build time.** `scripts/deploy.ps1` sets `APP_VERSION` from the git short SHA + branch (+ `-dirty` when the tree isn't clean); the Dockerfile takes it as an `ARG` declared after the pip layer so bumping it doesn't bust the dependency cache. A build without the script leaves `unknown`, which is logged as a WARNING at startup rather than silently mistagging every row in `telegram_messages`.

## Alternatives Considered

### Alternative 1: restart the updater in-process (`await updater.stop()` then `start_polling()`)
- **Pros**: no downtime; no dropped pipeline tick.
- **Cons**: retries inside the same event loop and httpx pool that just failed; a half-restarted updater can fail the same silent way, and the failure mode is then recursive.
- **Why not**: the whole defect being fixed is *silent* failure. A recovery path that can fail silently is not a fix.

### Alternative 2: alert only, no auto-recovery
- **Pros**: least code; Justin stays in control.
- **Cons**: the bot stays deaf until he notices and acts — which on 2026-07-25 was several hours.
- **Why not**: the alert is the same one either way; restarting costs ~15s and removes the wait.

### Alternative 3: Docker healthcheck against `/health`
- **Pros**: declarative; no application code.
- **Cons**: Docker does not restart unhealthy containers on its own — it only marks them. That needs an external supervisor.
- **Why not**: it would report the problem without fixing it, which is what alert-only already does.

### Alternative 4: timestamp as the deploy version instead of a git SHA
- **Pros**: never `unknown`; needs no git.
- **Cons**: doesn't identify which commit produced a behaviour, which is the entire point of stamping the RL dataset.
- **Why not**: rejected by Justin for that reason.

## Consequences

### Positive
- A silent class of total failure becomes loud and self-correcting. Drilled live 2026-07-25: with polling reported dead the watchdog alerted, the alert was itself captured in `telegram_messages`, and the exit was requested.
- `GET /health` replaces guessing. `curl` answers version, polling state, queue depth.
- One alerting path, so a new failure kind inherits the rate limit and the restart-can't-re-spam property for free.
- Log volume drops and ERROR regains signal value.

### Negative
- A restart drops the in-flight pipeline tick. Acceptable: `run_once` is idempotent and re-runs on the next interval.
- The watchdog only fires as often as the pipeline tick (default 15 min), so up to one tick of deafness can pass unnoticed. Deliberate — no new job for a rare fault.
- `_is_transient` is a hand-maintained list. An unlisted transient error still logs at ERROR, which is the safe direction to be wrong in.

### Risks
- A fault that makes `polling_alive()` return `False` *persistently* becomes a restart loop. Bounded in practice by `restart: unless-stopped` backoff and by the alert cap, which goes quiet after 5 in 24h — but a loop would still be visible only in the container log. Not mitigated further; flagged here.
- `scripts/deploy.ps1` is the only path that sets `APP_VERSION` correctly. A hand-rolled `docker compose up --build` still works and is only detectable by the startup WARNING.

## Corrections to the record

- `pipeline.py` and `db.py` previously cited "ADR-0011 ops-alerting" for the rate-limited alert mechanism. ADR-0011 does not cover it; those citations now point here. The 2026-07-23 decision itself stands unchanged — only its pointer was wrong.
- ADR-0013 existed as a file but was missing from `docs/adr/README.md`; added.
