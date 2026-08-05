# Serial assignment by evidence, not by ordering

## Why

`_claim_for_sr` attached a Petcover serial to "the oldest-transaction claim not yet serialized", on
the reasoning that their serials run oldest-first. Nothing ever confirmed that, and it is now
measured: against Petcover's own status table of 2026-07-29 — the only document that states a
**treatment date per serial** — the heuristic was wrong on **all ten** serials we held.

It then cost real money in the open. On 2026-08-05, recovering the five approval letters that never
reached the claims service, an under-excess refusal for a **$55.74 arthritis** claim was attached to
the **$2,521.46 ALT workup** (claim #12) and moved it from `sent` to `below_excess`. Its real letter —
`DC1-26-5992` Sr 3, approved $2,521.46, paid **$1,638.95** — was left unlinked, so the largest single
recovery in the set did not land. Repaired by hand the same day.

The guess was never the whole problem. The guess being **written as a fact, with nothing recording
that it was a guess**, is what let it stay wrong across ten serials and three investigations.

## What changes

- **The amount the letter states decides which claim it belongs to.** Where exactly one claim awaiting
  a serial is worth what Petcover says they assessed — matched to the cent against its recorded
  claimable subtotal — that is the claim.
- **Where the stated amount matches nothing (or matches two things), no serial is assigned.** The
  event is recorded unlinked and flagged `needs manual link`, naming the figure and the candidates it
  considered. That is an existing, working surface: the dashboard lists unlinked events and
  `link_event` attaches one in a click.
- **The under-excess refusal letter now yields its figures.** It writes `Amount claimed:` without the
  `Total`, so `_APPROVAL_PATTERNS` matched nothing at all — which is precisely why the letter that
  broke claim #12 had no amount to route by and fell through to ordering.
- **The ordering heuristic survives only for letters that state no amount** (acknowledgements), and
  those already record `sr_assigned_by` so the log can tell a guess from a citation.

## What does not change

- No re-routing of historical rows, no rewriting of events. The nine serials corrected on 2026-08-05
  were corrected against Petcover's own stated treatment dates, by hand, with a backup.
- Acknowledgement correlation (`correlate_ack`) is untouched: choosing the *submission* is a different
  question from choosing which of its claims a serial belongs to.
- Petcover's condition per thread is not yet used as a gate. It would have caught the claim #12 case
  independently (`DC1-27-5628` is Arthritis; #12 is an ALT workup) and is the obvious next step, but
  the amount rule covers every failure in the live corpus and needs no new extraction.

## Impact

**BREAKING for automation, deliberately:** a letter we cannot place now produces an unlinked event
instead of a confident wrong link. Expect more manual links and fewer silent mis-attachments. In the
live corpus this affects one letter — `DC1-27-5628` Sr 4, whose $55.74 claim we do not hold.

Capability: `condition-thread-tracking`. Code: `claim_status._claim_for_sr`, `stated_claim_amount`,
`_APPROVAL_PATTERNS`, `process_reply`.
