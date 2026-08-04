## Context

A Telegram card told Justin `REVIEW SETTLEMENT / Claim #2 … / Blocks: a paid-vs-expected difference
is unreviewed`. He tapped Dismiss without knowing what it meant. Behind the card:

- `_validate_settlement` (`claim_status.py:902`) computes `claimable − $150` and compares it to
  Petcover's paid amount. It ignores every figure Petcover's letter states, three of which it already
  captures into the event detail.
- Petcover's letters state a 35% **Age Contribution**. Against five live letters re-read read-only on
  2026-08-04, `paid = (claimed − fixed_excess − non_claimable) × 0.65` holds to the cent, 5/5. So the
  app's expectation is structurally ~35% too high and mismatches even when Petcover is exactly right.
- The claimable subtotal is read with a silent `claimable_amount → amount` fallback in three
  places. Claim #2 has no `claimable_amount` key, so its "expected $430.74" came from the $580.74
  invoice total.
- The routing hypothesis is refuted. Every one of the five letters cites a reference and a Treatment
  number that match the claim it was recorded against exactly. `extract_sr` handles `Treatment
  number: N` correctly (added 2026-07-24, `d7b9812`).

What genuinely does not reconcile is `claimed_amount` versus what we submitted, and it cannot be
resolved from data we hold: every stated amount matches some real invoice of ours exactly, but the
amounts cross thread boundaries, and the approval letter carries no invoice number and no treatment
date.

Constraints that shape everything below: never send mail; never write the live DB from the host;
failures stay visible; `openspec/specs/dashboard-visit-ledger` carries a prior decision against an
"invented age-contribution percentage"; ADR-0011 (excess/policy-year model), ADR-0013 (transaction-date
bucketing), ADR-0008 (append-only events), ADR-0020 (event-level idempotency did not fix the
2026-07-27 incident).

## Goals / Non-Goals

**Goals:**
- Separate "is Petcover's arithmetic right" from "did Petcover assess what we submitted". They have
  different answers, different audiences and different next steps.
- Never compute an expectation from a number that was not submitted.
- Make every action card say who is waiting on what, and show the claim amount and expected payment.
- Leave a mechanical guard, not a promise, for each defect class.
- Give Justin a short, concrete list of questions to put to Petcover.

**Non-Goals:**
- **No auto-correction of historical rows.** Re-routing a settlement moves money. It is Justin's call
  after Petcover answers.
- **No change to the dashboard's estimate** (`_apply_excess_and_cap`). See Decision 3.
- No backfill of the five under-recorded `approved` events, and no re-read of Petcover mail to
  produce one. `_already_recorded` blocks it by design and ADR-0020 records why re-reading mail is
  the wrong tool.
- No new table, no `ALTER TABLE`, no new dashboard page.
- No ADR. New ADRs and amendments to accepted ones are Justin's call; this change references
  ADR-0011 and ADR-0020 rather than editing them.

## Decisions

### Decision 1 — Two checks, not one flag

Fusing them is what made the real finding invisible. Claim #8's live flag reads *expected $296.50,
Petcover paid $130.97 (fresh $150 excess this policy year)*. Every clause is wrong or irrelevant:
$296.50 came from our number not theirs, the $150 was stated on the letter rather than inferred, and
the actual anomaly — they assessed $351.50 where we submitted $446.50 — is not mentioned. One flag
cannot carry two findings, and the one it chose to carry was the false one.

Alternative considered: keep one flag and enrich its wording. Rejected — the two findings need
different *actions* (`dismiss_mismatch` vs "ask Petcover"), and the action kind is what drives the
card, the waiting party and whether dismissal should make the claim vanish.

### Decision 2 — Re-add the letter's stated line items; do not model Petcover's policy

`settlement-validation`'s 2026-07-24 revision note dropped an idea to "reverse-engineer Petcover's
own internal excess mechanics (observed varying between $0 and $105 across real letters)". That
observation was correct and misattributed. Across the five letters the **fixed excess** is what
varies ($0.00, $0.00, $0.00, $49.26, $150.00); the **age contribution** is a constant 35%, printed in
the letter's own text.

So this is not reverse-engineering. Every input is a labelled field on the letter being validated,
including the percentage. Re-adding a supplier's own stated line items and checking they sum to their
own stated total is arithmetic on given data, not a model of their policy. Concretely: we never
predict what the fixed excess *will be*, we only check that the numbers they printed add up.

Alternative considered: derive the age contribution from Aari's DOB (2013-04-02) and an age band.
Rejected — that *is* modelling their policy, one dog is not a sample, and it would fabricate a term
for the letters that state one.

### Decision 3 — The dashboard estimate stays untouched, deliberately

`dashboard-visit-ledger` says the dashboard "SHALL NOT display a fabricated or hard-coded deduction
(such as an invented age-contribution percentage) in place of the real excess/cap math". This change
does not touch it, and the distinction is worth stating because it is easy to erase by accident:

- The dashboard **projects** an expectation onto an invoice not yet claimed. It has no letter, so any
  age contribution there would be invented. The prior decision stands.
- Check A **re-adds** a settlement already received, from the letter in hand.

Applying 35% to the dashboard would overturn a recorded decision as a side effect of a bug fix. If
Justin wants the dashboard's estimate to net the age contribution too, that is a separate change and
his call — recorded in Open Questions, not resolved here. Consequence to expect meanwhile: the card's
"Expected payment" (from the ledger) will read higher than what Petcover actually pays, by roughly
35%. That is a known, named divergence, not a second bug.

This sits beside the existing recorded disagreement in the same spec (the closed-policy-year
default, undecided since 2026-07-25). Two open disagreements about the same numbers is one too many;
both belong in one conversation with Justin, not in a silent fix.

**Reversed 2026-08-04, by Justin, in that conversation.** The decision above rests on the premise that
a rate applied to the dashboard would be *invented* — derived from settlement letters and projected
onto invoices that have none. The premise is false: **65% is the policy's benefit rate**, which Justin
had already explained ("Petcover only paying 65% of a claim") independently of any letter. The
letters' constant 35% Age Contribution is the same term seen from the insurer's side, so it
corroborates the rate rather than being its source.

That distinction is the whole of Decision 3, so with the premise corrected the decision goes the other
way: the ledger estimate now nets `config.PETCOVER_BENEFIT_RATE` after the excess and before the cap,
and the note says `est. after $150 excess, 65% benefit`. Pets with no Petcover excess/cap on file
(Echo, insured with Bow Wow) are untouched and stay unavailable. See the `dashboard-visit-ledger`
delta for the requirement and why the original "no fabricated deduction" rule is not contradicted.

Consequence retired: the "Expected payment will read ~35% above what Petcover pays" divergence listed
under Risks no longer applies. The closed-policy-year disagreement is untouched and stays open.

### Decision 4 — One accessor for the claimable subtotal, with a self-checking guard test

The three sites spelling `claimable_amount` with a fallback to `amount` are the exact shape
`app/openclaw/CLAUDE.md`'s collapse table exists for: a rule enforced by convention across N callers.
The table's own verdict is that the four parts are needed or it decays — declared table, single
writer/reader, a **named** guard test, and a shadow-compare phase when the thing collapsed *writes*.

Here nothing writes; the accessor only reads `invoice_data`. So no shadow phase. The guard is
`test_no_module_substitutes_the_invoice_total_for_a_claimable_subtotal`, a regex scan over
`app/openclaw/*.py` that asserts its own matcher against a known-violating string first — the
self-check `test_no_module_outside_claim_status_writes_the_status_column` already carries, and the
reason that row of the table reads **yes** while three others read partial or none.

Alternative considered: a validation at write time that refuses to store `invoice_data` without
`claimable_amount`. Rejected — five live claims already lack it, so this would flag history rather
than prevent recurrence, and the missing key is legitimate for a claim whose invoice was extracted
before per-claim claimable subtotals existed.

### Decision 5 — Reuse the existing review queue for a dismissed-but-unexplained difference

Dismissal today nulls the flag and appends `mismatch_dismissed`. For an arithmetic difference Justin
has personally checked, that is right. For a question outstanding with Petcover it is wrong, and
event 58 is the proof: claim #2's entire finding now lives as prose in `detail.dismissed_flag` and
appears on no surface.

`claim_status.dashboard_lists()` already builds a manual-review queue. Keeping an assessment
difference in it costs a predicate, not a table. Rejected alternatives: a new `open_questions` table
(needs hand-run `ALTER TABLE` on the live DB — `db.py`'s `CREATE TABLE IF NOT EXISTS` will not touch
an existing schema), and a free-text reason on dismissal (Justin's complaint was that the card was
unclear; asking him to type an explanation of a card he did not understand solves nothing).

### Decision 6 — Waiting party is data on the action kind

`_ACTION_META` maps kind → (label, blocks-phrase). Adding a third element is the whole change; the
card reads it rather than assembling a phrase. This mirrors `status_labels.py` and ADR-0021: one map,
guarded, because three hand-synced copies is how seven claims read "Matched" for weeks. A kind with
no waiting party fails a test instead of defaulting — `owed_by` on `info_requested` already
establishes that never defaulting a party is the rule here, because naming the wrong one is how a
chase never happens.

### Decision 7 — Card figures come from the ledger row already in hand

`pending_actions()` iterates `visit_ledger()`, and those rows already carry `claimable` and
`expected` (`{available, value, note}`) with `_apply_excess_and_cap` run. So "expected payment"
needs no new calculation — a second calculation is a second answer that eventually disagrees with the
first ("derive, don't store"). The only edit inside `visit_ledger` is routing its `claimable` through
the accessor from Decision 4, which is what makes `Not recorded` reachable at the card.

## Risks / Trade-offs

- **The 35% is inferred from five letters, all for one dog.** → Every input is read from each letter
  individually, including the percentage; nothing is hard-coded. A letter stating a different
  percentage is handled by reading it. A letter stating *no* percentage skips Check A rather than
  assuming 0.35.
- **Rounding.** Petcover rounds half-up ($70.525 → $70.53) where Python's `round` gives $70.52. →
  Reuse the existing `SETTLEMENT_TOLERANCE` ($2.00) rather than introduce exact-equality arithmetic;
  the worst observed rounding gap is $0.01.
- **Displaying `Not recorded` on five live claims looks like a regression.** → It is the correct
  reading, and showing the invoice total is what produced the wrong $430.74. Called out as BREAKING
  in the proposal so it is not mistaken for a bug on first sight.
- **The card's "Expected payment" will read ~35% above what Petcover pays** while Decision 3 holds. →
  Named divergence, in Open Questions, resolved by Justin not by this change.
- **Check B will flag most live settled claims at once** if it ever runs over history. → It runs only
  on new letters; nothing re-reads or re-flags history, and `_already_recorded` already blocks a
  re-read. The historical cases go in the Petcover question list instead.
- **A second dismissal path adds a state Justin must understand.** → Only two words differ on the
  card ("for your review" vs "waiting on Petcover"), and the second keeps a row where the first
  removes it; no new UI.

## Migration Plan

None. No schema change, no data rewrite, no backfill. Deploy is the ordinary path
(`scripts/deploy.ps1` from the deploy worktree). Rollback is a revert: the change adds no column and
no event type, so an older build reads every row it wrote.

The five under-recorded `approved` events stay as they are and are documented as a historical gap.

## Open Questions

Both are Justin's to decide; neither blocks implementation.

1. **Should the dashboard's expected-reimbursement estimate net the 35% age contribution?** Today it
   does not, and `dashboard-visit-ledger` has a recorded decision against inventing one. With five
   letters showing a constant 35%, the estimate is systematically high for Aari — and Echo has no
   policy figures on file at all. Leaving it produces a card whose "Expected payment" never matches
   what arrives.
2. **The closed-policy-year disagreement, still open since 2026-07-25**, recorded in
   `settlement-validation` and `openspec/BACKLOG.md`: the dashboard drains the $150 excess for closed
   years and settlement validation does not. Untouched here, and worth settling in the same
   conversation as (1).
3. **What Petcover answers about the assessed amounts** determines whether any historical row should
   be re-routed. See `petcover-questions.md` in this change directory. No code decision until then.
