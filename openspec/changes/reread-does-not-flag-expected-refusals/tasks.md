## 1. Thread the replay flag

- [x] 1.1 Added `replaying: bool = False` to **both** `claim_status.process_reply` and `claim_status.apply_event`. Default `False`, so every existing caller keeps today's loud behaviour unedited. The design named only `process_reply`; `apply_event` is what actually writes the flag (see design.md's correction).
- [x] 1.2 `pipeline.poll_petcover_status` passes its existing `reread` through as `replaying`.

## 2. Suppress the flag write, never the event

- [x] 2.1 `apply_event` skips `_flag_claim` for a refused **transition** when replaying. The event and its detail are recorded exactly as today, and the refusal is still returned to the caller.
- [x] 2.2 Precedence revisited: `refusal_holds_the_flag = bool(outcome["refused"]) and not replaying`. During a replay nothing is being overwritten, so the finding lands.
- [x] 2.3 Confirmed: `poll_petcover_status` is the only production caller of `process_reply`.

## 3. Tests

- [x] 3.1 `test_a_replayed_refusal_is_recorded_but_does_not_flag_the_claim`.
- [x] 3.2 `test_an_ordinary_refusal_still_flags_the_claim` — the guard against fixing this by making refusals quiet everywhere.
- [x] 3.3 `test_a_replay_finding_reaches_the_flag_instead_of_losing_to_a_refusal` — claim #2's shape, asserting both directions (replay: finding wins; live: refusal wins).
- [x] 3.4 Suite green. Run with the main checkout's interpreter (`C:\Code\Me.OpenClaw\app\.venv`) — this worktree has no `.venv`. A fourth test was added beyond the plan: `test_a_replay_never_silences_a_defect`, because the first cut suppressed too much.

## 4. Verify against real data

- [x] 4.1 Replayed the live mail (`since=2026/07/24`, 37 messages checked) against a `src.backup()` copy — live file never opened read-write. **0 claims gained refusal text**, against 6 on the 2026-08-05 run. No claim's flag changed at all.
- [x] 4.2 Events still written with full detail, including the `needs manual link` unlinked ones. Nothing was suppressed from the log.
- [x] 4.3 `state_projection_disagreements` reads the event fold, not flags, and was 0 before and after deploy.

## 5. Out of scope, stated so it is not assumed

- [x] 5.1 Raised as an Open Question rather than swept — and Justin answered it on 2026-08-06: clear them. Done as a separate, explicit step after deploy, not as a side effect of this change.
