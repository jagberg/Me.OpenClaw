Phase 1 (groups 1–4) changes no behaviour and is shippable on its own. Phase 2 (groups 6–7) gives the projection authority and adds the timeline and revert control, and MUST NOT start until group 5's comparison has reported zero disagreements over a sustained period. The last state-writing change deployed without that step regressed four claims and needed a restore from backup.

## 1. Declare the machine

- [ ] 1.1 Add `STATE_EVENTS: dict[str, str]` (event type → target state) and `STATELESS_EVENTS: frozenset` to `claim_status.py`, covering every type the log already contains plus `matched`, `unmatched`, `drafted`, `sent`, `absorbed`, `state_reverted`, `state_backfilled`.
- [ ] 1.2 Add `TRANSITIONS: dict[str | None, frozenset]` exactly as tabled in `design.md` (including `matched`→`matched` and `drafted`→`matched`, which exist because a per-pet split resets a draft, and `below_excess` as non-terminal). `None` is the from-state of a brand-new claim.
- [ ] 1.3 Test: every declared pair is legal, and the four transitions the 2026-07-27 re-read performed (`settled`→`acknowledged` ×2, `below_excess`→`acknowledged`, `sent`→`below_excess`) are all illegal. This is the regression fixture for that incident.
- [ ] 1.4 Test: every value in `STATE_EVENTS` is a real status appearing in `status_labels.LABELS`, and no event type is in both `STATE_EVENTS` and `STATELESS_EVENTS`.

## 2. One write path

- [ ] 2.1 Add `claim_status.apply_event(claim_id, event_type, detail, email_id=None)`: append the event unconditionally, then apply the state if the transition is declared; on an undeclared transition leave the state alone and flag the claim naming both states and the event id. Return `{"applied", "state", "refused"}`.
- [ ] 2.2 Test: a legal transition writes the state; an illegal one records the event, leaves the state, and writes a flag naming both states; a stateless event records and moves nothing.
- [ ] 2.3 Test: `unclassified` and `confirmed_resolved` move no state — the same guarantees the old inline conditions gave, now from the event-type classification.

## 3. Route every writer through it

- [ ] 3.1 `claim_status.process_reply`: keep classification, correlation, reference/Sr learning and detail-building; hand the state change to `apply_event` and drop its own `status` UPDATE (including the `unclassified` special case, now redundant).
- [ ] 3.2 `claim_status.mark_sent`: append `sent` per claim in the submission via `apply_event`, preserving the existing `WHERE draft_id = ?` group semantics.
- [ ] 3.3 `invoice_matching`: `matched` (`:416`), `absorbed` (`:865`), `unmatched` (`:892`) go through `apply_event`; the field writes (`invoice_data`, `matched_email_id`) stay where they are.
- [ ] 3.4 `claim_forms`: both `drafted` writes (`:513`, `:839`) go through `apply_event`; `claim_file_path`/`draft_id` stay.
- [ ] 3.5 `claim_forms.split_between_pets`: its draft reset becomes a `matched` event on each affected claim, which is why `drafted`→`matched` is in the table.
- [ ] 3.6 Test: creating a claim, matching, drafting, sending and settling it produces one event per transition, in order, with no direct status write anywhere in the path.
- [ ] 3.7 Guard test: no `SET status` on `vet_claims` exists outside `claim_status.py` — a mechanical check, because this repo has a documented history of a convention being broken four times in one session.

## 4. The projection

- [ ] 4.1 Add `claim_status.project_state(claim_id)` (and a bulk form for the tick): fold events in `created_at, id` order, skipping reverted and stateless events, applying state events subject to `TRANSITIONS`.
- [ ] 4.2 Test the projection against **real sequences pulled from the live log** (read-only): claim #8's `acknowledged` → `info_requested`, claim #21's `acknowledged` → `approved` → `settled`, claim #2's single-event history after the 2026-07-28 repair.
- [ ] 4.3 Test: a reverted event is skipped; an event whose transition is illegal from the replayed state is skipped without aborting the fold.

## 5. Shadow mode (Phase 1 ends here)

- [ ] 5.1 Add the tick-time comparison: project every claim, compare to the stored column, log disagreements at **WARNING** with claim ids (not ERROR — ADR-0015 reserves that for "Justin must act", and he cannot fix a projection bug).
- [ ] 5.2 Expose `state_projection_disagreements` on `/health`.
- [ ] 5.3 Test: an injected disagreement is reported and **no** claim's status or flag is modified by the comparison.
- [ ] 5.4 Run both suites; deploy from the `deploy` worktree; confirm `/health` and that the count is present.
- [ ] 5.5 Record the disagreement count on the live DB before the backfill — it is expected to be non-zero and to name exactly the claims whose transitions we caused. Write down which claims and why.

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
