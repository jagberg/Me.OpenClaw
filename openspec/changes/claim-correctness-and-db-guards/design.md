## Context

Six backlog items, grouped by sequencing rather than by feature. Two are money-affecting and blocked on Justin; four are guards and verifications deferred while those were unknown.

The state that matters going in:

- **Every serial we hold is on the wrong claim.** Measured 2026-08-04 against Petcover's own status table of 2026-07-29: 0 for 10 correct. The old rule assigned "the oldest-transaction claim not yet serialized". Meanwhile every letter's stated amount matched its *true* claim's invoice to the cent, 7 for 7. So "Petcover assessed a different amount" findings are almost certainly our mis-assignment, not theirs.
- **Redo has no implementation and two live requests.** Both fell through to `propose_create_task`, producing tasks #124 and #125. The premise of the second was also wrong — #7's draft existed the whole time (`r-7259758204005672288`, correct recipient, subject and three attachments); it only looked missing because both drafts were titled `Vet claim — Aari`.
- **ADR-0018's rule is convention and has failed twice.** The ADR left the enforcement unbuilt "worth building if this recurs". It recurred.

## Goals / Non-Goals

**Goals:**

- Correct the live serial map, once, with evidence, inside the container.
- Turn "redo" from a phrase into one named operation with visible confirmation.
- Make the ADR-0018 rule mechanical so the next inline `connect()` is rejected rather than regretted.
- Close three read-only verifications that need no decision.

**Non-Goals:**

- **The recovery re-read of the five lost approval letters.** Sequenced strictly after the remap; separate go-ahead. Named here only as a dependency.
- **Schema for one invoice spanning two Condition Threads.** Claim #2 under-reports by $87.75 and the real fix is a claim owning several `(reference, sr)` pairs. Out of scope; this change must not paper over it by linking event 91 to #2, which would make `_latest_settlement_detail` report $87.75 and be *worse*.
- **Re-extracting claims #16, #18, #19, #21** to recover claimable subtotals. Spends vision budget and rewrites settled claims for display only.
- Any change to which date anchors the excess/policy year. Two open questions about the same anchor are to be decided together, not one at a time.

## Decisions

**1. The remap is a one-off script run in the container, not a feature.**
Alternative considered: a general "reassign serial" tool reachable from Telegram. Rejected for now — the correction is a known finite set of nine claims against a table that no longer refreshes, and a permanent tool invites re-running a heuristic that was wrong ten times out of ten. The *spec* records that corrections are admissible on evidence; the *mechanism* stays manual until a second occurrence justifies more.

**2. The remap runs before anything else that writes claim links.**
The recovery re-read routes by the same 0-for-10 heuristic. Doing it first attaches real settlements to wrong claims and creates a second cleanup. Non-negotiable ordering.

**3. Prior links are superseded, not overwritten.**
The correction writes the new link and records the old one. A money-affecting rewrite with no trail is indistinguishable from a bug the next time someone reads it.

**4. Redo semantics are Justin's, and the build waits.**
Alternative considered: implement all three and let the user pick per invocation. Rejected — three verbs for one phrase is how the ambiguity got here, and the two live requests are consistent with more than one reading. One meaning, named in the confirmation. *Recommendation to put to Justin, not a decision taken here:* option 1 (rebuild the draft) fits both observed uses, since #7's figures and invoice were correct and only the draft was in doubt.

**5. `submission_id` stays derived unless redo can re-split a drafted batch.**
`submission-group-id` ships `S6+7` derived from claim ids, stable only because nothing can re-group a drafted batch. If the chosen redo can, the token must become a stored column — a hand-run `ALTER TABLE` plus backfill on the live DB. Decided as a consequence of decision 4, not independently.

**6. The DB guard is a harness hook, not application code.**
The failure mode is an agent writing `sqlite3.connect(<live path>)` inline in an ad-hoc script — application code cannot intercept that. A `PreToolUse` hook matching the live path without `mode=ro` catches it before execution. `scripts/query_db.py` ships alongside as the paved path, with the ADR's own objection noted: a helper alone is insufficient, which is why it is not the whole answer.

**7. The phantom DB is made to fail loudly rather than deleted.**
Deleting `C:\data\openclaw.db` converts silent wrong answers into "unable to open database file", which is the better failure — but nothing has verified what else writes to it. Raising on the resolution achieves the same outcome reversibly.

## Risks / Trade-offs

- **A wrong remap moves real money against the wrong claim.** → Backup first, dry-run diff reviewed by Justin, run in-container, old links recorded. Nine rows, enumerable in full before the write.
- **The evidence table is a one-off and covers serials only to 2026-07-29.** → Spec records that a later ambiguity means asking Petcover again, not re-deriving. No pretence of a feed.
- **Redo could be built for the wrong meaning.** → Blocked on the answer; the confirmation names the operation so a wrong choice surfaces on first use rather than silently.
- **The guard fires on legitimate commands and gets disabled.** → Scope it to the live DB path only, and ship the read-only helper at the same time so the paved path is easier than the workaround. A guard people route around is worse than none.
- **Bundling six items risks one blocker stalling five.** → Tasks are ordered so the four unblocked items (guard, assertions, three verifications) complete independently of the two gated ones.

## Migration Plan

1. Unblocked work first: DB guard, `_action_kind` assertions, three read-only verifications. None touch live data.
2. Get the two answers from Justin.
3. Remap: backup → dry-run diff → review → in-container write → verify.
4. Redo: build to the recorded semantics; close tasks #124 and #125.
5. Decide `submission_id` storage as a consequence; record "no change" if derived survives.
6. Only then unblock the five-letter recovery re-read, as separate work.

Rollback: the remap is a row-level rewrite with the prior values recorded and a backup taken — reversible. The guard and the assertions are revertible commits. Redo is new code behind a new verb.

## Open Questions

1. **Which of the three operations does "redo claim #N" mean?** Justin. Blocks the redo build and, through it, decision 5.
2. **Go-ahead for the remap**, and confirmation that the true map in `serial-assignment-by-evidence`'s post-mortem is the one to apply. Justin.
3. Does the guard belong in project settings or user settings? Project, if other repos should not inherit a rule about this DB — unverified which the user prefers.
4. Claim #21's $44.75 vs Petcover's $35.00: unknown whether our extraction or their assessment is wrong until investigated. Given 7-for-7 amount matching elsewhere, our extraction is the likelier suspect — but that is a prior, not a finding.
