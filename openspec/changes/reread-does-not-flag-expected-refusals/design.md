## Context

`poll_petcover_status(reread=True)` exists so a classifier or extraction fix can be applied to mail
already ingested — without it, every such fix only helps the *next* letter. It is safe to run because
`process_reply` skips any (email, claim, event) triple already logged, so a replay records only what
is genuinely new.

What it does not skip is the transition machine. An acknowledgement from July, re-read against a claim
that has since settled, is not a declared move: `apply_event` records the event, refuses the
transition, and `process_reply` writes `refused settled -> acknowledged …` to `vet_claims.flag`. That
is the correct behaviour when a live letter arrives out of order — it means something surprising
happened. During a replay it means nothing at all happened, and it is guaranteed to happen on every
claim whose state has moved on since the mail was first read.

Live consequence, 2026-08-05: six claims (#1, #2, #6, #7, #8, #13) ended a recovery run carrying
refusal text. Worse than noise — `process_reply` deliberately prefers a refusal over a settlement
flag ("A refusal already flagged this claim, and it is the more serious of the two"), so on claim #2
the finding the replay produced, `claimable subtotal not recorded`, never reached the column at all.

Constraint from the hard rules: **failures stay visible**. Nothing here may stop *recording* a
refusal; the event and its detail are the audit trail (ADR-0008), and this change must not touch them.

## Goals / Non-Goals

**Goals:**
- A replay stops writing expected refusals to the one column Justin reads.
- Findings a replay genuinely produces reach that column instead of losing to those refusals.
- The refusal stays in the append-only log, unchanged.

**Non-Goals:**
- Cleaning up the six claims already carrying this noise. A flag is Justin's to dismiss; a change that
  silently wipes flags is the invisible-failure pattern the hard rules forbid.
- Any change to refusals during ordinary polling, where a refused transition is genuinely surprising.
- Any change to `apply_event` or to `TRANSITIONS`. The refusal is right; only its side effect on the
  flag column is wrong.

## Decisions

### Decision 1 — thread the replay flag through to `apply_event`, don't infer it

**Corrected during implementation, 2026-08-06.** This section originally said the suppression belonged
in `process_reply`. It does not: `apply_event` is what writes the refusal, via `_flag_claim`, before
`process_reply` ever sees the outcome. So `replaying` is threaded one step further —
`poll_petcover_status(reread=…)` → `process_reply(replaying=…)` → `apply_event(replaying=…)` — and the
suppression sits at the single site that writes the flag. The original reasoning stands; only the
location was wrong, and it was wrong because the design was written from the caller's shape rather
than from the writer's.

Both functions gain an explicit `replaying: bool = False`, passed by `poll_petcover_status` from the
`reread` argument it already has. The alternative — inferring "this must be a replay" from the event
already existing, or from a module-level flag set by the poller — was rejected on both counts:
inference gets it wrong for a genuinely late-arriving letter (which looks identical), and a module
flag is invisible state that a second caller or a test would silently inherit.

Default `False` matters: every existing caller keeps today's behaviour without being edited, so
nothing becomes quiet by accident.

### Decision 2 — suppress the flag write, never the event

The refusal event is written exactly as it is today, including its detail. Only the
`_flag_claim` call in `apply_event`'s refused-transition branch is skipped when `replaying` is true,
and only for that branch: an unknown event type or an unknown backfill status is a defect whoever is
reading the mail, and stays flagged. So the log answers
"what happened during that replay" in full, and the dashboard answers "what needs Justin" without
being flooded by it.

### Decision 3 — a settlement finding during a replay now reaches the flag

Once the refusal is not competing for the column, the existing precedence (`settlement_flag and not
outcome["refused"]`) has to be revisited: during a replay the refusal is not written, so a settlement
finding should be. This is the actual repair — claim #2's missing finding was the symptom that
started this.

## Risks / Trade-offs

- **A replay could hide a refusal that was genuinely surprising.** → The event is still recorded with
  its full detail, and `/health`'s `state_projection_disagreements` is unaffected: it compares stored
  status against the event fold and does not read flags. A replay that produced an unexpected refusal
  is still discoverable, just not by the flag column.
- **`replaying` is a second parameter that a future caller could forget.** → It defaults to `False`,
  which is the loud behaviour. Forgetting it produces today's noise, not silence.
- **Six claims stay noisy after this ships.** → Named in the proposal as out of scope rather than
  quietly swept, because clearing a flag is a decision about what Justin has reviewed.

## Migration Plan

None. No schema change, no new event type, no backfill. Deploy is the ordinary path; rollback is a
revert, and an older build simply resumes writing the refusal flags.

## Open Questions

1. **Should the six existing noisy flags be cleared, and by whom?** They are historical and describe
   replays, not live failures. Clearing them is one statement, but it is Justin's call which of those
   six he has actually looked at.
