Phase 1 (groups 1–4) changes no behaviour and is shippable on its own. Phase 2 (groups 6–7) gives the projection authority and adds the timeline and revert control, and MUST NOT start until group 5's comparison has reported zero disagreements over a sustained period. The last state-writing change deployed without that step regressed four claims and needed a restore from backup.

## 1. Declare the machine

- [x] 1.1 Add `STATE_EVENTS: dict[str, str]` (event type → target state) and `STATELESS_EVENTS: frozenset` to `claim_status.py`, covering every type the log already contains plus `matched`, `unmatched`, `drafted`, `sent`, `absorbed`, `state_reverted`, `state_backfilled`. **Verified against the live log** (read-only, 2026-07-29): the seven types actually stored are `acknowledged`, `info_requested`, `settled`, `below_excess`, `approved`, `mismatch_dismissed`, `reference_detached` — all declared.
- [x] 1.2 Add `TRANSITIONS: dict[str | None, frozenset]` exactly as tabled in `design.md` (including `matched`→`matched` and `drafted`→`matched`, which exist because a per-pet split resets a draft, and `below_excess` as non-terminal). `None` is the from-state of a brand-new claim.
- [x] 1.3 Test: every declared pair is legal, the terminals are dead ends, and the `settled`→`acknowledged` move the re-read performed is refused. **This task was written on a false premise and is corrected here rather than ticked as stated.** It claimed all four of the 2026-07-27 transitions were illegal; the table in `design.md` legalises two of them. `sent`→`below_excess` and `below_excess`→`acknowledged` are ordinary forward moves — `below_excess` is non-terminal by decision (Decision 3), because the invoice is retained. What was wrong with those two was the routing and the replay, not the transition, so their guard is `_already_recorded` plus reference/Sr precedence, and the table cannot be their regression fixture. Only `settled`→`acknowledged` (#6, #7) is the table's to refuse. `test_the_backwards_moves_of_the_2026_07_27_reread_are_refused` asserts both halves and says which is which, so the next reader doesn't re-derive it.
- [x] 1.4 Test: every value in `STATE_EVENTS` is a real status appearing in `status_labels.LABELS`, and no event type is in both `STATE_EVENTS` and `STATELESS_EVENTS`.

## 2. One write path

- [x] 2.1 Add `claim_status.apply_event(claim_id, event_type, detail, email_id=None)`: append the event unconditionally, then apply the state if the transition is declared; on an undeclared transition leave the state alone and flag the claim naming both states and the event id. Return `{"applied", "state", "refused"}`. Added beyond the spec: an event type in *neither* set is also refused and flagged, rather than falling through as stateless — a typo'd type would otherwise be a permanent silent no-op, which is the failure class this change exists to end.
- [x] 2.2 Test: a legal transition writes the state; an illegal one records the event, leaves the state, and writes a flag naming both states; a stateless event records and moves nothing.
- [x] 2.3 Test: `unclassified` and `confirmed_resolved` move no state — the same guarantees the old inline conditions gave, now from the event-type classification. Asserted over **every** member of `STATELESS_EVENTS`, not the two named, so a type added to the set is covered on the day it is added.

## 3. Route every writer through it

- [x] 3.1 `claim_status.process_reply`: keep classification, correlation, reference/Sr learning and detail-building; hand the state change to `apply_event` and drop its own `status` UPDATE (including the `unclassified` special case, now redundant). One ordering decision made here: a refusal flag is **not** overwritten by a settlement-mismatch flag, because a refused transition means the state did not move at all — the more serious of the two, and the paid amount is still in the event detail.
- [x] 3.2 `claim_status.mark_sent`: append `sent` per claim in the submission via `apply_event`, preserving the existing `WHERE draft_id = ?` group semantics. Implemented by resolving the same `draft_id = ? AND status = 'drafted'` selection to ids first, then one `apply_event` each; the returned count is now applied-events rather than `rowcount`, which is the same number.
- [x] 3.3 `invoice_matching`: `matched`, `absorbed`, `unmatched` go through `apply_event`; the field writes (`invoice_data`, `matched_email_id`) stay where they are. State is applied **after** the field write in each case, so a `flag = ?` in the same statement cannot wipe the refusal it caused.
- [x] 3.4 `claim_forms`: both `drafted` writes go through `apply_event`; `claim_file_path`/`draft_id` stay. Needs a function-local `from . import claim_status` — `claim_status` imports `claim_forms`, so a module-level import is a cycle. Same precedent as `history_rows`' local `status_labels` import.
- [x] 3.5 `claim_forms.split_between_pets`: its draft reset becomes a `matched` event on each affected claim, which is why `drafted`→`matched` is in the table.
- [x] 3.6 Test: creating a claim, matching, drafting, sending and settling it produces one event per transition, in order, with no direct status write anywhere in the path. Runs through the real writers (`_mark_matched`, `mark_sent`, `process_reply`), not through `apply_event` directly, and asserts the column and the projection agree at the end.
- [x] 3.7 Guard test: no `SET status` on `vet_claims` exists outside `claim_status.py` — a mechanical check, because this repo has a documented history of a convention being broken four times in one session. **Two write sites the task list did not count** turned up while doing this: `invoice_matching:467` and `claim_forms:657` both `INSERT` a claim straight at `'matched'` (the apportionment siblings). They are creations, not transitions, so the INSERT stands — but each now appends its own `matched` event, without which those claims would have no history to fold and would disagree with the column forever. The count is nine statements, not seven. The guard test also asserts its own regex fires on a synthetic violation and stays quiet on the legitimate `WHERE ... AND status = ?` reads — it had a false positive on exactly those before that assertion was added.

## 4. The projection

- [x] 4.1 Add `claim_status.project_state(claim_id)` (and a bulk form for the tick): fold events in `created_at, id` order, skipping reverted and stateless events, applying state events subject to `TRANSITIONS`. **A gap in `design.md` had to be closed to make this work:** the design says `None` is a new claim's from-state and `TRANSITIONS[None] = {pending_match}`, but no creation event exists to consume that step, so a fold seeded at `None` refuses every claim's first event. The fold is therefore seeded at `CREATED_STATE = "pending_match"`, which all three INSERT sites honour (`vet_detection` writes it literally; the two apportionment inserts write `'matched'` and append the matching event). `TRANSITIONS[None]` stays as the notional creation step and is asserted, but nothing folds through it.
- [x] 4.2 Test the projection against **real sequences pulled from the live log** (read-only, 2026-07-29): claim #8's `acknowledged` → `info_requested` → `info_requested`, claim #21's `acknowledged` → `approved` → `settled`, claim #19's `acknowledged` → `below_excess` → `acknowledged` (which only folds because `below_excess` is non-terminal), and claim #6's `approved` → `settled` → `mismatch_dismissed`. Each is prefixed with the `matched`/`drafted`/`sent` events the writers now append; **without that prefix every one of them folds to `pending_match`**, which is asserted separately as the backfill case. Sequences are copied into the fixture rather than read live, so the suite stays hermetic.
- [x] 4.3 Test: a reverted event is skipped; an event whose transition is illegal from the replayed state is skipped without aborting the fold. `_reverted_event_ids` handles **one level only** — reverting a reversion is task 7.1's, and nothing writes `state_reverted` yet — and says so in its docstring rather than leaving the limit implicit.

## 5. Shadow mode (Phase 1 ends here)

- [x] 5.1 Add the tick-time comparison: project every claim, compare to the stored column, log disagreements at **WARNING** with claim ids (not ERROR — ADR-0015 reserves that for "Justin must act", and he cannot fix a projection bug). `pipeline.compare_state_projection()` runs **first in `run_once`, before the Gmail-auth check**, deliberately: a check placed after that check would be skipped by the very outage it should still report on, which is the mistake ADR-0015's own alert path made.
- [x] 5.2 Expose `state_projection_disagreements` on `/health`.
- [x] 5.3 Test: an injected disagreement is reported and **no** claim's status or flag is modified by the comparison.
- [ ] 5.4 Run both suites; deploy from the `deploy` worktree; confirm `/health` and that the count is present. — **suites done** (159+ core, 29 telegram, both green, and green *before* the new tests were added too, which is what establishes no behaviour change). Deploy not yet run.
- [x] 5.5 Record the disagreement count on the live DB before the backfill — it is expected to be non-zero and to name exactly the claims whose transitions we caused. Write down which claims and why.

  **Measured 2026-07-29 on a `src.backup()` copy of the live DB in the scratchpad, with `DATABASE_PATH` pointed at the copy** (never the app's own connection against the live file — ADR-0018 and the phantom-`C:\data` rule).

  **19 of 22 claims disagree.** Every one projects `pending_match`; the three that agree (#4, #5, #17) agree only because they never moved off `pending_match`. So the count is exactly `22 − 3` and has a single cause: the six writers that moved claims to `matched`/`drafted`/`sent` appended no events, so no claim's log contains its own submission history. Nine claims hold events at all, and all nine hold **reply** events only.

  | Claim | stored | projected |
  |---|---|---|
  | #1, #2, #12 | `sent` | `pending_match` |
  | #3, #9, #10, #15, #16, #20, #25 | `matched` | `pending_match` |
  | #6, #7, #21 | `settled` | `pending_match` |
  | #8 | `info_requested` | `pending_match` |
  | #13, #19 | `acknowledged` | `pending_match` |
  | #18, #22 | `below_excess` | `pending_match` |
  | #11 | `absorbed` | `pending_match` |

  No claim projects to a *wrong non-birth* state, which is the result that would have indicated a table or fold defect. Group 6.2's check is therefore: the backfill must touch exactly these 19 and nothing else.

## 6. Backfill (gate to Phase 2)

- [ ] 6.1 Write the backfill: for each claim, project; if it already equals the stored status, leave it alone; otherwise append one `state_backfilled` event carrying the current status, timestamped at `updated_at`, `detail = {"backfilled": true, "reason": "transition predates the event log"}`.
- [ ] 6.2 Dry-run it, print the per-claim diff, and check it against 5.5's list — anything backfilled that 5.5 did not predict is a projection bug, not a backfill case.
- [ ] 6.3 Run it **inside the container** (`docker exec`), backup first, snapshot before/after (ADR-0018). Ask before running.
- [ ] 6.4 Confirm the disagreement count is zero afterwards, and stays zero across a week of ticks. **Do not proceed to group 7 until it does.**

## 7. Reversion, timeline and authority (Phase 2)

- [ ] 7.1 Add `claim_status.revert_state(claim_id, event_id, reason)`: append `state_reverted` with `{reverts_event_id, reason, from, to}`; the projection already ignores reverted events. Refuse reverting an event that is already reverted, and refuse a stateless one.
- [ ] 7.2 Test: reverting a misrouted event returns the claim to the state its remaining events produce; reverting a reversion re-applies the original; reverting the event that settled a claim moves it out of `settled` while no path can assert a new state on a terminal claim directly.
- [ ] 7.3 Make the projection authoritative: `apply_event` writes what the projection says, and a mismatch is repaired from the log rather than trusted.
- [ ] 7.4 Dashboard: a claim timeline (events in order, source, reverted entries marked, backfills labelled) and a revert control that names the before/after states and requires confirmation.
- [ ] 7.5 Telegram: timeline on request by claim id, and revert as a confirm-by-tap mutation naming before/after.
- [ ] 7.6 Surface a refused transition beyond the flag — it means either a defect or new insurer behaviour, and both are worth knowing the same day.
- [ ] 7.7 Tests for both surfaces, including that a backfilled claim's timeline says so.

## 8. Absorb and close out

- [ ] 8.1 Mark `vet-info-request-chase` group 0 absorbed: a re-read appends events and the write path decides, so "a re-read may not write status" is no longer a special rule. Its task 0.4 (unheld-serial assignment) stays its own.
- [ ] 8.2 New ADR: this completes ADR-0008 rather than reversing it; the transition table; projection-with-cached-column and why not compute-on-read; reversion by append; why not a state-machine library and why not full event sourcing; the two incidents that motivated each. Add it to `docs/adr/README.md`.
- [ ] 8.3 `README.md`: the lifecycle is a declared state machine; state changes are recorded and reversible.
- [ ] 8.4 `app/openclaw/CLAUDE.md`: `apply_event` is the only writer of `vet_claims.status`; where the transition table lives; the event-type split; the projection comparison and what a non-zero `/health` count means.
- [ ] 8.5 Record in this file what was verified against the live DB versus only unit-tested — including the disagreement counts before and after the backfill.
- [ ] 8.6 `openspec/BACKLOG.md`: the two open questions — whether reverting a settled claim needs a stronger confirmation than a tap, and whether a refused transition deserves its own notification.
