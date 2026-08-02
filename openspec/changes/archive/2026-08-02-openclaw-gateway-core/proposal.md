## Why

Five subsystems in this repo are hand-rolled reimplementations of what the OpenClaw gateway does natively: Telegram transport (`telegram_bot.py`, 1022 lines), the agent tool loop (`agent.py`, 692), provider-agnostic model routing (`llm.py` + `gemini.py`, 379), scheduling (`scheduler.py` + APScheduler), and inbound message durability (`message_log.py`, 202). That is 2312 of 8689 lines — 27% of the codebase — spent on plumbing, and four ADRs (0009, 0014, 0015, 0017) exist only to document its failure modes.

The repo has carried the name `OpenClaw` since inception but has never depended on it: no `openclaw.json`, no `package.json`, nothing in `requirements.txt`. This change makes the name true. OpenClaw becomes the **shell** — channel transport, agent sessions, model routing, cron — and the claims domain (`claim_status`, `invoice_matching`, `claim_forms`, `pipeline`, `vet_detection`, ~5300 lines) stays Python and becomes the thing the shell calls.

OpenClaw is explicitly *not* becoming the core in the sense of owning claims logic. It owns the edges. The domain rules that took a year of real emails to learn stay exactly where they are.

## Scope — slice 1 of two (narrowed 2026-08-02, task 8.14)

**This change is now slice 1: everything that is true while the Python app still owns Telegram.** Two containers deployed, the gateway holding no bot token and no channel bound, the plugin loaded with its commands registered, the MCP read tools answering, the preflight failing a bad deploy, the hermetic suite green. Telegram behaviour is bit-for-bit what it is today.

The cutover and everything downstream moved to **`openclaw-telegram-cutover`** — sections 3, 4, 5, 6, 9, 10, 12, task 13.1c, and the whole `telegram-bot` / `conversational-agent` / `llm-backend` / `reminder-scheduling` / `task-capture` deltas.

**The sections below are the original proposal and are deliberately not rewritten.** Justin's call (2026-08-01) was two slices, taken after the last spike closed; the scope narrowed, the reasoning did not change, and rewriting this document to pretend it was always scoped this way would delete the trail that explains why the split exists. Bullets that moved are marked **→ slice 2**. Task 8.11 holds the boundary rule and the per-requirement assignment.

## What Changes

- **→ slice 2** — **BREAKING** — the gateway owns the Telegram bot token. Two processes cannot long-poll one token, so `telegram_bot.py`'s updater is retired. Inbound messages and callback taps arrive via the gateway; outbound notifications go out through `openclaw message send` / `editMessage`.
- **→ slice 2** — **BREAKING** — free-chat handling moves from `agent.py`'s own tool loop to an OpenClaw agent whose tool inventory is an explicit allowlist. ADR-0016's boundary (three named sweeps, no mailbox browsing, no code/spec access) is re-expressed as gateway tool configuration instead of Python prompt discipline.
- **→ slice 2** — **BREAKING** — the 15-minute tick and the daily nudge move from APScheduler to `openclaw cron`. `pipeline.run_once` itself is unchanged.
- The claims domain is exposed as a Python MCP server registered via `openclaw mcp set`. Reads stay direct — **shipped in this slice**. Mutations stay proposals-confirmed-by-tap — **→ slice 2**.
- Gmail is walled off: OAuth token, `gmail_client`, and every Gmail query stay inside the Python process. The gateway agent gets no filesystem, Bash, or browser tool that can reach `app/data/`, and no Gmail scope of its own. The drafts-only hard rule stays enforced by the absence of `send()` in `gmail_client`, not by prompt.
- **→ slice 2** — Telegram UI is preserved feature-for-feature: Pillow claim cards as photos, inline keyboards, tap-to-resolve callback tokens, 👍 ack, result-appending via message edit.
- `telegram_messages` and the at-least-once replay queue are **retained** (the logging tee shipped in this slice; the gateway carrying the traffic it tees is slice 2), not replaced. The gateway's own logging does not supersede them: the raw-payload + `app_version` dataset is a stated project asset.
- **→ slice 2** — Model routing delegates to the gateway's fallback chain, **except** daily-token-budget exhaustion, which the gateway does not treat as a failover trigger (it switches on rate-limit responses only). ADR-0017's per-model daily budget walk must either survive in `llm.py` or be proven redundant against real Groq 429 bodies.
- Deploy becomes two runtimes: the existing Python container plus a Node 24 gateway daemon.

## Capabilities

### New Capabilities

- `openclaw-gateway-runtime`: the gateway as the process that owns channel transport, agent sessions, model resolution and cron; how the Python app registers with it; the two-runtime deploy and version-stamping shape; what happens to each half when the other is down.
- `claims-mcp-surface`: the claims domain exposed as MCP tools — the exact tool inventory, which tools read versus propose, and the rule that a capability absent from the inventory is unreachable rather than prompt-discouraged.
- `gmail-isolation-boundary`: Gmail credentials, scopes and queries are reachable only by the Python process; the gateway agent cannot read the mailbox, reach the token, or acquire a send path by any tool it holds.

### Modified Capabilities — ALL MOVED TO SLICE 2

Every one of these describes behaviour that only exists after the swap, so none of them could be synced into the current-state baseline by this slice.

- `telegram-bot`: transport owner changes from `python-telegram-bot` polling to the gateway. Requirements for authorized-user identification, immediate ack, card rendering, callback dispatch, edited-message handling and reply-to-card context all need re-anchoring to gateway-delivered events; "no autonomous send" must additionally hold against the gateway's own DM-pairing behaviour.
- `conversational-agent`: the tool loop moves to a gateway agent. Bounded-LLM-usage, real-pet-list injection, confirm-before-commit, and never-claim-mailbox-access become properties of agent configuration and the MCP inventory.
- `llm-backend`: `chat()` is no longer the app's own tool loop. Provider selection and rate-limit failover delegate to the gateway; the exhausted-daily-budget requirement changes because the gateway does not cover it. Gemini-only vision stays in Python.
- `reminder-scheduling`: firing moves to gateway cron; the restart-safe requirement (a reminder whose time passed while down fires on startup) must be restated against a scheduler the app no longer owns.

### Added after the proposal was written — MOVED TO SLICE 2

- **`task-capture`** (2026-08-01). Its confirm-before-commit requirement *is* the proposal gate,
  and the gate's location split by origin (ADR-0025), so the capability was affected and had no
  delta. Added while the reasoning was fresh rather than rediscovered months later during the
  archive sync. Recorded here because the eval on 2026-08-02 found it declared only inside
  `tasks.md` — a whole capability that this document, the one that states scope, did not mention.

## Impact

- **Retired — all of it slice 2**: `telegram_bot.py` updater loop, `agent.py` tool loop, `scheduler.py`, APScheduler dependency, `python-telegram-bot` dependency.
- **New**: Python MCP server module, a gateway bridge module (callback → `record_inbound` → existing handler), `openclaw.json` config, Node 24 in the deploy.
- **Retained unchanged**: `pipeline`, `claim_status`, `invoice_matching`, `claim_forms`, `vet_detection`, `claim_card`, `netbank_csv`, `gmail_client`, `db`, the FastAPI dashboard.
- **ADRs affected**: 0002 (stack), 0009 + 0017 (LLM backend and daily-budget fallback), 0014 + 0015 (message log, dead-updater restart), 0016 (agent tool surface). 0003 is worth revisiting — the no-push decision predates having a gateway.
- **Hard rules at risk, and why they are the gating concern**: "never send email" and "never store bank credentials" are currently enforced by Python having no code path to violate them. Introducing a general-purpose agent runtime with browser, file and shell tools available in principle is the one part of this change that can regress a hard rule, which is why `gmail-isolation-boundary` is a first-class capability rather than a design note.
- **Unverified at proposal time** (each becomes a spike task): whether inline buttons attach to photo messages via `message send`; whether editing a photo *caption* is reachable through `editMessage`; whether Groq's daily-budget 429 body is distinguishable to the gateway; whether the gateway can be pinned to a single authorized Telegram username as strictly as today's check.
