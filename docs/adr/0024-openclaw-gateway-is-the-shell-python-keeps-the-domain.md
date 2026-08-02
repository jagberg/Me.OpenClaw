# ADR 0024: The OpenClaw gateway is the shell; Python keeps the domain

- Status: accepted
- Date: 2026-08-01
- Supersedes the single-runtime assumption in ADR-0002
- Related: ADR-0023 (tool allowlist), ADR-0025 (proposal gate), ADR-0014/0015 (message log, supervision)

## Context

This repository has been named `OpenClaw` since inception and never depended on
OpenClaw the product. Verified 2026-08-01: no `openclaw.json`, no
`package.json`, and `requirements.txt` holds only FastAPI, APScheduler,
`python-telegram-bot`, the Google clients, `pypdf` and Pillow. The name was a
collision, not an integration.

What grew in its place is a hand-rolled equivalent of a gateway's edges — 2,312
of 8,689 lines across `telegram_bot.py` (1,022), `agent.py` (692), `llm.py` plus
`gemini.py` (379), `message_log.py` (202) and `scheduler.py` (17). Four ADRs
(0009, 0014, 0015, 0017) exist for no purpose other than recording how that
plumbing fails and what was done about it.

OpenClaw is a local-first daemon owning exactly those edges: channel transport
across 25+ platforms, agent sessions with per-sender isolation, model resolution
with fallback chains, cron, and a plugin/MCP extension surface.

Justin's constraint, stated at the outset and treated as binding: **the solution
fits the OpenClaw architecture; OpenClaw is not bent to fit the solution.** Keep
what this repo built only where losing it costs something real, with the burden
of proof on the bespoke thing.

## Decision

Four components, with one direction of dependency at each seam.

1. **The gateway**, in Docker. Sole holder of the Telegram bot token, sole
   poller, owner of the chat agent loop, model resolution and cron.
2. **An in-gateway plugin** (`openclaw-claims`). Registers the app's slash
   commands and forwards each to the app. Carries no claim logic.
3. **The Python app.** Claims domain, Gmail, SQLite, the dashboard, and an
   internal HTTP surface the plugin and cron call.
4. **The agent**, reaching the domain through an enumerated MCP tool inventory.

The deterministic path — the one that matters most — is:

```
button (action.type "command", e.g. "/mark 7 sent")
  -> core native command path
  -> plugin command handler
  -> HTTP to the app's /internal
  -> existing claim logic
```

No model is invoked at any point in it.

The domain is not ported. `pipeline`, `claim_status`, `invoice_matching`,
`claim_forms`, `vet_detection`, `claim_card`, `netbank_csv`, `gmail_client` and
`db` are untouched — roughly 5,300 lines derived from a year of real emails,
PDFs, CSVs and corrections.

### Alternatives rejected

**Port the domain to TypeScript as a plugin.** Discards the one asset that is
genuinely hard to rebuild, and the stack is Python-shaped: `pypdf` for invoice
segmentation, Pillow for card rendering, `google-api-python-client`, SQLite.

**Run the claims service as a gateway subprocess.** Ties the domain's lifecycle
to the gateway's. The pipeline must keep advancing claim state while the gateway
is down; a claim does not stop needing to be matched because a chat channel is
offline.

**Forward every inbound message into Python** (the original D2). This is using
an agent gateway as a dumb pipe and discards the reason to adopt one. Rejected
by the guiding principle before any measurement was needed.

## Consequences

**The cutover is atomic, not gradual.** Telegram answers a second `getUpdates`
caller with `409 Conflict`, so exactly one process may poll a token. There is no
window in which both transports run.

**Two runtimes means two configurations**, and `.env` already diverges between
the main checkout and the deploy worktree. ADR-0002's single-service simplicity
is genuinely lost; see its addendum.

**The gateway must never touch the database file.** A read-write open from the
wrong side once deleted the WAL sidecars and took out the scheduler and Telegram
together until a container restart (2026-07-25).

**The hard rules are enforced by absence.** "Never send email" holds because
`send()` appears nowhere in `app/openclaw/`, while the OAuth token *does* carry
`gmail.compose`. Introducing a runtime that ships shell, file and browser tools
is the single part of this change capable of regressing a hard rule — hence
ADR-0023.

**What was kept, and why, each on its own evidence** — the guiding principle
producing keeps as well as replacements:

| Kept | Basis |
|---|---|
| `telegram_messages` | Owner decision: the training-dataset job is real and intended. |
| Pillow claim cards | Compared live against a native table; the card holds month subtotals, status pills and a layout that does not truncate on a phone. |
| `tasks.py` | Provenance — `source_message_id` back to the Gmail message, `outcome_at`, rows beside the claims. |
| `llm.py` daily walk (extraction) | The gateway has one `rate_limit` bucket and treats it as transient; ADR-0017's per-day distinction does not survive there. |

| Replaced | Basis |
|---|---|
| `reminders.py` | `cron --at` plus startup catch-up covers the `misfire_grace_time=None` behaviour its own comment calls out. |
| `telegram_bot.py` transport | The gateway owns channel transport; this was the hand-rolled equivalent. |

**Limitations accepted, recorded here rather than discovered:**

- The platform fails silently in eight measured ways. Success responses and
  inspection output are not evidence of anything; the deploy preflight exists
  because of this, not as belt-and-braces.
- A `command` button is deterministic only while its command is registered. An
  unregistered one is not an error — it reaches the agent as a chat turn
  (measured live). The determinism above rests on a deploy-time assertion.
- Rendered cards must reach the gateway inside its own allowlisted media root,
  which forces a narrow shared volume between the two containers. This became
  mandatory when the Pillow cards were kept.
