## Why

The claim pipeline (`vet-claim-automation`) stalls at `matched` whenever a required field is missing — most commonly the condition text, sometimes the pet assignment for an ambiguous transaction — and today the only way to unblock it is for Justin to open the dashboard and fill a form. ADR-0003 deliberately deferred a push channel to keep v1 narrow; the `claim-form-automation` spec explicitly names Telegram as the intended channel for supplying condition text once that follow-up change happened. This is that change: a Telegram bot that tells Justin when a claim needs him, and lets him unblock it from his phone instead of a dashboard visit.

## What Changes

- Add a Telegram bot (long-polling, single authorized user — Justin's Telegram username, `jagberg`) that OpenClaw sends messages through and receives commands/replies from.
- Add a `/start` command: on first use, if the sending user's Telegram username matches the configured `jagberg`, the bot records the resulting numeric chat ID for future outbound notifications (Telegram's Bot API can only push messages to a chat ID it has already seen, not to a username directly).
- On each pipeline run, notify Justin in Telegram for claims newly stuck at `matched` needing input (missing condition text, or a vet-flagged transaction with no pet assigned) and for claims newly advanced to `drafted` (Gmail draft ready to review/send).
- Add a `/mark <claim_id> <condition text>` command (and equivalent for pet assignment) so Justin can supply the missing field by replying in Telegram — same effect as the existing dashboard form (`POST /claims/{id}/condition`), just a second entry point into the same update path.
- Add a `/mark <claim_id> reviewed` (or equivalent) command so Justin can confirm a `drafted` claim is correct from Telegram — records a reviewed timestamp only, does not send anything; sending stays a manual act on the Gmail draft itself.
- Add a `/process <claim_id>` command that runs the matched→drafted advance for one claim immediately, instead of waiting for the next scheduled pipeline tick (`VET_CLAIM_PIPELINE_INTERVAL_MINUTES`).
- The dashboard remains fully functional and unchanged — Telegram is an additional entry point, not a replacement.
- No change to send behavior: Gmail drafts are still never auto-sent from Telegram or anywhere else (existing `claim-form-automation` requirement stands).

## Capabilities

### New Capabilities
- `telegram-bot`: bot process/session management, authorized-chat enforcement, outbound notification delivery, inbound command parsing and dispatch.

### Modified Capabilities
- `claim-form-automation`: the condition-text and pet-assignment inputs, and the matched→drafted advance, gain a second entry point (Telegram command) alongside the existing dashboard form and scheduled pipeline tick. No change to the underlying fill/draft/send rules.

## Impact

- New dependency: a Telegram bot library (e.g. `python-telegram-bot`) and a bot token (BotFather-issued), stored as a new credential/config value.
- `app/openclaw/pipeline.py`: after each `run_once()`, diff claim states against what was last notified and push Telegram messages for newly-`matched`-needs-input and newly-`drafted` claims.
- `app/openclaw/main.py` / new `telegram_bot.py`: inbound command handling reuses the same update logic as the existing `/claims/{id}/condition` dashboard route (and the pet-assignment route) rather than duplicating it.
- New config: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_USERNAME` (`jagberg`) — no manually-copied chat ID; registration is self-service via `/start`.
- New DB table to hold the registered chat ID once `/start` succeeds, so outbound notifications know where to send.
- New DB field or table to track "last notified state" per claim, so the pipeline doesn't re-notify on every tick for a claim still sitting in the same blocked state.
- New DB field to record when Justin marks a `drafted` claim reviewed from Telegram.
- Automated tests (same runnable-assert style as `tests/test_core.py`) covering authorization, notification dedup, and the reviewed-mark guard — see `tasks.md` section 7.
