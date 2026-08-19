# OpenClaw retrospective — 2026-08-13

A deep look at the setup: architecture, documentation, where scope was deliberately
narrowed ("clipped wings"), and what's worth fixing. Actionable items are tracked in
[#11](https://github.com/jagberg/Me.OpenClaw/issues/11); this doc is the narrative and
the diagrams behind them.

## Project timeline

26 days, 305 commits, five eras. Density triples the moment the gateway swap starts.

```mermaid
timeline
    title OpenClaw build eras (2026-07-17 -> 2026-08-11)
    2026-07-17/18 : Genesis — claims automation + personal assistant openspec, ADRs/README/CLAUDE.md written same week
    2026-07-18/24 : Core claims build-out — Telegram bot, condition entry, per-item split, Drive backup
    2026-07-25 : Single dense day (25 commits) — durable message log, dead-updater watchdog, read-only DB rule, agent tool-surface widening
    2026-07-27/31 : Correctness hardening — one-invoice-many-pets, status/label split, claim status becomes a declared state machine
    2026-08-01/02 : Gateway swap begins — workspace identity files, tool allowlist (22.8k -> 3.9k tokens/turn), proposal-gate redesign
    2026-08-03/06 : Cutover stabilization — Telegram transport fixes, Groq investigation, settlement reconciliation
    2026-08-07/11 : Late fixes — Groq UA-ban correction, cross-provider fallback, CSV-upload-via-Telegram, invoice-auto-send exception
```

## Architecture: the two-runtime boundary

The core structural decision (ADR-0024): the gateway is the shell, Python keeps the
domain. No Gmail credential and no database access cross into the gateway — enforced by
container boundary, not config.

```mermaid
flowchart LR
    subgraph Gateway["gateway container — no Gmail, no DB"]
        TG[Telegram channel]
        Agent[Chat agent<br/>Gemini 2.5 Flash]
        Plugin[claims plugin<br/>13 commands]
    end
    subgraph App["app container — owns the domain"]
        Pipeline[pipeline.py<br/>15-min tick]
        Claims[vet_detection -> invoice_matching<br/>-> claim_forms -> claim_status]
        DB[(SQLite<br/>WAL, host read-only)]
        Gmail[Gmail API<br/>read + draft only]
    end

    TG <--> Plugin
    Agent <--> Plugin
    Plugin -- "/internal, /mcp<br/>shared secret" --> Pipeline
    Pipeline --> Claims
    Claims --> DB
    Claims --> Gmail
    Claims -. "notify" .-> TG

    style Gateway fill:#2d1b4e,stroke:#8b5cf6,color:#fff
    style App fill:#0f3d2e,stroke:#10b981,color:#fff
```

This buys a real guarantee (`send()` doesn't exist anywhere in the gateway's reach, so it
structurally cannot happen) at a real, continuous cost: two `.env` files that have
drifted at least three times, a plugin-hook bug that silently never fired
(ADR-0029), and the stale-socket incident below — all failure modes that only exist
*because* the boundary is a process boundary, not a module boundary.

## Where scope was deliberately narrowed

| Constraint | ADR | Why | Verdict |
|---|---|---|---|
| Chat agent can't read/search Gmail | 0016 | Agent had **fabricated** having checked mail it couldn't see | ✅ right call — narrowed later to 3 named idempotent sweeps, not re-litigated |
| No filesystem/shell/browser tools, no dynamic registration | 0023 | Stock runtime ships 32 tools/32k chars; cut to 1 tool/304 chars | ✅ right call, ⚠️ security + token-budget coupled with **no error if someone adds a tool** (ADR says so itself) |
| Model output can never be the commit | 0025 / 0027 | Model proposed assigning a claim to two pets **despite an explicit prompt rule** | ✅ strongest decision in the project — "the prompt rule lost on live data" |
| No dynamic pet-list memorization | USER.md | Model invented pet names when left to guess | ✅ right call, minor |
| One named exception to never-send (vet invoice-request) | 0030 | Justin's explicit override, scoped to one call site | ✅ well-guarded — rejected a `send: bool` flag specifically to prevent scope creep |
| Domain logic never enters the gateway | 0024 | gmail-isolation-boundary enforced by container, not config | ✅ right call, 🔍 ongoing tax (see architecture above) |
| Telegram's full generic command menu (`/exec`, `/elevated`, `/approve`, 50+ commands) ships alongside the 13 domain commands | — none | Never audited | 🔍 not yet checked whether ADR-0023's own principle was applied one layer too low |

## Capability map: what could it be leveraging more

The honest answer to "have I clipped its wings": **once, by accident, not by any of the
decisions above.** Everything else in the table above was clipped on purpose, with a
reason written down. This one wasn't — it's scope that slipped through a rewrite and
nothing ever caught it.

```mermaid
flowchart TB
    subgraph Live["live today — mcp_server.TOOLS, 12 entries"]
        direction LR
        L1[query_claims]
        L2[pending_actions]
        L3[claim_detail / claim_history]
        L4[submissions_awaiting_reply]
        L5[list_tasks — read only]
        L6["propose_* — mark_sent, set_condition,<br/>assign_pet, mark_resolved, split_between_pets"]
    end

    subgraph Regressed["built + tested, NOT reachable — planned in the gateway design doc, dropped in the rewrite"]
        direction LR
        R1[reconcile_sent_invoice_requests]
        R2[rematch_claims]
        R3[poll_petcover_now]
        R4["propose_create_task /<br/>propose_close_task"]
    end

    subgraph Deferred["available in the product, deliberately shelved — door left open"]
        direction LR
        D1["Additional channels<br/>WhatsApp / Signal"]
        D2["~60 generic gateway commands<br/>/exec /subagents /acp /steer ..."]
    end

    subgraph Rejected["considered and rejected — won't revisit without new evidence"]
        direction LR
        X1[Filesystem / shell / browser tools]
        X2[Mailbox search or read]
        X3[Model-issued commits]
        X4[ClawHub skills]
        X5[Reading its own code / docs / specs]
    end

    style Live fill:#0f3d2e,stroke:#10b981,color:#fff
    style Regressed fill:#4a3510,stroke:#f59e0b,color:#fff
    style Deferred fill:#1e3a5f,stroke:#3b82f6,color:#fff
    style Rejected fill:#3f1d1d,stroke:#ef4444,color:#fff
```

**The regression, in one sentence:** ADR-0016 (2026-07-25) explicitly carved out three
Gmail sweeps as safe for the chat agent to run on request — *"check if Petcover
replied," "recheck this claim's matching," "reconcile what I've sent."* The gateway
migration's own design doc (`openclaw-gateway-core/design.md`, D4) restated this as a
requirement, word for word: *"Mail is reachable only through the three existing named
sweeps."* Then `mcp_server.py` — the only tool list the live agent actually sees — was
built with 12 tools and never one of the three. No comment, no ADR amendment, no test
against the live surface (the tests that exist check `agent.py`'s schema, which belongs
to the Telegram poller that's been switched off since the cutover — they've been
testing dead code). Same story for `propose_create_task`/`propose_close_task`, minus
the paper trail saying they were ever meant to ship.

| Bucket | What "leveraging it more" looks like | Effort |
|---|---|---|
| 🟡 Regressed — 3 sweeps | Ask the bot to check Petcover, recheck a match, or reconcile sent requests, and it actually can again | Low — 3 tool entries + existing `_impls`, no new logic |
| 🟡 Regressed — task tools | Create/close a household task from chat, not just list them | Low, but needs a scope call first: `AGENTS.md` currently says "claims and nothing else" |
| 🔵 Deferred — command audit | Confirm the ~60 stock gateway commands are actually inert from this chat, or close the hole | Low — one audit pass, closes issue #11 item 2 |
| 🔵 Deferred — channels | Same assistant reachable on WhatsApp/Signal, if ever wanted | Medium — re-opens the single-authorized-user question |

Everything in the ⬛ rejected bucket should **stay** rejected — each one is backed by a
live incident (a fabricated mail-check, a model overriding an explicit prompt rule), not
caution for its own sake. Widening any of those is the wrong kind of "leveraging it
more."

## Findings filed as follow-ups

Full detail in [issue #11](https://github.com/jagberg/Me.OpenClaw/issues/11).

```mermaid
quadrantChart
    title Follow-ups by effort vs impact
    x-axis Low effort --> High effort
    y-axis Low impact --> High impact
    quadrant-1 Do next
    quadrant-2 Worth scheduling
    quadrant-3 Nice to have
    quadrant-4 Reconsider
    "Checkout-drift check in deploy.ps1": [0.15, 0.55]
    "Name the ADR-0030 pattern in CLAUDE.md table": [0.1, 0.3]
    "Audit Telegram's generic command surface": [0.25, 0.6]
    "Tool-allowlist / token-budget coupling guard": [0.55, 0.65]
    "Stale-socket silent-failure class (mitigated by watchdog)": [0.4, 0.9]
```

## What's genuinely working

- **Single-writer-plus-guard** is the load-bearing pattern of the whole codebase
  (`claim_status.apply_event`, and five more instances catalogued in
  `app/openclaw/CLAUDE.md`). Claim status used to be 9 write sites across 4 modules,
  6 silently dropping the event — now one function, one test that fails on a violation.
- **ADRs correct themselves in place** rather than getting quietly rewritten — 0028
  reverses 0009's "Groq is unreachable" claim, the claimable-subtotal ADR corrects its
  own "35% is fiction" line after Justin confirmed it's a real policy term. Rare for
  design docs to admit they were wrong.
- **BACKLOG.md** is a working ledger, not a wishlist — dated resolutions, explicit
  reason for existing ("without this file these items would disappear").
- **The gateway-workspace persona files** are honest about their own limits — `SOUL.md`
  opens with "nothing here is a safety control ... they lost once as prompt rules."
  Most projects load up a system prompt with rules and never admit which ones are
  decorative.

## Process note

This session found the local checkout **8 commits behind `origin/master`**, missing
ADR-0031, ADR-0032, and two openspec changes — silent drift, caught only because this
retrospective happened to run `git log --all`. Tracked as item 4 in #11.
