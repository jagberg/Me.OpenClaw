## Context

`claim_status.unanswered_vet_requests()` lists claims with an open, unresolved information request owed by the vet (clinic, requested document, days outstanding/remaining). Today the only way one leaves that list is Justin's explicit "confirm resolved" tap. Justin follows up with vets by his own email, outside the app, so the app has no visibility into that conversation at all today. This session's manual Gmail check (four claims, one real reply) found the reply wasn't a plain yes/no — the vet said the notes were sent **to Petcover directly**, weeks ago — which isn't "resolved" (we still don't know Petcover has it) and isn't "still owed by the vet" either (there's nothing more to ask them). Justin's own words after seeing it: he'd need to follow up with **Petcover**, not the vet, if nothing happens next.

## Goals / Non-Goals

**Goals:**
- Correlate a reply to the right claim when one clinic owes several (Kings Vet owed both claim #6 and claim #8 simultaneously, confirmed live) — never resolve the wrong one, and never guess when correlation is ambiguous.
- Interpret the reply's *content* into one of a small, closed set of outcomes, and map each to the state that already exists to represent it — reusing `confirm_resolved` and the existing `owed_by` field rather than inventing a parallel state machine.
- Never guess: an outcome the classifier can't confidently place stays exactly as unresolved as before.

**Non-Goals:**
- Not verifying receipt with Petcover — that remains Justin's own manual follow-up, explicitly. This change surfaces *that he needs to*, it doesn't do it.
- Not changing how Petcover's own replies are classified or correlated (`poll_petcover_status`'s three-tier router) — this is a parallel, independent poller over a different sender population.
- Not building a new state machine parallel to `owed_by` — the third outcome (sent to Petcover) is a new *value* of the existing field, not a new mechanism.

## Decisions

**Correlate by clinic email address + reference/Sr in the subject or thread, not "oldest open ask for this clinic."** A clinic can owe more than one claim at once (Kings Vet: claim #6 DC1-26-5992 Sr1 and claim #8 DC1-27-5628 Sr5, both open simultaneously, confirmed live). Justin's own follow-up subjects already carry the Petcover reference and Sr ("Re: Petcover claim for Ari - DC1-26-5992 sr.1", observed live) — the same signal the reply itself echoes back (`Re: <same subject>`). Match the reply's subject/thread against each of the clinic's open claims' `(petcover_reference, petcover_sr)`; exactly one match resolves it, zero or multiple matches leave it open for Justin.

**Content is interpreted only after correlation succeeds, into a closed set of outcomes, each mapped onto vocabulary the app already has:**
- **Provided** → `claim_status.confirm_resolved`, unchanged — the vet answered the ask, towards us, done.
- **Sent to Petcover directly** → a new `info_requested` event on the claim with `owed_by: "petcover"`. This is the live case found this session: the vet's job is done from their side, but the claim isn't resolved — Petcover confirming they have it is the open question now, and that's specifically Justin's to chase, not the app's. `owed_by` already exists precisely to answer "who does Justin need to act on" (CLAUDE.md: "vet asks the vet as often as Justin ... naming the wrong party is how the chase never happens") — a third value is the smaller addition than a second field or a new event type, and every reader that already keys off `owed_by` (labels, the vet-nudge list) gets the right answer for free once it knows the new value exists.
- **Can't find it / declined** → no event; leave `owed_by: vet` exactly as today, but append the vet's stated reason as a note (mirrors the settlement-clarification-email pattern: visible, not silent, but not a resolution either).
- **Unclear / doesn't answer the ask** → nothing happens. Same discipline as `_pair_identifies_claim`'s "reply never named this claim, leave it untouched" in the sibling settlement-clarification feature: an unconfident classification is not evidence of anything.

Alternative considered and rejected: treat any reply from the owing clinic as sufficient regardless of content (this proposal's first draft). Superseded — the one real reply found live was exactly the case that draft would have gotten wrong: calling it "resolved" would have hidden that Petcover was never actually confirmed to have received anything.

**`gmail_ingest` exclusion is scoped to clinics with a currently-open ask, not all of `vet_contacts`.** Petcover's sender list is small and fixed, so CLAUDE.md's existing carve-out excludes it outright. A vet clinic is neither: most of a clinic's mail has nothing to do with an open Petcover request, and Justin would still want it as a task if that's ever relevant. Excluding only senders with a currently-open unresolved vet-request — recomputed each poll, same as `PETCOVER_STATUS_SENDERS` is checked each poll — stops being excluded the moment the request resolves.

**New poller runs before `gmail_ingest.poll_once` in the tick**, mirroring `poll_petcover_status`'s position — whichever poller reads a message first is the one whose marking rule applies to it under the shared `processed_emails` gate.

## Risks / Trade-offs

- **The classifier misreads "sent to Petcover" as "provided," or vice versa.** Mitigated by keeping the outcome set small and each outcome's action reversible by Justin (a wrongly-resolved claim can still be revisited; a wrongly-`owed_by:petcover` claim just sits waiting, visibly, until he looks) — never a money-affecting write, unlike settlement figures.
- **Reference/Sr matching misses a reply that doesn't quote the subject.** Falls through to "no match, stays open" — the existing, safe default.
- **A new `owed_by` value needs every existing reader updated** (labels, vet-nudge list exclusion) — CLAUDE.md's own gotcha names this as the exact failure mode of getting `owed_by` wrong; the label map is the one place to check, by construction, since `owed_by` is already a single declared field, not three independently-maintained booleans.

## Migration Plan

Purely additive: a new poller function, a narrower `gmail_ingest` exclusion check, one new `owed_by` value plus its label, and one new scoped `llm.extract()` call. No schema change, no backfill — existing open vet-requests become eligible on their next reply, same as if Justin had read it himself.

## Open Questions

- Exact tick position and function name for the new poller (`pipeline.poll_vet_replies`, proposed) — implementation detail, not a design fork.
- Whether "sent to Petcover" claims need their own dashboard surfacing (a "chase Petcover" list) versus just a distinct label on the existing vet-nudge-adjacent view — defaulting to the latter (smaller diff) unless Justin wants the former once he sees it live.
