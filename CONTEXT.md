# OpenClaw Claims

Vet-insurance claims automation for one household: bank charges in, ready-to-send insurer submissions out, reply tracking until settlement.

## Language

**OpenClaw** (the name collides — say which you mean):
*This repo* has been called OpenClaw since inception. **OpenClaw the product** is an unrelated local-first gateway daemon (channel transport, agent sessions, model routing, cron, plugins). The repo never depended on it — verified 2026-08-01 — and the resemblance is entirely the name. A change in flight (`openclaw-gateway-core`, ADR-0024) adopts the product as this system's shell, which makes the ambiguity operational rather than cosmetic: "OpenClaw is down" will mean two different outages.
_Say_: "the gateway" for the product, "the app" or "the claims service" for this codebase.
_Avoid_: bare "OpenClaw" in anything written after the swap.

**The gateway**:
The OpenClaw daemon, in its own container. Owns the Telegram bot token, polling, the chat agent loop, model resolution and cron. Owns no claim logic and holds no Gmail credential, deliberately (ADR-0024, ADR-0023).
_Avoid_: server, bot, OpenClaw

**The plugin**:
`openclaw-claims`, running *inside* the gateway. Registers this app's slash commands and forwards each to `/internal`. Exists because a `command` button invokes a **native** command and `/mark` is not native to the gateway. Carries no claim rules.
_Avoid_: adapter, bridge, integration

**Claim**:
One `vet_claims` row, anchored 1:1 to a bank charge. The system's unit of reconciliation ("claim #22"). Not what the insurer sees.
_Avoid_: transaction-claim, charge-claim

**Submission**:
One send to Petcover: one draft, one filled form, 1–4 invoices. Identified by `draft_id`. Claims sharing a `draft_id` move together. A Submission does NOT own a reference — Petcover files its documents into Condition Threads, possibly several.
_Avoid_: batch, claim (in insurer-facing copy)

**Condition Thread**:
Petcover's actual unit: one (pet, condition) pairing with one claim reference, reused for the life of the condition — proven to span years and many settle cycles (DC1-27-5628 Arthritis: settled Feb 2026, reused Jul 2026). Petcover assigns the condition themselves from the invoices; our condition text is input, not authority. A declined thread is terminal only for its own claims — other threads are unaffected.
_Avoid_: claim reference (the reference is the thread's id, not the thing itself)

**Serial (Sr)**:
Petcover's running number for each claim document inside a Condition Thread ("DC1-27-5628 Sr 3"). Their letters cite reference + Sr; it is how an event targets one claim within a thread.

**Excess**:
$150 deducted from the first settlement of each Condition Thread in each *current, open* Policy Year — judged by each claim's own transaction date, not by when Petcover replies (shipped 2026-07-24; a claim whose own transaction falls in an already-closed policy year is assumed to have already passed the threshold, since our history there is presumed incomplete). A `below_excess` reply (non-terminal, invoice retained) is real evidence to Justin, not something to submit and eat as a $0 settlement — also shipped. **Still pending** (`excess-threshold-accrual`, ADR-0013): actually *holding* a condition's claim from submission until its accrued claimable exceeds this excess — today the claim still drafts and submits regardless, it's only the reply-side validation/classification that's live.

**Policy Year**:
Runs anniversary-to-anniversary of the pet's policy, NOT the calendar year. Excess consumption and the $10k annual cap both reset on the anniversary.

**Invoice**:
The vet's per-visit itemised document. Usually paid by one charge; can be paid across several charges (merge), and up to 4 ride one Submission.
_Avoid_: bill, statement (a statement is precisely NOT an invoice — running totals fail adequacy validation)

**Charge**:
A bank transaction from the NetBank CSV. The ceiling on what its Claim can be worth, never the claimed amount itself.
_Avoid_: payment, transaction (ambiguous with Petcover payouts)

## Language — assistant side

This half of OpenClaw (`tasks.py`, `reminders.py`, `gmail_ingest.py`) is independent of claims and shares none of the vocabulary above. It had no entry here at all until 2026-07-25, when it gained its first interface (Telegram chat) and the ambiguity started to matter.

**Task**:
One `tasks` row: a piece of household admin to be chased (call the painter, book the service). Identified by `#id`, which is the handle for closing it. Has an *outcome* recorded when closed — who was spoken to, what was said. Nothing to do with a Claim, and never a step in the claims pipeline.
_Avoid_: action (an Action is claims-side, one of `pending_actions`' nine kinds), todo, item

**Follow-up**:
A datetime extracted from a Task's own text by the LLM at capture time, which schedules a Reminder. Absent when the text implies no date — most Tasks have none.

**Reminder**:
One `reminders` row plus a scheduled job — APScheduler today, gateway cron plus an app-side catch-up sweep after the swap (ADR-0002 addendum). Fires by flipping to `due`; restart-safe (a reminder whose time passed while the app was down fires on startup, not skipped).
_Avoid_: notification, alert (those are the claims side's Telegram pushes and `ops_alerts`)

## Sweep

A named, on-demand check over Gmail the chat agent may run — `reconcile_sent_invoice_requests`, `rematch_claims`, `poll_petcover_now`. Deliberately not "searching email": each has a fixed scope, and the agent must state that scope rather than imply it read the mailbox (ADR-0016). All three act immediately rather than proposing, because the 15-minute tick already makes the same calls unattended and each is idempotent under replay.
_Avoid_: search, reading my email
