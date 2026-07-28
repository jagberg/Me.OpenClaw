# ADR-0021: A claim's stored status is pipeline state; its label is who holds it

**Date**: 2026-07-28
**Status**: accepted
**Deciders**: Justin

## Context

`vet_claims.status` was doing two jobs: driving the pipeline (`pending_match → matched → drafted → sent → acknowledged → approved → settled`, with `info_requested` / `suspended` / `declined` / `below_excess` / `absorbed` off to the side) and supplying the word Justin reads. Two live failures made the conflict concrete.

**"Matched" told him what the software did, not who holds the claim.** Seven claims sat at `matched`; every one was Echo's, flagged `Bow Wow Insurance claim process not yet defined` — permanently blocked, no tap can clear them. They rendered identically to a claim waiting five minutes for a condition to be typed in. His question was "shouldn't it progress to another stage after this?" — it does (`matched → drafted` once a condition exists), but the word can't distinguish the two cases.

**"Suspended" made a request for information look like a dead claim.** Petcover's "Further Information Required" letter says *"Your claim will be suspended until we have the required information"*, and the classifier keyed on that word. `vet-info-request-chase` fixed the classification (`4a7fb6d`), but the resulting `info_requested` still reads the same whether the document is owed by Justin or by the vet — and the letter goes to the vet as often as to him, with him only on Cc.

**Nothing owned the wording.** It lived in three hand-synced copies — `claim_card._STATUS_LABELS`, `templates/index.html`'s `labels`, `templates/basic.html`'s `needs` — plus `pipeline`'s notify strings. The only link between them was a comment: "Mirrors templates/index.html's status_chip macro." `vet-info-request-chase` was about to add a fourth for `chase_vet`.

Constraints: live schema changes to existing tables need a hand-run `ALTER TABLE`, so a new column was off the table; `claim_status_events` is append-only (ADR-0008); and `claim_status._action_kind` already computes what a claim needs, so any second answer to that question would eventually disagree with the action list.

## Decision

**The stored status stays exactly as it is. The words are a separate, single mapping in `openclaw/status_labels.py`.**

- `LABELS` maps status → chip wording; `label(claim)` returns the display string; `needs(claim)` returns the "what's needed" line the phone-first `/basic` view wants. Two questions, one module — there is still one place wording lives.
- **A `matched` claim's label is derived**, not stored: `claim_status._action_kind_from_row` (the row-only half of `_action_kind`, extracted for this) gives `set_condition` → "Needs condition", `assign_pet` → "Needs pet", `blocked_insurer` → "Blocked: no claim process", anything else → "Matched". A label can never disagree with the action list, because both call the same function.
- **An information request is worded by who owes the document**, from the `owed_by` that `resolve_owed_by` already records on the event: vet → "More vet info required", Justin → "Petcover needs info from you", unrecorded → "Info requested". Never defaulting to "vet" mirrors `resolve_owed_by`'s own rule that it never defaults to Justin — naming the wrong party is how a claim quietly dies waiting for someone who was never asked.
- The word "suspended" appears in no information-request wording. A claim is labelled Suspended only when Petcover has actually suspended it.
- **Colours key off the status, not the label.** `claim_card._STATUS_COLOURS` was keyed by wording (`"Info requested"`, `"Below excess"`), so a rename would silently drop a chip to the default. Re-keyed.
- `history_rows` words its own rows, because the card renderer only receives that row and the derivation needs the claim's flag, pet, condition and `owed_by`.

## Consequences

- No schema change, no stored-status change, no event rewritten. Existing rows render under the new vocabulary immediately.
- The dashboard's word and `vet_claims.status` legitimately differ for a `matched` claim. That is the point; the claim detail view still shows the raw status. Do not "fix" one to match the other.
- A new state costs one edit. `vet-info-request-chase` task 5.5 was amended to add `chase_vet`'s wording here and only its colour to the renderer.
- The three deleted maps are named above, so a fourth reads as a regression rather than a convention. `test_one_status_vocabulary_no_second_map` asserts it.
- `status_labels` imports `claim_status`, so `claim_status.history_rows` imports it locally to avoid closing the cycle. One local import, commented.
- Relabelling fixed nothing already stored: claims #2 and #8 kept reading as suspensions until the separate 2026-07-28 data repair (`vet-info-request-chase` group 1) corrected the six mis-typed events.

## Alternatives considered

- **A `blocked` status stored on the row.** Needs hand-run DDL on the live DB, needs every state-machine consumer to learn a new state, and duplicates what `flag` already says.
- **Renaming the stored statuses** so status and label are one string again. ~40 call sites, plus the append-only event log, for a cosmetic win — and it would recreate the same conflict the first time a label needed to say something the state machine doesn't care about.
- **A Jinja filter.** Fixes the two templates and leaves the Telegram cards and notify text on their own copies, which is the actual problem.
- **A second copy of the "what does this claim need" logic inside the label module.** Cheaper to write, and guaranteed to drift from `pending_actions` — the two would then disagree about the same claim on the same screen.

## Amendment (2026-07-28) — the label names the document, not just the party

The decision stands. One thing it left implicit turned out to matter more than the part it stated.

"More vet info required" satisfies the rule above — it names the party the claim waits on — and Justin's response on reading it live was that it still could not be acted on. Petcover's letter says exactly what it wants, in a fixed template phrase: *"To assess your claim, we need a copy of / Consultation notes dated 18/05/2026"*. A clinic asked for a named document on a named date answers in one look; asked for "further information", it does nothing.

So the label names the document when the event records one, and still says who owes it:

| `owed_by` | document | label |
|---|---|---|
| vet | recognized kind | **Vet: consult notes needed** |
| Justin | recognized kind | **Consult notes needed from you** |
| either | absent or unrecognized kind | the wording in the Decision above, unchanged |
| unrecorded | either | **Info requested** |

Three consequences worth recording:

- **The chip shortens; the full phrase goes where there is room.** `status_labels.short_document` maps recognized kinds (consultation notes, itemised invoice, claim form, referral history) to a chip-sized noun phrase. A table chip and a Telegram card row cannot carry `dated 18/05/2026`, and the ledger's information density is a stated requirement of `dashboard-visit-ledger`. The full phrase reaches the claim detail, the action card and the weekly nudge.
- **The document does not replace the party.** The document says *what*, `owed_by` says *who*, and an unrecorded owner stays neutral no matter how specific the document is — naming a document without naming who owes it invites exactly the wrong chase.
- **`needs()` gained an action form** for the phone-first view: "Chase vet for consult notes", "Send Petcover the consult notes". Same module, still one vocabulary.

**What this cost, recorded because it is the useful part:** the extraction that feeds these labels was refactored from an inline regex shipped days earlier, and the refactor dropped that regex's consumption of the letter's own filler. Two real vet cover notes then yielded `information in order for us to review the` and `for us to review the claim Consult notes dated` as the document to chase. It was caught by a dry run against the real mailbox, *while the unit tests were passing*, moments before it would have been written to the live DB. A capture shorter than four characters is now rejected too, because one letter's body ends mid-sentence at "for us to review the" and left a stray `the`. The pattern carries both real phrasings as comments so the next refactor can see what they are for.
