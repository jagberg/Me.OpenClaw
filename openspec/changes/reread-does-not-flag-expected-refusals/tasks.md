## 1. Thread the replay flag

- [ ] 1.1 Add `replaying: bool = False` to `claim_status.process_reply`. Default `False` so every existing caller keeps today's loud behaviour without being edited.
- [ ] 1.2 `pipeline.poll_petcover_status` passes its existing `reread` argument through as `replaying`.

## 2. Suppress the flag write, never the event

- [ ] 2.1 In `process_reply`'s update, skip the `flag = ?` write for a refused transition when `replaying` is true. The event and its detail are recorded exactly as today.
- [ ] 2.2 Revisit the precedence: today a settlement finding loses to `outcome["refused"]`. During a replay the refusal is not written, so the finding SHALL be. This is the actual repair — claim #2's `claimable subtotal not recorded` never reached the column.
- [ ] 2.3 Confirm no other caller of `process_reply` exists that would need the flag (grep; `poll_petcover_status` is the only one today).

## 3. Tests

- [ ] 3.1 `test_a_replayed_refusal_is_recorded_but_does_not_flag_the_claim` — re-read an acknowledgement against a settled claim with `replaying=True`: event exists with its detail, `flag` stays NULL, status unchanged.
- [ ] 3.2 `test_an_ordinary_refusal_still_flags_the_claim` — the same input with `replaying=False` still writes the refusal flag naming both states. The guard against fixing this by making refusals quiet everywhere.
- [ ] 3.3 `test_a_replay_finding_reaches_the_flag_instead_of_losing_to_a_refusal` — claim #2's shape: a replayed approval whose transition is refused and whose settlement check produces a finding; the finding is what lands in `flag`.
- [ ] 3.4 Run the suite from `app/`: `./.venv/Scripts/python.exe tests/test_core.py`. This worktree has no `.venv` — record which interpreter was used.

## 4. Verify against real data

- [ ] 4.1 Re-run `poll_petcover_status(reread=True, since=…)` against a `src.backup()` copy of the live DB — never the live file — and confirm no claim gains refusal text, while any genuine finding still lands.
- [ ] 4.2 Confirm the refusal events are still written to the copy's `claim_status_events` with their detail intact.
- [ ] 4.3 Confirm `/health`'s `state_projection_disagreements` is unchanged by the run: it folds events and does not read flags, so this change must not move it.

## 5. Out of scope, stated so it is not assumed

- [ ] 5.1 The six live claims (#1, #2, #6, #7, #8, #13) already carrying refusal noise are **not** cleaned up here. Clearing a flag is a decision about what Justin has reviewed; raise it as an Open Question rather than sweeping it.
