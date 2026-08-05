# Post-mortem — the settlement card Justin could not read

**Date of investigation:** 2026-08-04. **Trigger:** Justin tapped Dismiss on a `REVIEW SETTLEMENT`
card for claim #2 while testing button plumbing, not knowing what it meant.

Not an ADR. An ADR is Justin's call and accepted ones are append-only. This is the incident record
for one change. Cross-references rather than restatements: **ADR-0011** (excess and policy-year
model), **ADR-0020** (event-level idempotency, and why the 2026-07-27 incident was not a duplicate
event), **ADR-0008** (append-only status events), **ADR-0007** (charge is a ceiling; the form carries
the claimable subtotal).

## What the card said

```
REVIEW SETTLEMENT
Claim #2 - The Shire Veterinary Ca... - $585.39
Aari - Arthritis; Raised ALT - 2026-06-19 (46d ago)
Blocks: a paid-vs-expected difference is unreviewed
```

The flag behind it, preserved verbatim in `claim_status_events` id 58 under `detail.dismissed_flag`:

> `settlement mismatch — we expected $430.74, Petcover paid $22.75 (fresh $150 excess this policy year) — review`

Four numbers, three of them wrong or misleading, and no statement of what Justin was supposed to do.

## The hypothesis we went in with, and why it was wrong

The working theory was mis-routing: a letter had reached the wrong claim. It had support.
`_already_recorded`'s own docstring cites ADR-0020 — the 2026-07-27 incident "was never duplicate
events, it was a correctly-deduplicated event reaching the wrong claim" — and event 34 records claim
#2 having previously learned a truncated reference `DC1` from a U+2010 hyphen, from a letter that
belonged to claim #8's thread.

**The letters refute it.** Read read-only through the running container on 2026-08-04, all five
`Claim Approval` letters cite a reference and a `Treatment number` that match exactly the claim they
were recorded against:

| event | email id | letter cites | recorded against | claim's stored ref / sr |
|---|---|---|---|---|
| 18 | `19f92024cabed6ac` | `DC1-27-5628` Treatment 2 | #21 | `DC1-27-5628` / 2 |
| 21 | `19fa131a64fa488b` | `DC1-27-5628` Treatment 5 | #7 | `DC1-27-5628` / 5 |
| 22 | `19fa131b1857eb00` | `DC1-27-5628` Treatment 6 | #6 | `DC1-27-5628` / 6 |
| 54 | `19fb1035e89a8360` | `DC1-26-5992` Treatment 1 | #8 | `DC1-26-5992` / 1 |
| 55 | `19fb10367f2443da` | `DC1-26-5992` Treatment 2 | #2 | `DC1-26-5992` / 2 |

Five for five. `extract_sr` handles the `Treatment number: N` form (added 2026-07-24, `d7b9812`,
after a live-caught routing bug of exactly this kind). Routing is not the defect here.

This is worth recording as a method note: the mis-routing story was reached from the repo's own
documented failure modes plus the shape of the data, and it was wrong. It cost nothing because the
letters were read before anything was written. Reading them first is the whole lesson — `CLAUDE.md`'s
rule about the product's own artifacts over inference from this codebase's shape applies to Petcover's
letters as much as to a third-party API.

## How a letter's numbers ended up describing a different claim

The letters route correctly and their *amounts* still do not describe the claim they route to:

| claim | our invoice | letter says claimed |
|---|---|---|
| #21 | $44.75 | $35.00 |
| #7 | $132.50 | $446.50 |
| #6 | $351.50 | $45.00 |
| #8 | $446.50 | $351.50 |
| #2 | $580.74 (no `claimable_amount` key) | $35.00 |

Every amount Petcover states matches some real invoice of ours to the cent — $446.50 is claim #8's,
$351.50 is claim #6's, $45.00 is #18's or #22's, $35.00 is #19's or #1's. They hold our documents.
What disagrees is which document sits under which serial.

**We cannot determine the correct mapping and should not try.** The amounts cross thread boundaries —
$446.50 is a `DC1-26-5992` invoice assessed under `DC1-27-5628` — so no permutation of serials within
a thread explains it. Two of our invoices are $35.00 and two are $45.00, so the amounts alone are not
unique keys. And the approval letter states no invoice number and no treatment date. This is the
question for Petcover, in `petcover-questions.md`, and it is the reason this change proposes no
auto-correction: re-routing a settlement is a money-affecting write.

What our side contributed: `_claim_for_sr` assigns a serial to "the oldest-transaction claim not yet
serialized" — a documented heuristic over Petcover's ordering, not a fact they told us. Where our
serial→claim map is wrong, correct routing lands a letter on the wrong claim and the routing code is
blameless. That is a real second-order risk and it is not fixed by this change either; it is fixed by
Petcover telling us which invoice each serial covers.

## Why the expected figure was also wrong — the bigger defect

`_validate_settlement` computes `claimable − $150` and compares it to Petcover's paid amount. Every
approval letter states its own full breakdown, and Petcover's arithmetic is exact:

| claim | claimed | fixed excess | age contribution | paid | (claimed − excess) × 0.65 |
|---|---|---|---|---|---|
| #21 | $35.00 | $0.00 | $12.25 [35%] | $22.75 | $22.75 |
| #7 | $446.50 | $49.26 | $139.03 [35%] | $258.21 | $258.21 |
| #6 | $45.00 | $0.00 | $15.75 [35%] | $29.25 | $29.25 |
| #8 | $351.50 | $150.00 | $70.53 [35%] | $130.97 | $130.97 |
| #2 | $35.00 | $0.00 | $12.25 [35%] | $22.75 | $22.75 |

There is a 35% **Age Contribution** that our expectation does not model, so our expectation is
structurally about 35% too high and flags a mismatch on letters that are arithmetically perfect.
Claim #8's live flag says *expected $296.50, Petcover paid $130.97*; against the letter's own stated
$351.50 and $150.00, $130.97 is right to the cent.

The proximate cause is a decision recorded in `openspec/specs/settlement-validation/spec.md` on
2026-07-24, which dropped an idea to "reverse-engineer Petcover's own internal excess mechanics
(observed varying between $0 and $105 across real letters)". The observation was accurate and
attributed to the wrong term. Across five letters the **fixed excess** is what varies ($0.00, $0.00,
$0.00, $49.26, $150.00). The **age contribution** is a constant 35%, printed in the letter's own text.
In choosing not to *infer* their mechanics, we also stopped *reading the numbers the letter states* —
and `extract_approval_amounts` was already capturing three of them into the event detail, where
nothing used them.

Three supporting defects, all verified:

1. **Read-before-learn.** `_validate_settlement` runs at `claim_status.py:867` and reads
   `claim["petcover_reference"]` at `:949`; `process_reply` writes the learned reference at
   `:876-878`, later in the same loop iteration. On the letter that teaches the reference, the
   thread-sibling query runs with `reference = None` and finds nothing. Live: claim #2 was told
   "fresh $150 excess this policy year" one second after claim #8's letter in the same thread and
   policy year stated `fixed_excess_stated: 150.00`.

2. **The claimable subtotal was substituted with the invoice total.** Three sites spell
   `invoice.get("claimable_amount")` then fall back to `invoice.get("amount")`:
   `_validate_settlement:923-925`, `visit_ledger:1243-1245`, `claim_detail:1644`. Claim #2's
   `invoice_data` has no `claimable_amount` key, so $430.74 = $580.74 − $150.00 was computed from a
   number never submitted as a claimable subtotal. Five live claims are in that position (#2, #16,
   #18, #19, #21). This is the shape `app/openclaw/CLAUDE.md`'s collapse table exists to catch — a
   rule held by convention across N callers, with no guard — and the fix is that table's fix: one
   accessor, one named guard test.

3. **The stored events under-record what the code can read.** None of the five `approved` events
   carries `age_contribution_stated`, though today's `extract_approval_amounts` extracts it from
   those same five emails when re-run (5/5, 2026-08-04). The pattern landed 2026-07-24 20:27 +1000
   (`97ad49d`); events 54 and 55 were written 2026-07-30, six days later, so the deployed build was
   behind. `_already_recorded` blocks a re-read from backfilling, correctly — so the gap is permanent
   in the log and belongs in the backlog, not in a repair script.

## Why the mismatch was invisible after dismissal

By design, and the design is right for one case and wrong for the other.

`_validate_settlement` has exactly one caller (`claim_status.py:867`, inside `process_reply`).
`_already_recorded` deliberately blocks a re-read from re-running the flag write, precisely so a
dismissed mismatch cannot be resurrected. Nothing un-dismisses. Only a new letter carrying a paid
amount flags again.

For an arithmetic difference Justin has personally checked, one-way dismissal is correct — otherwise
every mail re-read renags him. For a question outstanding with Petcover it is wrong, and event 58 is
the proof: claim #2's whole finding is now prose inside `detail.dismissed_flag`, the claim's `flag` is
NULL, and no surface says a question is open. The append-only log preserved the evidence, which is
ADR-0008 working; what was missing is anything that keeps an unanswered question *in front of him*.

Compounding it: the card's `Blocks:` line came from
`"confirm_resolved": ("Confirm resolved", "Petcover is waiting on you")`. Justin's own words —
"It also said Petcover is waiting on me but that doesn't seem to be the case if I have to just check
the payment discrepancy". A card that misnames the waiting party and states no numbers is a card that
gets dismissed, and it was.

## The process, next time

1. **Read the third party's own artifact before theorising.** The five letters took one read-only
   command and demolished the mis-routing hypothesis. Every number needed to diagnose this was in
   them, including the 35% and the exact excess.
2. **Never compute an expectation against a supplier's figure without using the figures they state.**
   `extract_approval_amounts` had captured three of four terms for weeks; `_validate_settlement`
   consulted none. Captured-and-unused is worse than uncaptured — it reads like the check has the
   data.
3. **One accessor, one declared shape, one named guard test.** The convention-across-N-callers shape
   is in `app/openclaw/CLAUDE.md`'s table with its own verdict: without the mechanical guard it
   decays, and every row in that table was a convention before it became an incident. The guard here
   is `test_no_module_substitutes_the_invoice_total_for_a_claimable_subtotal`, self-checking so a
   broken matcher fails rather than passes vacuously.
4. **A card must name the waiting party and show its numbers.** `owed_by` on `info_requested` already
   established that never defaulting a party is the rule, because naming the wrong one means the
   chase never happens. The same applies to a review task: "nobody is waiting, check these two
   numbers" is actionable; "Petcover is waiting on you" with no numbers is not.
5. **Distinguish "I checked this" from "I asked about this".** A dismissal that clears the only
   surface is right for the first and wrong for the second.
6. **Don't auto-correct money.** Every amount here matches some real invoice of ours, which is
   tempting and not sufficient — amounts are not unique, they cross threads, and the letters carry no
   invoice number. Ask, then let Justin decide.

## Corrections, after implementation and a full mailbox sweep (2026-08-04, same day)

Everything above stands as written except where corrected here. The trigger was mundane: before
implementing, the mailbox was swept for every Petcover message rather than re-reading the five
letters the investigation already knew about.

**1. There were never five approval letters. There are ten.** Five more sat unread by this
investigation: `DC1-27-5628` Tr 7 and Tr 8, `DC1-26-5992` Tr 3 and Tr 4, `DC1-26-5993` Tr 1. The
figures table above is correct for its five and incomplete as a picture of the account.

**2. The formula in "Why the expected figure was also wrong" is wrong for one of the ten.** That
section's column `(claimed − excess) × 0.65` reproduces all five letters it was derived from, and
`DC1-27-5628` Tr 8 breaks it: claimed $580.74, **non-claimable $135.00**, paid $289.73. The correct
form deducts the non-claimable amount first — $(580.74 − 135.00) × 0.65 = $289.73 — and Tr 8 is the
only letter in the mailbox where the two forms differ, by $87.75. Five letters were enough to find
the age contribution and not enough to fix the formula.

**3. A fifth deduction line was never mentioned:** `Percentage Excess: $0.00 [0%]`, printed on all
ten letters. Zero every time so far, which is exactly why it went unnoticed while three of us read
these letters closely.

**4. "Three sites spell `claimable_amount` then fall back to `amount`" undercounts by two.** The
guard test found `dashboard_lists` (falling through to *Petcover's own* stated figure, inside the row
whose job is to compare against it) and `claim_forms.apportion_between_pets` (splitting the invoice
total between pets). The mechanical guard found in one run what careful reading had missed twice,
which is the argument for writing it.

**5. The `Blocks:`-line attribution is wrong.** This document says the card's `Blocks:` line "came
from `confirm_resolved`"; the card quoted at the top reads *a paid-vs-expected difference is
unreviewed*, which is `dismiss_mismatch`'s phrase. Both were mis-worded and the fix covers both, but
the quoted card was not the one saying "Petcover is waiting on you". Justin's report and the
`_ACTION_META` entry are both real; the causal link between them in this document was assumed.

**6. The bigger finding this investigation could not have reached from claim #2.** No `approved`
event has been recorded since #55 on 2026-07-30, against a mailbox holding ten approval letters. Five
letters (28/07–03/08) were consumed by `gmail_ingest` as assistant *tasks* — `processed_emails` is a
single dedupe gate shared with `poll_petcover_status`, and whichever poller runs first wins
permanently. Roughly $2.6k of settlements, including one claimed at $2,521.46 and paid at $1,638.95,
reached no claim at all. Claim #13 carries `DC1-27-5628 sr 7`, the exact serial the 28/07 letter
cites, so this was never routing and never matching: the letter never arrived.

That reframes the "how a letter's numbers ended up describing a different claim" section too. The
amounts that looked unattributable are turning up under later serials — `$580.74`, claim #2's invoice
total, is assessed under `DC1-27-5628` Tr 8 with `$135.00` deemed non-claimable, and $135.00 is
exactly the "Blood Profile (Chem 10 only)" line on that invoice. Petcover's serial ordering still
disagrees with `_claim_for_sr`'s oldest-transaction heuristic, and that remains a question for them,
not a thing to correct here.

**7. What the method note should say.** The investigation's own lesson — read the third party's
artifact before theorising — was applied to five letters and stopped there. Reading *all* of them cost
one query and overturned a formula, added a term, and surfaced a delivery failure. "Read the artifact"
is not done until the set is bounded.

## The mapping is not undeterminable. Petcover sent it, and we already had it.

**This supersedes "We cannot determine the correct mapping and should not try" above.** That paragraph
is wrong, and the evidence refuting it was in the mailbox before this investigation started.

On **2026-07-29 10:56 AEST** Petcover answered a request Justin made on 25/07 with a table of every
claim lodged since 2023: claim reference, **Sr number**, **treatment date**, date advised, amount
payable, loss cause and status. The treatment date per serial is exactly the field the approval
letters omit, and it resolves the mapping completely.

Against it, **every serial we hold is on the wrong claim**:

| serial | treatment date | Petcover's amount | our claim with that date | we assigned it to |
|---|---|---|---|---|
| `DC1-26-5993` Sr 1 | 2026-02-23 | $944.50 | **#13** | (unassigned) |
| `DC1-27-5628` Sr 8 | 2026-06-19 | $580.74 | **#2** | (unassigned) |
| `DC1-26-5992` Sr 1 | 2026-05-18 | $351.50 | **#6** | #8 |
| `DC1-27-5628` Sr 7 | 2026-04-17 | $132.50 | **#7** | #13 |
| `DC1-27-5628` Sr 6 | 2025-07-28 | $45.00 | **#22** | #6 |
| `DC1-27-5628` Sr 5 | 2026-04-02 | $446.50 | **#8** | #7 |
| `DC1-26-5978` Sr 1 | 2025-08-08 | $44.75 | **#21** | #22 |
| `DC1-27-5628` Sr 3 | 2025-09-26 | $45.00 | **#18** | #19 |
| `DC1-27-5628` Sr 2 | 2025-09-11 | $35.00 | **#19** | #21 |

Every letter's stated `claimed_amount` matches its **true** claim's invoice to the cent, seven for
seven. So the section above titled "How a letter's numbers ended up describing a different claim" has
its answer: they did not. Petcover assessed our invoices correctly; we filed their serials against the
wrong claims, and the disagreement was ours the whole time.

Two specific corrections to that section's reasoning:

- *"The amounts cross thread boundaries — so no permutation of serials within a thread explains it."*
  True, and the wrong conclusion was drawn from it. The correct mapping **also** crosses thread
  boundaries: claim #22 belongs to `DC1-27-5628` Sr 6 while we filed it under `DC1-26-5978`, and #21
  is the reverse. Permutation-within-a-thread failed because the error is not within a thread.
- *"The approval letter states no invoice number and no treatment date."* Correct about the letter,
  and it hid the fact that a different Petcover document states the treatment date for every serial.
  The letters were re-read three times; the answer was in a reply nobody re-opened.

`_claim_for_sr`'s "oldest-transaction claim not yet serialized" heuristic is now measured rather than
suspected: **0 for 10**. It is documented as an inference, and the log did not distinguish an inferred
link from a cited one — it now records `sr_assigned_by` when the serial was guessed.

**Why the app never saw the table.** `gmail_client.full_message_text` extracted 198 characters from
it. The mail has no `text/plain` part, and `_message_text` fell through to Gmail's `snippet` — a
truncated body that reads exactly like a short email, with nothing saying it had been truncated. The
one document that would have prevented every wrong conclusion in this post-mortem was in the mailbox,
was fetched, and was silently reduced to a greeting. That is the "a silent result is not a finding"
rule failing on our own side of the boundary rather than the supplier's.

**And it changes the recovery plan.** Re-reading the five lost letters routes them by the same 0-for-10
heuristic, so it would attach real settlements to wrong claims. The serial map wants correcting first,
or the re-read wants doing in the knowledge that its links need fixing afterwards. Either way it is
Justin's call — re-routing settlements moves money — and this document should stop implying the
information to decide it does not exist.
