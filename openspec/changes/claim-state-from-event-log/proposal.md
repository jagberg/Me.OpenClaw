## Why

A claim's state is written seven different times by four modules, and only one of those writes records that it happened.

| Writer | Sets | Appends an event? |
|---|---|---|
| `invoice_matching:416` | `matched` | no |
| `claim_forms:513`, `:839` | `drafted` | no |
| `claim_status.mark_sent:653/658` | `sent` | no |
| `invoice_matching:865` | `absorbed` | no |
| `invoice_matching:892` (unmatch) | `pending_match` | no |
| `claim_status.process_reply:502` | insurer states | yes |
| the 2026-07-28 repair script | corrections | by hand |

`claim_status_events` is append-only and correct (ADR-0008), but it only ever hears about states **Petcover** causes. Every state **we** cause is invisible. Three consequences, all of them already realised:

- **A previous state cannot be read, only guessed.** Repairing claim #2 on 2026-07-28 required inferring its prior state from an absence — "no `acknowledged` event exists, so it must have been `sent`" — because nothing recorded the transition. There is no undo for a wrong state because there is no record of the right one.
- **Nothing knows a transition is illegal.** Re-reading Petcover mail on 2026-07-27 moved claims #6 and #7 from `settled` back to `acknowledged`, #18 from `below_excess` to `acknowledged` and #22 from `sent` to `below_excess`. The DB was restored from backup. `TERMINAL_STATUSES` exists, but as a list consulted by *some* callers, not as a rule the write path enforces.
- **The rules that do exist are scattered.** "`unclassified` never writes status" is an `if` inside one writer. "A terminal claim is never reopened" is a comment plus a helper some paths call. Neither is checkable, and a new writer inherits neither.

This is a state machine whose transition table doesn't exist anywhere. Justin's ask — "change the state back to the previous one" — isn't a feature that can be bolted on; it needs the history the system currently throws away.

## What Changes

- **Every transition appends an event**, ours included: `matched`, `drafted`, `sent`, `unmatched`, `absorbed`, alongside the insurer-side types already logged. Same table, same shape — the omission was never applying ADR-0008's rule to our own transitions.
- **One writer.** `claim_status.apply_event(claim_id, event_type, detail)` becomes the only code that touches `vet_claims.status`. It owns the transition table: what may follow what, which states are terminal, and the existing rules (`unclassified` never writes status; a terminal claim is never reopened) — now enforced in one place instead of restated per caller.
- **`status` becomes a projection of the log.** The column stays (every query and index depends on it) but is derived: it is whatever replaying the claim's events produces. Its value can then be *checked* against the log, which is what makes a bad write detectable instead of silent.
- **Undo is an append, never a delete.** A `state_reverted` event names the event it reverts; the projection skips reverted events and recomputes. This is non-destructive, auditable, and gives Justin a revert-to-previous action on the dashboard and on Telegram. The 2026-07-28 repair — done by hand, with a script, against the live DB — becomes a button.
- **Shadow mode before it becomes the writer.** Phase 1 computes the projection and compares it to the stored column on every tick, logging disagreements and changing nothing. Only once it agrees for all claims does Phase 2 hand it the pen. The re-read incident is why this is not optional.
- **A claim's timeline is shown**, on the dashboard and on request in Telegram — the history already exists and has never been visible.
- **`vet-info-request-chase` group 0 is absorbed.** "A re-read may not write status" stops being a special case: a re-read appends events, and the projection decides whether any of them changes state. Its task 0.4 (assigning a letter with an unheld serial) stays its own.

## Capabilities

### New Capabilities
- `claim-state-machine`: the transition table, the single write path, the projection of `vet_claims.status` from the append-only event log, and non-destructive reversion of a state change.

### Modified Capabilities
- `claim-status-tracking`: insurer replies route through the single write path rather than writing status directly, and a reply that would move a claim illegally is recorded and refused rather than applied.
- `claims-pipeline-resilience`: our own pipeline transitions (`matched`, `drafted`, `sent`, `unmatched`, `absorbed`) are recorded as events, so a claim's arrival at a state is auditable and reversible.
- `dashboard-visit-ledger`: a claim's timeline is visible, and a state change can be reverted from the row that shows it.
- `telegram-bot`: a claim's timeline is available on request, and a reversion is confirmed by tap like every other mutation.

## Impact

- Code: `claim_status.py` (`apply_event`, the transition table, the projection, `revert_state`), `invoice_matching.py` (3 write sites), `claim_forms.py` (2 write sites), `pipeline.py` (shadow-mode comparison on the tick), `main.py` + `templates/` (timeline, revert control), `telegram_bot.py` (timeline + revert callback).
- Data: **no schema change.** `claim_status_events` already has `claim_id`, `event_type`, `raw_email_id`, `detail`, `created_at`; new event types are values, not columns, and `state_reverted` names its target in `detail`.
- **Backfill, one-off:** each of the 23 existing claims needs a synthetic event for the state it is in now, or the projection would regress every one of them to `pending_match`. Container-side, backed up, dry-run diff reviewed — the same procedure the 2026-07-28 repair used.
- Tests: the transition table (every legal and illegal pair), the projection over real event sequences from the live log, reversion (including reverting a reversion), shadow-mode disagreement detection, and the four claims the re-read regressed as an explicit regression fixture.
- Docs: an ADR (this completes ADR-0008 rather than reversing it; records why not a state-machine library and why not full event sourcing), `README.md`, `app/openclaw/CLAUDE.md`.
- No third-party calls, no LLM, no new dependency.
