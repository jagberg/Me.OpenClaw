## Context

Slice 1 (`openclaw-gateway-core`, archived 2026-08-02) settled the architecture and
deployed both runtimes. This document does **not** restate it. Its decisions
D1–D12 and ADR-0023/0024/0025 govern this change unchanged, and a copied decision
trail diverges — when the two disagree, slice 1's archived `design.md` is the
authority and the disagreement is a defect here.

Read before working on this change:

| Where | What it settles |
|---|---|
| slice 1 `design.md` **D12** | The consolidated architecture. Supersedes D2, D9, D10, D11. |
| slice 1 `design.md` **D3** | The proposal gate splits by origin. Also ADR-0025. |
| slice 1 `design.md` **D4** | Gmail walled off by capability, not by instruction. |
| slice 1 `design.md` **D5** | `chat()` deleted; `extract()` and the daily-budget walk stay. |
| slice 1 `design.md` **D7** | `telegram_messages` is a hard keep. Justin's call. |
| **ADR-0024** | Gateway as the shell, Python as the domain. |
| **ADR-0025** | Where the proposal gate lives. |
| **ADR-0023** | The agent tool allowlist — security and token budget at once. |
| `docs/gateway-deploy.md` | Deploy steps, and **"What CANNOT be asserted, and why"**. |

What is new here is not architecture. It is the one irreversible operation the
architecture implies, plus the behaviour that only exists on its far side.

## Goals / Non-Goals

**Goals.** One poller. The Telegram UI feature-for-feature: Pillow cards as
photos, inline keyboards, tap-to-resolve, 👍 ack, result-appending by edit. The
proposal gate enforced on both entry points, tested independently on each.
Scheduled work on gateway cron with the single-fire guarantee APScheduler gave
for free. `telegram_messages` complete, with no field lost.

**Non-Goals.** Re-opening D12. Moving domain logic. Improving the Telegram UI
during the swap — a change to behaviour mid-cutover makes a real regression
indistinguishable from an intended one, which is exactly why the 👍 ack is kept
for now (12.3) and its replacement is in `openspec/BACKLOG.md`.

## Decisions

### C1 — The cutover is one deploy step, guarded by a preflight that cannot be satisfied by both runtimes

Supplying `TELEGRAM_BOT_TOKEN` to the gateway and clearing the app's updater flag
happen together. `scripts/gateway_preflight.py`'s `check_exactly_one_poller`
already fails on both polling (Telegram answers the second with `409 Conflict`)
and on neither (every message silently dropped). Slice 1 shipped that check
running in the "app polls" direction; this change flips its expected direction.

Rationale: the failure this guards is not a crash. Two pollers means alternating
delivery — half of Justin's messages reaching a runtime that no longer handles
them — and neither runtime logs anything unusual.

### C2 — The updater flag survives one week of real use, then dies

Justin, 2026-08-01. Section 6's deletions do not start until section 4 has run one
full claim lifecycle on real data. The rollback is one env var and a restart.

Rationale, stated as a cost: carrying dead code for a week is the price of being
able to undo the swap in 30 seconds during the only week when the failures that
matter — a caption that will not edit, buttons that will not attach to a card, a
tap that quietly reached the LLM — are still being discovered.

### C3 — 0.10 gates section 12, and is raised before anything in it is built

Whether a plugin can conditionally claim an inbound *text* message is the largest
unverified assumption in this change. Justin's decision on the pending
condition/split flows (12.2) rests on it, and the product's docs evidence callback
claiming only.

This is a direct application of slice 1's most expensive lesson, recorded in root
`CLAUDE.md`: five architectural conclusions reasoned from documentation prose plus
this codebase's shape, all five wrong, all five demolished in minutes by the
product's actual behaviour. So 0.10 is a probe against the running gateway, not a
reading of the SDK.

If it fails, 12.2's decision is unavailable and Justin must re-choose between the
agent-tool and fully-conversational options — **both of which put a model between
his typing and `condition_text`**, the field the hard rules forbid inferring. That
is a decision for him, not a fallback to pick silently.

## Risks / Trade-offs

- **The gateway treats a daily token exhaustion as transient.** It has one
  `rate_limit` bucket (`model-fallback-*.js:46`) and retries the same model.
  ADR-0017's per-model daily walk must survive in `llm.py` or be proven redundant
  against a real Groq 429 body. Measured on slice 1's first deploy:
  `Limit 100000, Used 96708` — the starvation arrived faster than the task
  predicted, and from the direction it named (task 17.6).
- **Two entry points can reach the commit.** A single test of "the" gate no longer
  covers the system (10.5 and 3.2 must both exist).
- **`telegram_messages` completeness now depends on a plugin.** Slice 1's tee
  (`/internal/telegram/event`) is the only thing keeping the dataset whole once the
  app stops seeing inbound messages. It returns 500 rather than swallowing, for
  that reason.
- **A `command` button is deterministic only while its command is registered.** An
  unregistered one is not an error — it reaches the agent as a chat turn and spends
  tokens, measured live three times. The preflight's registration check is a
  deploy-time assertion propping up a claim about the mechanism.

## Open Questions

- 0.10 — can a plugin conditionally claim inbound text? Gates 12.2.
- 12.4 — does the gateway deliver **edited** messages to the agent? If not, a typed
  correction vanishes: the exact 2026-07-27 failure, whose fix now sits outside the
  path.
- 13.3 — the isolated polling ingress and its spool at
  `/home/node/.openclaw/telegram/ingress-spool-default` may already provide part of
  what ADR-0014's replay queue does. Understand before trusting either.

## Changelog

## 2026-08-02 — Carved out of `openclaw-gateway-core` as slice 2

**Decision:** the cutover and everything downstream become their own change;
slice 1 archives with both runtimes deployed and the app still owning Telegram.
**Reasoning:** slice 1's requirements are true today and the baseline should say
so. The cutover is atomic — two processes cannot poll one token — so there is no
partial state to describe.
**Trade-off accepted:** two change directories to keep coherent, and a decision
trail split at its densest point. Mitigated by slice 1 carrying the whole trail
and this change referencing rather than restating it.
**Supersedes:** n/a — this is the second half of slice 1's task 8.11, not a
reversal of it.

## 2026-08-02 — The vision-OCR cap requirement moved here, correcting task 8.11

**Decision:** `gmail-isolation-boundary` splits four/three, not three/four — the
vision-OCR attempt cap comes to slice 2.
**Reasoning:** its only scenario is a rematch sweep requested repeatedly, and no
sweep tool exists in slice 1's seven-tool read inventory; gateway cron drives
nothing yet. Both pressure sources the requirement names are slice-2 constructs,
so by 8.11's own boundary rule it cannot be slice 1.
**Trade-off accepted:** none of substance — the cap (ADR-0010) is enforced in
Python today and is unaffected. What moves is the assertion that it survives new
pressure, which cannot be tested until that pressure exists.
**Supersedes:** slice 1 task 8.11's eval correction, which assigned three
requirements here.
