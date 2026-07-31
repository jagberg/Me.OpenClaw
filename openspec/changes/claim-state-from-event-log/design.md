## Context

The claim lifecycle has twelve states and no transition table. What exists instead:

- `claim_status_events` — append-only, one row per insurer reply or Justin action. Correct as far as it goes (ADR-0008).
- `vet_claims.status` — a mutable column written by nine statements across `invoice_matching`, `claim_forms` and `claim_status` (seven UPDATEs plus two INSERTs that create a claim already `matched`). All but one append nothing.
- `TERMINAL_STATUSES`, `CLOSED_STATUSES`, `AWAITING_REPLY_STATUSES` — three tuples consulted by *some* call sites. Not a rule; a convention.

Two live incidents define the requirements:

**2026-07-27, the re-read.** Replaying 23 already-ingested Petcover emails appended 11 events and moved four claims backwards: #6 and #7 `settled` → `acknowledged`, #18 `below_excess` → `acknowledged`, #22 `sent` → `below_excess`. Restored from `openclaw.db.bak-pre-inforequest`. Event-level idempotency was tried first and did not help, because the problem was never duplicate events — it was that a re-routed event's status write had nothing standing between it and the column.

**2026-07-28, the repair.** Six stored events were the wrong type. Fixing them meant recomputing three claims' statuses by hand, one at a time, with the reasoning written into a script — including claim #2, whose prior state had to be *inferred from an absence* ("it holds no `acknowledged` event, so it was `sent`"). That inference was correct, and it should never have been necessary.

Constraints:

- Live schema changes to existing tables need hand-run DDL. New event types are values in an existing column, so this needs none.
- ADR-0018: host-side access to the live DB is read-only; the backfill is a container-side write.
- ADR-0015: ERROR means *Justin* must act. A projection disagreement means a developer must act, so it is not an ERROR.
- `status` is read by roughly forty call sites, several SQL `WHERE status IN (...)` filters, and the dashboard. It cannot be removed.

## Goals / Non-Goals

**Goals:**

- One place that knows every legal transition, and one path that can write state.
- A claim's previous state is a fact on record, not an inference — so reverting to it is possible.
- An illegal transition is refused and visible, not applied silently.
- No schema change, no new dependency, no LLM.

**Non-Goals:**

- Full event sourcing. Only `status` has this problem. `invoice_data`, `pet_id`, `condition_text`, `draft_id` are facts that get corrected, not states that get transitioned; projecting them would be a rewrite with no failure to point at.
- A state-machine library. See Decision 6.
- Removing the `status` column. It is a cache of the projection, and forty readers depend on it.
- Changing what any state *means*, or adding a state. The vocabulary (ADR-0021) and the statuses themselves are unchanged.
- `vet-info-request-chase` task 0.4 (assigning a letter whose serial no claim holds). Different problem — routing, not state.

## Decisions

### 1. Every state change is an event; some events change no state.

Event types split in two, and the split is data, not an `if`:

**State events** — each names the state it moves the claim to:

| Event | Target state | Raised by |
|---|---|---|
| `matched` | `matched` | `invoice_matching` |
| `unmatched` | `pending_match` | wrong-invoice rejection |
| `drafted` | `drafted` | `claim_forms` |
| `sent` | `sent` | `mark_sent` |
| `absorbed` | `absorbed` | split/merge resolution |
| `acknowledged` `info_requested` `suspended` `approved` `settled` `declined` `below_excess` | same name | `process_reply` |

**Stateless events** — recorded, never move state: `unclassified` (a review-queue entry, per the existing rule), `confirmed_resolved`, `mismatch_dismissed`, `reference_detached`, `state_reverted`.

**Correction, 2026-07-29 (during implementation).** This list originally included
`state_backfilled`, which contradicts Decision 8's "append one `state_backfilled`
event carrying the current status". Stateless wins in the fold, so the backfill
would have written 19 events and moved nothing — every backfilled claim would still
project `pending_match`, and Decision 7's Phase 2 handover would then have reset all
19 to `pending_match`. The 2026-07-27 regression again, at five times the scale, and
caused by the step meant to prevent it.

`state_backfilled` is therefore a **third category**, in neither set:
state-bearing, but with a per-claim target read from `detail["status"]` instead of a
fixed one, and exempt from the transition table — it *asserts* a state the log has
no path to, which is the entire reason it is written. It is the only exemption in the
machine, and a backfill naming an undeclared status is refused rather than seeded.

`unclassified` not writing status stops being a special case in one writer and becomes a property of the event type. That is the whole shape of this change in miniature.

### 2. `apply_event` is the only writer, and it enforces the table.

```
apply_event(claim_id, event_type, detail, email_id=None) -> {"applied": bool, "state": str, "refused": str | None}
```

It appends the event unconditionally — the event happened, and hiding it is how the 2026-07-28 audit became necessary — then consults the transition table:

- **Legal** → writes the new state.
- **Illegal** (e.g. `settled` → `acknowledged`) → does **not** write state, and flags the claim with a human-readable reason naming both states and the event id. Failures are visible; the event stays as evidence.
- **Stateless** → no state write, no refusal.

Callers stop writing `status` themselves. `process_reply` keeps its routing and detail-building and hands off; `invoice_matching` and `claim_forms` keep their own field writes (`invoice_data`, `draft_id`, `claim_file_path`) and call `apply_event` for the state.

### 3. The transition table, written down for the first time.

| From | May move to |
|---|---|
| *(new claim)* | `pending_match` |
| `pending_match` | `matched`, `absorbed` |
| `matched` | `drafted`, `pending_match`, `matched` (re-match after a split), `absorbed` |
| `drafted` | `sent`, `matched` (a per-pet split resets the draft), `pending_match` |
| `sent` | `acknowledged`, `info_requested`, `suspended`, `approved`, `settled`, `declined`, `below_excess` |
| `acknowledged` | `info_requested`, `suspended`, `approved`, `settled`, `declined`, `below_excess` |
| `info_requested` | `suspended`, `acknowledged`, `approved`, `settled`, `declined`, `below_excess` |
| `suspended` | `info_requested`, `approved`, `settled`, `declined`, `below_excess` |
| `approved` | `settled` |
| `below_excess` | `sent`, `acknowledged`, `approved`, `settled`, `declined` — non-terminal by decision, the invoice is retained |
| `settled`, `declined` | **nothing** (terminal, ADR-0011) |
| `absorbed` | `pending_match` (a mistaken merge is undone by reverting it) |

Derived from what the code actually does today, not from what a lifecycle diagram would suggest: `matched → matched` and `drafted → matched` both exist because `split_between_pets` resets a draft, and `below_excess` is deliberately non-terminal.

Every pair not in this table is illegal.

**Correction, 2026-07-30 (found by eval).** This paragraph originally read "including the four the re-read performed. Those four become an explicit regression fixture", which the table two rows above contradicts: `sent` lists `below_excess`, and `below_excess` lists `acknowledged`. Only `settled`→`acknowledged` (claims #6 and #7) is this table's to refuse. Claim #22's `sent`→`below_excess` and claim #18's `below_excess`→`acknowledged` are ordinary forward moves — `below_excess` is non-terminal by the decision recorded in this very table. What was wrong with those two on 2026-07-27 was the routing and the replay, not the transition. **They have no demonstrated guard** — ADR-0020 records event-level idempotency as tried against the real DB for this incident and found insufficient, and reference/Sr routing precedence has never been tested against replayed misrouted mail; see `openspec/BACKLOG.md`. `tasks.md` 1.3 recorded this correctly on 2026-07-29. A second eval on 2026-07-31 found this paragraph and `specs/claim-state-machine/spec.md` still naming idempotency as the guard — the retraction had reached the sibling delta and this correction's own opening sentence, but not its conclusion.

### 4. `status` is a projection, and the column is its cache.

`project_state(claim_id)` folds the claim's events in `created_at, id` order: skip reverted events, skip stateless ones, apply state events subject to the table. The result is the claim's state.

> **As shipped (2026-07-31):** the reverted-event skip is *not* present. It was built, found never to have fired — nothing wrote `state_reverted`, so the branch was unreachable — and removed by the retro along with the event type. The design above is still the Phase 2 target; task 7.1 restores the skip alongside `revert_state`. ADR-0022's amendment has the reasoning.

The column keeps being written by `apply_event` — so reads, indexes and `WHERE status IN (...)` filters are untouched — but it is now *derivable*, which is the point: a disagreement between column and projection is a detectable defect rather than an invisible one.

Alternative: drop the column and compute on read. Rejected — forty readers, several SQL filters, and a per-row fold on every dashboard render, to fix a problem that a comparison already catches.

### 5. Reversion appends; it never deletes.

`revert_state(claim_id, event_id, reason)` appends `state_reverted` with `detail = {"reverts_event_id": …, "reason": …, "from": …, "to": …}`. The projection ignores the reverted event, so state falls back to whatever the remaining events produce — the genuine previous state, read rather than inferred.

- Reverting a reversion is a `state_reverted` naming the earlier `state_reverted`. No special case.
- A reverted event stays in the log and stays visible on the timeline, marked reverted. The audit trail is the whole point (same reasoning as `detach_reference`, which chose a logged undo over a silent wipe).
- Reversion is gated: it is a mutation, so Telegram confirms by tap like every other mutation, and the dashboard control names both states before and after.
- Reverting *is* allowed out of a terminal state, because a wrong `settled` is exactly the case that needs it — but only by reverting the event that caused it, never by asserting a new state directly. That is the difference between an undo and reopening a closed claim, and it keeps ADR-0011's rule intact.

### 6. Not a state-machine library, and not full event sourcing.

`transitions` / `python-statemachine` encode legal transitions — which is the table above, twelve lines of data — and provide no history and no undo. The undo is the requirement, so a library solves the half already solvable and adds a dependency to describe one table.

Full event sourcing (project every field, keep no mutable row) is the other over-correction: there is no incident involving `invoice_data` drifting from its events, because those writes are corrections of fact, not transitions. Scope stays at `status`.

### 7. Shadow mode first. Non-negotiable, and the reason is on record.

**Phase 1** — `apply_event` and the projection exist; every writer routes through `apply_event`; the projection runs on each tick over every claim and compares against the column. Disagreements are logged at WARNING with claim ids and surfaced as a `/health` count (`state_projection_disagreements`). WARNING, not ERROR: ADR-0015 reserves ERROR for "Justin must act", and he cannot fix a projection bug.

**Phase 2** — once the count has been zero across a week of ticks and the backfill is in, the projection becomes authoritative: `apply_event` writes what the projection says, and a mismatch is repaired from the log rather than trusted.

The last time a state-writing change went to the live DB without a comparison step first, it regressed four claims and needed a restore from backup. Phase 1 is that comparison step.

### 8. Backfill preserves real history and synthesizes only what is missing.

For each existing claim (22 at the time of writing): project over its existing events. If the projection already equals the stored status, do nothing — the history is genuine and stays untouched. If it disagrees (every claim whose state we caused, since those transitions were never recorded), append one `state_backfilled` event carrying the current status, timestamped at the claim's `updated_at`, with `detail = {"backfilled": true, "reason": "transition predates the event log"}`.

Container-side, backup first, dry-run diff reviewed, preconditions asserted — the procedure the 2026-07-28 repair established.

## Risks / Trade-offs

- **The transition table is wrong somewhere and refuses a legal move** → shadow mode catches it before the projection has authority, and a refusal flags the claim visibly rather than silently dropping the change. The table is derived from the seven existing writers, so its first version is a description of current behaviour, not an aspiration.
- **A caller forgets `apply_event` and writes `status` directly** → the projection comparison flags that claim on the next tick. Worth a test that greps for `SET status` outside `claim_status.py`; a mechanical guard beats a convention (this repo has evidence: ADR-0018's rule was broken four times in one session).
- **Backfilled claims have a shallow history** — one synthetic event, so reverting them lands on "nothing before this". Honest, and stated on the timeline as a backfill rather than presented as real history.
- **Reversion becomes a footgun** — a tap that quietly moves a claim out of `settled`. Mitigated by naming both states in the confirmation and by recording who reverted what and why; there is no unlogged path.
- **Two sources of truth during Phase 1** (column and projection). That is deliberate and time-boxed; the whole point of the phase is that they are compared rather than assumed equal.
- **Scope**: seven write sites, three surfaces, one backfill. Not a weekend, and it touches the paths that create every claim. Phase 1 is shippable on its own and changes no behaviour, which is how the risk gets paid down incrementally.

## Migration Plan

1. Phase 1: `apply_event`, the transition table, the projection, all seven writers routed through it, shadow comparison on the tick, `/health` count. No behaviour change.
2. Deploy; watch the disagreement count for a week of ticks.
3. Backfill the disagreeing claims, container-side, backup + dry-run diff.
4. Phase 2: projection becomes authoritative; add the timeline view and the revert control.
5. Absorb `vet-info-request-chase` group 0: a re-read now appends events and lets the projection decide, so "a re-read may not write status" needs no separate rule.

Rollback: Phase 1 is inert and reverts cleanly. Phase 2 reverts to reading the column, which is still being written.

## Open Questions

- **Who may revert what?** Everything currently reversible in this system is pre-submission. Reverting a `settled` claim is new territory — worth deciding whether that specific case needs a typed confirmation rather than a tap.
- **Should a refused transition notify?** It flags the claim, which the dashboard shows and the daily nudge picks up. A dedicated Telegram message may be right, since a refusal means Petcover said something the model of the lifecycle doesn't allow — which is either a bug or a genuinely new insurer behaviour, and both are worth knowing about the same day.
