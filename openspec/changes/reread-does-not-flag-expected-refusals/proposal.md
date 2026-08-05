## Why

A deliberate re-read replays mail the log has already applied, so most of it is expected to be
refused: an acknowledgement from July, re-read against a claim that has since settled, is not a legal
transition and correctly moves nothing. What is wrong is that the refusal overwrites the claim's
`flag`, which is the surface Justin actually reads.

Live, 2026-08-05: recovering five approval letters that had never reached the claims service left
`refused settled -> acknowledged` text on **six claims** (#1, #2, #6, #7, #8, #13). On several of them
that text displaced the finding the re-read existed to produce — claim #2's "claimable subtotal not
recorded" never reached the flag column, because `process_reply` treats a refusal as the more serious
of the two and suppresses the settlement flag behind it. The noise looked like six new failures; it
was one expected consequence of asking for a replay.

## What Changes

- `poll_petcover_status(reread=True)` marks the events it produces as a replay, and a transition
  refused during a replay SHALL NOT be written to `vet_claims.flag`.
- The refusal is still **recorded as an event**, with the same detail it carries today. Nothing is
  hidden from the log — only the human-facing flag column is left for findings that are actually new.
- A settlement finding produced during the same replay reaches the flag column normally, instead of
  losing to a refusal that was expected.
- Refusals during **normal** polling are unchanged: there, a refused transition is genuinely
  surprising and the flag is how it becomes visible.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `claim-status-tracking`: a transition refused while replaying already-applied mail is recorded as
  an event but does not write the claim's flag; refusals during ordinary polling are unaffected.

## Impact

- `claim_status.process_reply` — needs to know whether it is replaying, and to skip the refusal-flag
  write when it is.
- `claim_status.apply_event` — already returns `refused`; no change expected to its own behaviour.
- `pipeline.poll_petcover_status` — passes the replay flag it already has as `reread`.
- No schema change, no new event type, no data rewrite. Six live claims currently carry this noise and
  are **not** cleaned up by this change; clearing them is a separate decision, since a flag is Justin's
  to dismiss.
