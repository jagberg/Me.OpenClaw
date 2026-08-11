# ADR 0031: Settlement clarification is one card reused twice, "Acceptable" is a terminal dismiss, and `awaiting_petcover_clarification` names only the period actually waiting on Petcover

- Status: accepted
- Date: 2026-08-12

## Context

Two settlement-validation failure modes (Check B assessment differences,
unrecorded-claimable-subtotal claims) sat flagged on the dashboard with no
path forward except Justin re-reading letters himself and no way to ask
Petcover directly. `openspec/changes/settlement-clarification-email/` designs
a settlement-review card (condition, line items or invoice PDF, submitted vs.
assessed figures) with two actions, and a way to batch several claims into
one consolidated email Petcover can reply to.

Three points were re-litigated across the design conversation and are worth
recording so they aren't re-opened by a future session:

1. **Is the pre-send review card the same UI as the post-reply "still
   unresolved" card, or two different things?** Early drafts of the design
   gave the post-reply case its own dashboard action and its own event type
   (`clarification_more_info_requested`).
2. **What does the "resolve" button mean, and is it reversible?** Named
   "Confirm resolved" initially, mirroring the existing `info_requested`/
   `suspended` pattern.
3. **What does `awaiting_petcover_clarification` name?** Initially wired to
   cover the review card's entire lifetime, including before any email to
   Petcover existed.

## Decision

**One card, two actions, reused at two points in the timeline — not two
states with two different UIs.** The flagged-claim list is the queue; a
reply that doesn't resolve things resurfaces the *identical* card rather than
a distinct one. This removed a redundant "trigger the batch" dashboard action
and a redundant event type (`clarification_more_info_requested` was dropped
entirely — the post-reply "still stuck" case writes a `flag` note, nothing
else).

**The button is "Acceptable," and it is a terminal dismissal, not a status
transition.** "Acceptable" means "I'm satisfied with what was paid, nothing
further happens on *this* settlement" — it reuses `claim_status.
dismiss_mismatch` (extended with an optional `reply_confirmation` param so an
auto-resolved reply records its own figure, not the original letter's),
which clears `vet_claims.flag` only. "Terminal" does not mean "this claim can
never be flagged again": a later, distinct letter carrying different figures
still opens an independent question and can flag again, exactly like today's
existing one-way dismissal semantics for Check A/B. This was surfaced
directly by Justin asking why the state name implied "waiting" when the
button was about *his* acceptability judgment, not Petcover's reply.

**`awaiting_petcover_clarification` names only the period genuinely spent
waiting on Petcover — never the Justin-review step itself.** It is entered
only when "More Info" queues a claim into an open clarification draft
(`claim_status.queue_clarification`), not while the review card is merely
showing on an unqueued flag. `TRANSITIONS["awaiting_petcover_clarification"]
= frozenset()` — nothing moves a claim out of this status via `apply_event`;
resolution (Acceptable, or an auto-resolved reply) clears the flag only,
mirroring exactly how `confirm_resolved` treats `info_requested`/`suspended`.

A related implementation bug this decision exposed: `_action_kind_from_row`
originally read raw `status == "awaiting_petcover_clarification"` to decide
whether a claim still needed to appear as a pending action. Because `status`
never reverts, a resolved claim kept surfacing as "waiting on Petcover"
forever on every surface except the dashboard's own settlement-review section
(which separately filtered on `flag`). Fixed by introducing
`claim_status._awaiting_petcover_clarification(claim)`, the one place that
combines `status` and `flag` into a single fact — both
`_action_kind_from_row` and `settlement_review_claims()` now ask it instead
of picking one column each. See `app/openclaw/CLAUDE.md`'s "one writer, one
declared table, one mechanical guard" table for the pattern this repeats.

## Alternatives considered

- **Two dashboard actions (a "review" card plus a separate "send batch"
  button).** Rejected: the flagged-claim list already is the queue; a
  separate trigger duplicates that list for no benefit and adds a UI step
  with nothing to decide.
- **A second event type for "reviewed, still unresolved after a reply."**
  Rejected: nothing downstream needs to distinguish it from the original
  flag state as an event — a `flag` note is enough, and inventing an event
  type per UI action is exactly the kind of taxonomy `app/openclaw/
  CLAUDE.md` warns against growing without a real need.
- **Naming the button "Confirm resolved" (matching `info_requested`/
  `suspended`'s existing wording).** Rejected: "resolved" implies the
  question was answered; "Acceptable" is honest that Justin is choosing to
  stop asking, which is a different claim than "we found out what happened."
- **Transitioning `status` back to something else (e.g. `settled`) on
  resolution, instead of leaving it at `awaiting_petcover_clarification`
  forever.** Rejected for this change: `dismiss_mismatch` is the one reuse
  point for both Acceptable and an auto-resolved reply, and it was built to
  clear `flag` only, matching `confirm_resolved`'s existing shape. Revisiting
  this is left open — see Consequences.

## Consequences

- A resolved claim's `status` column reads `awaiting_petcover_clarification`
  forever, which is correct for "was this state entered" but not for "is it
  still open" — any *new* code path reading `status` directly (rather than
  through `_awaiting_petcover_clarification` or `dismiss_mismatch`'s own
  `flag`-clearing) will reproduce the exact bug this ADR's fix closed. Read
  `status` for this value only through the shared accessor.
- A settlement event arriving on a claim already at
  `awaiting_petcover_clarification` (e.g. a corrected letter) has no declared
  transition out of that status — it is refused and flagged, same as any
  other undeclared transition, rather than moving the claim forward. This is
  an accepted, documented gap (not silently wrong, refusals are always
  visible), but it means a "later, distinct question" scenario is validated
  and flagged correctly while the status column itself stays stuck. Revisit
  if this proves to matter in practice.
- Whether a second Gmail `send()` exception (mirroring ADR-0030) gets added
  for the clarification email is deliberately **not** decided by this ADR —
  the implementation ships draft-only, and `openspec/changes/
  settlement-clarification-email/tasks.md` task 9.3 stays unchecked pending
  Justin's explicit sign-off, the same bar ADR-0030 itself required.
