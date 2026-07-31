# ADR-0022: A claim's status is a declared state machine with one writer, and the event log is its source

**Date**: 2026-07-31
**Status**: accepted (Phase 1 implemented and deployed; Phase 2 designed, not built)
**Deciders**: Justin

## Context

ADR-0008 established that every claim status change is recorded to an append-only `claim_status_events` log. It did not say which changes are *legal*, and it did not stop anything else writing the column. What existed by July 2026:

- `vet_claims.status` — a mutable column written by **nine** statements across `invoice_matching`, `claim_forms` and `claim_status` (seven `UPDATE`s plus two `INSERT`s that create a claim already `matched`). All but one appended no event.
- `TERMINAL_STATUSES`, `CLOSED_STATUSES`, `AWAITING_REPLY_STATUSES` — three tuples consulted by *some* call sites. A convention, not a rule.

Two live incidents forced the issue.

**2026-07-27, the re-read.** Replaying 23 already-ingested Petcover emails appended 11 events and moved four claims backwards: #6 and #7 `settled` → `acknowledged`, #18 `below_excess` → `acknowledged`, #22 `sent` → `below_excess`. Restored from backup. Event-level idempotency was tried first and did not help — the problem was never duplicate events, it was that a re-routed event's status write had nothing standing between it and the column.

**2026-07-28, the repair.** Six stored events were the wrong type. Fixing them meant recomputing three claims' statuses by hand, including claim #2, whose prior state had to be *inferred from an absence*: "it holds no `acknowledged` event, so it was `sent`". The inference was correct and should never have been necessary.

## Decision

**1. Event types are classified as data, in three kinds — not as conditions inside writers.**

- **State-changing** (`STATE_EVENTS`): each names one fixed target state.
- **Stateless** (`STATELESS_EVENTS`): recorded, never move state. `unclassified` lives here; it was previously an `if` inside one writer's `UPDATE`, and that particular special case becoming a property of the type is this whole change in miniature.
- **Backfill** (`BACKFILL_EVENT`): names a per-event target read from its own `detail`, and is **exempt from the transition table** because it asserts a state the log has no path to. The only exemption in the machine.

**2. `claim_status.apply_event` is the only writer of `vet_claims.status`.** It appends the event unconditionally — the event happened, and hiding it is how the 2026-07-28 audit became necessary — then consults the table. Legal → writes the state. Illegal → writes nothing, flags the claim naming both states and the event id, keeps the event as evidence. An event type in *neither* set is also refused and flagged rather than falling through as stateless: a typo'd type would otherwise be a permanent silent no-op.

Enforced mechanically, not by convention: `test_no_module_outside_claim_status_writes_the_status_column` fails if any module writes the column directly. This repo has a documented history of conventions being broken by people who had just read them (ADR-0018, four times in one session).

**3. `TRANSITIONS` declares every legal move**, derived from what the writers already did rather than from a tidy lifecycle diagram. Hence two entries that look wrong and are not: `matched → matched` and `drafted → matched` both exist because `split_between_pets` resets a draft, and `below_excess` is **non-terminal** because the invoice is retained. `settled` and `declined` are dead ends (ADR-0011).

**4. `status` is a projection; the column is its cache.** `project_state` folds a claim's events in `created_at, id` order — skipping reverted and stateless ones, applying state events subject to the table. The fold seeds at `CREATED_STATE = "pending_match"`, **not** at `None`: no creation event exists to consume a `None → pending_match` step, so seeding at `None` would refuse every claim's first event.

**5. Callers that destroy something before the state write must ask the table first.** `claim_status.transition_allowed` exists for exactly one reason, learned the hard way — see Consequences.

## Alternatives rejected

**A state-machine library** (`transitions`, `python-statemachine`). They encode legal transitions, which is twelve lines of data here, and provide no history and no undo. The undo is the requirement; a library solves the half already solvable and adds a dependency to describe one table.

**Full event sourcing.** Only `status` has this problem. `invoice_data`, `pet_id`, `condition_text` and `draft_id` are facts that get *corrected*, not states that get *transitioned* — there is no incident involving any of them drifting from their events. Projecting them would be a rewrite with no failure to point at.

**Dropping the column and computing on read.** Forty readers, several SQL `WHERE status IN (...)` filters, and a per-row fold on every dashboard render — to fix a problem that a comparison already catches.

**Tightening the table to cover the routing bug.** Rejected on principle: two of the four 2026-07-27 moves are legal transitions, and refusing them would make the table lie about the lifecycle to compensate for a different subsystem's fault. See Consequences.

## Consequences

**Shipped and deployed** (`6034028+deploy`): the three tables, `apply_event`, all nine write sites routed through it, the projection, and a shadow comparison on each tick logging at WARNING (never ERROR — ADR-0015 reserves that for "Justin must act", and he cannot fix a projection bug) with a `state_projection_disagreements` count on `/health`. Behaviour is otherwise unchanged.

**Not built:** `revert_state`, the claim timeline, and the projection becoming authoritative. Recorded as Phase 2 and gated (below). Until then the column is what everything reads and the projection only watches.

Limitations, recorded because none of them is obvious from the code:

- **A zero disagreement count proves much less than it appears to.** The backfill seeds each claim from the column the comparison then checks it against, and a refused transition leaves column and replay equally unchanged — detector and enforcer share `TRANSITIONS`. A read-only re-fold on 2026-07-31 found **17 of 24 real events skipped as illegal from the replayed state**, on claims #6 #7 #8 #13 #18 #19 #21 #22, which reach their stored status entirely via the seed. So Phase 2's gate is **not** "the count held at zero for a week" but "claims actually transitioned during the week and folded correctly".
- **The central mechanism has not fired in production.** `matched`, `drafted`, `sent`, `unmatched` and `absorbed` had **zero rows** in the live log two days after deploy. Everything about the six re-routed writers is unit-tested only.
- **Two of the four 2026-07-27 transitions have no demonstrated guard.** `sent → below_excess` and `below_excess → acknowledged` are legal and must stay legal; what was wrong was that the event reached the wrong claim. ADR-0020's amendments record that idempotency was tried and found insufficient, and reference/Sr routing precedence has never been tested against replayed misrouted mail. A misrouted-but-legal replay would still be applied today. Tracked in `openspec/BACKLOG.md`.
- **Routing a write through a refusable path can strand a caller that already destroyed something.** `invoice_matching.unmatch` wiped the invoice and *then* had its `pending_match` write refused for a submitted claim, leaving it in `sent` with no invoice — shipped, deployed and reachable from a Telegram button before an eval caught it. Hence `transition_allowed`: ask the table before destroying, not after. The general rule is in `docs/failure-modes.md`.
- **The backfill is shallow by design.** Nineteen claims carry one synthetic `state_backfilled` event instead of a fabricated matched/drafted/sent history, because we do not have those dates. Reverting such a claim lands on "nothing before this". Honest, and the timeline will say so when it exists.
- **Reversion handling is one level deep.** `_reverted_event_ids` does not yet handle reverting a reversion; nothing writes `state_reverted` at all until Phase 2.

Supersedes nothing. **Completes ADR-0008** — that ADR made the log authoritative as a record; this one makes it authoritative as a *state*. Relates to ADR-0011 (terminal states), ADR-0015 (why WARNING), ADR-0018 (the backfill is a container-side write), ADR-0020 (which of its decisions this implements, and which it shows unimplementable as written), and ADR-0021 (the vocabulary is unchanged; only who may write the state changed).

## Amendment (2026-07-31) — the reversion skip is removed until Phase 2 builds it

A whole-codebase retrospective looking for unearned complexity found this ADR's
last consequence to be understating the case. `_reverted_event_ids` was not merely
"one level deep": it was **inert**. `state_reverted` appeared in exactly three
places — the `STATELESS_EVENTS` set, that function's own equality check, and one
hand-written `INSERT` in a test. The live log holds zero rows of it, and
`apply_event` could not have produced one, because a stateless event type is
recorded and nothing more. So the fold's skip could never fire against real data.

Both are removed: the function, and `state_reverted` from `STATELESS_EVENTS`.

This changes behaviour in one direction, deliberately. With the type undeclared,
an attempt to append a reversion now lands on `apply_event`'s unknown-event-type
branch — refused, and the claim flagged naming the event id — instead of being
silently recorded and silently ignored. Phase 2 is not built; asking for a revert
today is a defect, and the repo's rule is that failures are visible.

Decision 4 above says the fold skips "reverted and stateless" events. Until
task 7.1 lands, read that as stateless only.

**Trade-off accepted:** task 7.1 now has to restore the fold's skip alongside
`revert_state`, rather than finding it already in place — its own wording claimed
"the projection already ignores reverted events", which is corrected in that file.
That is a few lines of re-work in exchange for deleting a branch that has never
executed and could not be tested against anything real. It also removes the trap
where Phase 2's author trusts a skip that was never exercised: the one-level-only
limitation named above was real, and rebuilding deliberately is the moment to
handle reverting a reversion, which the deleted version never did.
