## Context

Two settlement-validation failure modes sit unresolved on the dashboard with no path forward except Justin re-reading letters himself:

- **Check B assessment difference** (`settlement-validation` — `_check_what_petcover_assessed`): Petcover's `claimed_amount` doesn't match our recorded `claimable_subtotal`, and no invoice of ours matches their figure either, so the flag can't even name a likely cause.
- **Claimable subtotal not recorded**: a paid amount arrived for a claim whose `invoice_data` never got a `claimable_amount`, so Check B can't run at all.

Both are "we don't know what to compare against, and only Petcover can tell us" — as opposed to a Check A arithmetic dispute, which is a disagreement with Petcover's own math and stays a manual dispute, not something to ask them to re-explain.

This is additive scaffolding around the existing checks: it does not change what Check A/B compute, only gives Justin a way to close the loop when they can't resolve on our data alone.

## Goals / Non-Goals

**Goals:**
- Every open Check-B/unrecorded-subtotal flag surfaces **one settlement-review card** (condition, per-line costs when there are fewer than 5 line items, otherwise a link to the invoice PDF, submitted vs. assessed figures) with exactly two actions: **Acceptable** and **More Info**.
- The same card, same two actions, resurfaces if Petcover's reply doesn't resolve things cleanly — it is one card type used at two points in the timeline, not two different UIs.
- **Acceptable is terminal**: it means "I'm satisfied with what was paid, nothing further happens on this settlement." It permanently dismisses the flag (one-way, per `settlement-validation`'s existing dismissal semantics) — a genuinely new later letter with different figures opens a fresh question, it does not reopen this one.
- **More Info**'s behavior depends on where the claim currently sits:
  - Before any clarification email exists → it queues the claim into the open clarification-request draft to Petcover and the claim starts genuinely **waiting on Petcover** (`awaiting_petcover_clarification`).
  - After a reply arrived but didn't resolve things → there is nothing further to automate, so it just leaves a note that Justin looked and it's still unresolved; no new email, no new event type.
- A reply is auto-correlated to the right batch via Gmail thread id (not the existing reference/Sr/pet-condition routing — this is a direct reply to mail we sent) and auto-applies the same terminal dismissal as **Acceptable** when it unambiguously confirms the figure.

**Non-Goals:**
- No change to Check A/B's own math or to what counts as a mismatch.
- No automatic rewrite of any historical settlement/money row — settlement-validation's existing rule ("SHALL NOT re-route the settlement") holds. Both "Acceptable" and an auto-resolved reply dismiss the *flag*; neither ever adjusts `claimable_subtotal` or a paid amount.
- Arithmetic (Check A) differences are out of scope — those are disputes with Petcover's stated math, a different conversation than "please confirm what you assessed."
- Not deciding here whether this becomes a second permitted `send()` site (proposal flags it); design below assumes **draft** and calls out the one line that changes if Justin approves send.

## Decisions

**One card, two actions, reused at two points in the timeline — not two states with two different UIs.** Earlier drafts of this design put a separate "trigger the batch" dashboard action ahead of the review card and gave the post-reply "still stuck" case its own event type. Both were unnecessary: the flagged-claim list *is* the queue, and "still stuck after a reply" is not a new kind of thing, it's the same open question the card already represents. Collapsing to one card removes a redundant action and a redundant event type.

**`awaiting_petcover_clarification` names only the period we are actually waiting on Petcover — never the Justin-review step itself.** Before a claim is queued into a clarification draft, it's just today's existing Check-B/unrecorded-subtotal flag (no new state) wearing the new review card. The state is entered only when "More Info" queues the claim into an open draft, and it is genuinely descriptive from that point: we are waiting on them, not on Justin. This directly fixes a naming confusion from an earlier draft, where the review card was described as living *inside* `awaiting_petcover_clarification` — it doesn't; it precedes it (and can resurface after it, without changing the state's name or meaning).

**"More Info" queues into an open draft rather than sending one email per claim.** Clicking it ensures a single open `clarification_batches` draft exists (creating one via `drafts().create` if none is open), appends this claim's details to the draft body, records `clarification_requested`, and links the claim to the batch's thread id. Multiple claims accumulate in the same draft as Justin works through the flagged list; he reviews and sends the one consolidated email himself, matching the draft-only default. Alternative considered: send immediately per-claim — rejected, defeats the point of a *consolidated* email and removes Justin's last look before anything reaches Petcover.

**Reply correlation uses Gmail thread id, not the existing reference/Sr/ack routing.** The existing three-tier correlation in `claim-status-tracking` solves "which claim does this *unprompted* Petcover letter belong to." A clarification reply is different: it's a direct reply to a specific email we drafted, so Gmail's `threadId` (captured when the draft is created) deterministically identifies the batch. Falling back to the general router would risk a clarification reply about claim #8 getting attributed to claim #2 by the pet/condition heuristic.

**Figure extraction from the reply uses `llm.extract()`, not regex.** Every other classification in this codebase is regex/keyword on fixed Petcover template phrases (CLAUDE.md: "don't add LLM calls where regex/keywords work"). A clarification reply is free-form human prose answering our specific questions — there's no fixed template to match. Scoped tightly: extract `{claim_ref_or_serial, confirmed_amount}` pairs only, nothing else.

**Auto-resolve requires an exact match, otherwise the card resurfaces for Justin.** For each `{claim, confirmed_amount}` pair extracted: if it equals the claim's recorded `claimable_subtotal` to the cent, apply the same terminal dismissal as clicking Acceptable, recording the reply's figures. Anything else — no match, unparseable reply, amount close-but-not-exact — leaves the claim in `awaiting_petcover_clarification` and the review card resurfaces (now showing Petcover's reply figure too) rather than guessing.

## Risks / Trade-offs

- **Second `send()` site (if approved)** → mitigate by mirroring ADR-0030 exactly: named exception, test assertion updated, ADR written. Defaults to draft until Justin explicitly signs off in review.
- **LLM misreads a confirmed amount** → mitigate with the exact-match-only auto-resolve rule above; a misread simply fails to match and falls through to the manual card, it can't silently apply a wrong number.
- **Petcover answers only some of several batched claims in one reply** → each `{claim, amount}` pair is resolved independently; claims not addressed stay in `awaiting_petcover_clarification`.
- **Thread-id correlation breaks if Petcover starts a new thread instead of replying inline** → falls through to `unclassified`/unlinked, same visible-failure fallback the rest of the pipeline already uses; not silently dropped (`processed_emails` marking still applies normally since this sender is already carved out).
- **"Acceptable" read as "this claim is now locked, ignore anything else about it"** → mitigate by stating explicitly in the spec that a later, distinct letter with new figures is a new question, not a reopening — same rule `settlement-validation` already lives by for any dismissal.

## Migration Plan

Purely additive: new columns/table, new event type (`clarification_requested`), new `_ACTION_META` entry. No backfill — no claim has ever been in this state. Ship behind the existing pipeline cycle; first use is the next time Justin clicks "More Info" on a flagged claim.

## Open Questions

- Send vs. draft-only (proposal's flagged policy decision) — assumed draft here; one line (`drafts().create` → `send`) changes if approved, plus the ADR/test update.
- Scope of "eligible for the review card": this design includes Check-B assessment differences and unrecorded-claimable-subtotal claims on the same card type. Does Justin want the unrecorded-subtotal case handled separately (that one may not even need Petcover — it's often our own data gap)?
- Model/cost for the reply-extraction `llm.extract()` call — same backend as everything else (ADR-0009 fallback chain), no new decision needed, flagging only because it's a new LLM call site.
