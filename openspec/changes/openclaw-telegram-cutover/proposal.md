## Why

`openclaw-gateway-core` (slice 1) put both runtimes in production and stopped one
step short of the only irreversible move: the gateway does not hold the Telegram
bot token. The Python app still polls, still renders every card, still dispatches
every tap. Everything the cutover needs is built and idle — `gateway_client.py`
merged with no caller, the plugin's five commands registered, the compose file
carrying an empty `TELEGRAM_BOT_TOKEN`.

This change supplies that token and everything that only becomes true afterwards.

The split was Justin's call on 2026-08-01, taken once the last spike closed. The
reason is that the cutover is atomic and the work before it was not: two
processes cannot long-poll one bot token, so there is no half-swapped state to
run in. Shrinking what has to be right on that day was worth carrying two change
directories.

## What Changes

- **BREAKING** — the gateway becomes the sole poller. `telegram_bot.py`'s updater
  goes behind a flag (default on), then off, then deleted. Rollback is one env var
  and a restart, ~30 seconds. The flag stays for **one week of real daily use**
  (Justin, 2026-08-01) before section 6 removes it — a week is what it takes for
  the failures only real use finds.
- **BREAKING** — free-chat handling moves from `agent.py`'s own tool loop to the
  gateway agent, whose reach is the MCP inventory slice 1 already registered.
- **BREAKING** — the 15-minute tick, Gmail ingest and the daily nudge move from
  APScheduler to `openclaw cron` against slice 1's `/internal` endpoints.
  `pipeline.run_once` itself is unchanged.
- Mutations become `propose_*` tools behind a confirm gate that commits in Python,
  never as a tool return value. The gate's location split by origin (ADR-0025):
  card taps commit through `/internal`, chat-initiated proposals commit in the MCP
  surface. Both call the same commit function.
- Outbound moves to `gateway_client`: notifications, cards, PDF alerts, nudges,
  `_append_result` edits, the 👍 ack. No direct Bot API call from Python.
- The per-chat Telegram command menu is claimed for Justin's chat, giving five
  entries instead of the gateway's 47 (13.1c — mechanism verified live
  2026-08-01, requirement false until the gateway drives the bot).
- Retired at the end, not the start: `agent.py`'s tool loop, `llm.chat()`,
  `scheduler.py`, `python-telegram-bot`, `apscheduler`.

## Capabilities

### Modified Capabilities

- `telegram-bot`: transport owner changes from `python-telegram-bot` polling to
  the gateway. Authorized-user identification, immediate ack, card rendering,
  callback dispatch, edited-message handling and reply-to-card context all
  re-anchor to gateway-delivered events; "no autonomous send" must additionally
  hold against the gateway's own DM-pairing behaviour.
- `conversational-agent`: the tool loop moves to a gateway agent.
  Bounded-LLM-usage, real-pet-list injection, confirm-before-commit and
  never-claim-mailbox-access become properties of agent configuration and the MCP
  inventory rather than of `agent.py`.
- `llm-backend`: `chat()` is no longer the app's own tool loop. Provider selection
  and rate-limit failover delegate to the gateway; the exhausted-daily-budget
  requirement changes because the gateway does not cover it — it has one
  `rate_limit` bucket and treats per-minute and per-day alike. Gemini-only vision
  stays in Python.
- `reminder-scheduling`: firing moves to gateway cron; the restart-safe
  requirement (a reminder whose time passed while down fires on startup) is
  restated against a scheduler the app no longer owns.
- `task-capture`: its confirm-before-commit requirement *is* the proposal gate,
  which split by origin, so its reference to `telegram_bot._execute_action` no
  longer names where the gate lives.

### Added Requirements to Existing Capabilities

Slice 1 created these three capabilities and put its own requirements in the
baseline. This change adds the rest:

- `openclaw-gateway-runtime` — four: the gateway owning channel transport, the app
  reaching Telegram through gateway actions, independent degradation, and cron.
- `claims-mcp-surface` — three: the proposal gate, the harness-enforced refusals
  it carries, and mutating-tool claim identity.
- `gmail-isolation-boundary` — four: mail reachable only through named sweeps,
  drafts-only re-verified structurally, sweeps idempotent under redelivery, and
  the vision-OCR cap holding against the new pressure sources.

## Impact

- **Retired**: `telegram_bot.py` updater loop, `agent.py` tool loop, `scheduler.py`,
  APScheduler, `python-telegram-bot`.
- **New**: the event-bridge plugin path (a tee for logging, plus conditional text
  claiming for pending flows), `propose_*` tools, gateway cron entries.
- **Retained unchanged**: `pipeline`, `claim_status`, `invoice_matching`,
  `claim_forms`, `vet_detection`, `claim_card`, `netbank_csv`, `gmail_client`,
  `db`, the FastAPI dashboard.
- **ADRs affected**: 0014 + 0015 (replay and dead-channel supervision under a new
  transport), 0025 (if the gate moves again), 0009 + 0017 (what `chat()`'s removal
  does to the daily-budget walk). 0003's no-push decision predates having a
  gateway and is worth revisiting.
- **Hard rules at risk.** "Never send email" is enforced by Python having no code
  path to violate it, and slice 1 kept the gateway credential-free and tool-poor.
  This change is where the agent gains mail-*touching* sweeps, so
  `gmail-isolation-boundary`'s remaining four requirements are the gating concern
  — not a design note.
- **The largest unverified assumption**: that a plugin can conditionally claim an
  inbound *text* message and stop it reaching the agent (task 0.10). Justin's
  decision on the pending condition/split flows (12.2) depends on it, and the
  product's docs evidence callback claiming only. Raise it before building
  section 12.
