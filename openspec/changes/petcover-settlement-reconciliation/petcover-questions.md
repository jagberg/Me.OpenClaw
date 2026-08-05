# Questions for Petcover

Policy `GABR-0306-DC1-00000001R`, pet **Aari** (their letters say "Ari").

Justin sends these himself. The app never sends mail (hard rule) — Gmail drafts only, and nothing
here is drafted automatically.

**The one thing to ask, if you ask only one thing:**

> For each Treatment number below, which invoice did you assess — invoice number, treatment date and
> clinic? Your letters state an amount but no invoice reference, and in every case the amount you
> assessed is not the amount we submitted under that Treatment number.

Everything below is the per-letter detail for that one question. All five letters state the
arithmetic correctly (claimed − fixed excess, less 35% Age Contribution, equals paid), so **do not
dispute the sums** — the question is only ever *which invoice*.

---

## 1. `DC1-26-5992` Treatment 2 — the letter Justin was asked to review

- **Letter:** 30 Jul 2026 (`PetCover Letter - Claim Approval`)
- **States:** claimed $35.00 · fixed excess $0.00 · Age Contribution $12.25 [35%] · paid $22.75
- **Recorded against:** our claim #2 — charge $585.39 on 19 Jun 2026, The Shire Veterinary Caringbah,
  invoice total $580.74, conditions Arthritis and Raised ALT
- **The gap:** $35.00 assessed against a $580.74 invoice. The two are not the same document.
- **Ask:** which invoice was assessed under `DC1-26-5992` Treatment 2, and has the 19 Jun 2026 Shire
  Veterinary invoice of $580.74 been assessed at all? If not, what does it need?

## 2. `DC1-26-5992` Treatment 1

- **Letter:** 30 Jul 2026, one second before the above
- **States:** claimed $351.50 · fixed excess $150.00 · Age Contribution $70.53 [35%] · paid $130.97
- **Recorded against:** our claim #8 — charge $446.50 on 2 Apr 2026, Kings Vet Kingsgrove, **invoice
  199464**, $446.50, condition Raised ALT
- **The gap:** $351.50 is exactly the amount of a *different* invoice of ours — **invoice 1000229**,
  Kings Vet, our claim #6, charged 18 May 2026 — which we submitted under `DC1-27-5628` Treatment 6, a
  different claim reference.
- **Ask:** was invoice 199464 ($446.50) assessed, or was invoice 1000229 ($351.50) assessed under this
  reference? If the latter, is invoice 199464 still outstanding?
- This claim still carries a live flag in our system.

## 3. `DC1-27-5628` Treatment 5

- **Letter:** 27 Jul 2026
- **States:** claimed $446.50 · fixed excess $49.26 · Age Contribution $139.03 [35%] · paid $258.21
- **Recorded against:** our claim #7 — charge $132.50 on 17 Apr 2026, Kings Vet, **invoice 200017**,
  $132.50, condition Arthritis
- **The gap:** $446.50 is exactly invoice **199464** (our claim #8), which we submitted under
  `DC1-26-5992` Treatment 1 — see question 2. So $446.50 appears to have been assessed under this
  reference while a *different* amount was assessed under the reference we submitted it to.
- **Ask:** which invoice is `DC1-27-5628` Treatment 5? And separately — the $49.26 fixed excess is
  unlike the $0.00 and $150.00 on the other letters; what determined it?

## 4. `DC1-27-5628` Treatment 6

- **Letter:** 27 Jul 2026, three seconds after the above
- **States:** claimed $45.00 · fixed excess $0.00 · Age Contribution $15.75 [35%] · paid $29.25
- **Recorded against:** our claim #6 — charge $351.50 on 18 May 2026, Kings Vet, **invoice 1000229**,
  $351.50, condition Raised ALT
- **The gap:** $45.00 matches two other invoices of ours — **invoice 184556** (claim #22, charged
  28 Jul 2025, submitted under `DC1-26-5978` Treatment 1) and claim #18's $45.00 (charged 26 Sep
  2025, submitted under `DC1-27-5628` Treatment 4). We cannot tell which from the letter.
- **Ask:** which invoice is `DC1-27-5628` Treatment 6, and was invoice 1000229 ($351.50) assessed
  under this reference or under `DC1-26-5992` Treatment 1 (question 2)?

## 5. `DC1-27-5628` Treatment 2

- **Letter:** 24 Jul 2026
- **States:** claimed $35.00 · fixed excess $0.00 · Age Contribution $12.25 [35%] · paid $22.75
- **Recorded against:** our claim #21 — charge $44.75 on 8 Aug 2025, Kings Vet, invoice $44.75,
  condition Arthritis
- **The gap:** $35.00 versus $44.75. $35.00 matches our claim #19 (invoice $35.00, charged 11 Sep
  2025, submitted under `DC1-27-5628` Treatment 3) — one serial along.
- **Ask:** which invoice is `DC1-27-5628` Treatment 2? Our records have Treatment 2 as the 8 Aug 2025
  $44.75 invoice and Treatment 3 as the 11 Sep 2025 $35.00 invoice; if those are the other way round
  we should correct our side.
- This claim still carries a live flag in our system.

---

## Two questions that are not about a specific letter

6. **Can you list every Treatment number on `DC1-26-5992` and `DC1-27-5628` with the invoice number
   and treatment date each one covers?** Our serial-to-invoice map is inferred from the order
   acknowledgements arrive, not from anything you have confirmed, and questions 1–5 suggest it is
   wrong. One list settles all five at once.

7. **Is the 35% Age Contribution fixed for Aari's age band, and does it change at a policy
   anniversary?** It is 35% on all five letters. Confirming it lets us show a correct expected payment
   before a settlement arrives instead of a figure that is always about a third too high.

## STOP — most of this list is already answered (2026-08-04)

**Petcover answered the mapping question on 2026-07-29 at 10:56, before this list was written.**
Justin asked on 25/07 for "a detailed table from 23 SEP 2024 until today"; the reply
(`19fab5f3b534416c`) is a table of every claim lodged since 2023 with claim reference, **Sr number**,
**treatment date**, date advised, amount payable, loss cause and status. The app never surfaced it
because the mail is HTML-only and the body extractor fell through to a 198-character snippet — now
fixed.

That table resolves questions 1–6 and 8–11 without asking anything. It shows our serial→claim map is
wrong on every serial, that each letter's stated amount matches its **true** claim's invoice exactly,
and it supplies the treatment date the approval letters omit. Before sending anything, re-read it.

What is left to ask is much smaller, and different in kind:

- **A confirmation, not a question.** Send our corrected mapping (claim id → reference + Sr, with
  invoice number and treatment date) and ask them to confirm it, rather than asking them to derive it
  again. They have already done the work once.
- **`DC1-27-5628` Sr 8 was "Further Information Required" with $377.48 payable on 29/07**, and the
  03/08 approval letter settles it at $289.73 with $135.00 non-claimable. Worth asking what the
  further information was and whether it is now closed — $377.48 is exactly $580.74 × 0.65, i.e. their
  figure *before* the $135.00 was excluded.
- **Question 7 stands** (is the 35% fixed for Ari's age band, does it change at anniversary), except
  the framing changes: Justin has confirmed 65% as the policy's benefit rate, so this is asking
  whether the rate is stable, not what it is.

The list below was written on the assumption that the mapping was undeterminable. It is kept for the
record and because the specific amounts are still worth naming in any conversation — but do not send
it as-is.

## Added 2026-08-04, after sweeping the whole mailbox

The five letters this list was written from were not all of them — there are ten. The new ones add
four assessed amounts to ask about and one question of a different kind.

8. **Treatment 7 on `DC1-27-5628` was assessed at $132.50.** Our claim carrying that serial (#13) was
   submitted at $944.50, and $132.50 is claim #7's invoice — already settled under Treatment 5 of the
   same reference. Which invoice does Treatment 7 cover?

9. **Treatment 8 on `DC1-27-5628` was assessed at $580.74 with $135.00 deemed non-claimable.** That
   $580.74 is the invoice on claim #2, which you assessed at $35.00 under `DC1-26-5992` Treatment 2 on
   30/07. Is Treatment 8 the assessment of that invoice, and if so what does Treatment 2 cover? The
   $135.00 appears to be the "Blood Profile (Chem 10 only)" line — confirming that would tell us which
   items you treat as non-claimable, which we currently guess with a keyword list.

10. **`DC1-26-5993` is a reference we have never seen acknowledged.** Its Treatment 1 was assessed at
    $944.50 and paid $516.42 on 31/07. Which pet, condition and invoice does that thread cover?

11. **Treatments 3 and 4 on `DC1-26-5992` were assessed at $2,521.46 and $135.00.** We hold no claim
    against either serial. The $2,521.46 is our largest invoice (SAH Inner West, ultrasound and
    bloods); the $135.00 is a line item on a different invoice again.

**Four serials we hold no claim for at all** — `DC1-26-5992` Tr 3 and Tr 4, `DC1-27-5628` Tr 8,
`DC1-26-5993` Tr 1 — which is the strongest version of question 6: the serial-to-invoice list would
resolve every one of these at once.

## What is deliberately not asked

- Nothing disputes an amount paid. All **ten** letters' arithmetic checks out to the cent.
- Nothing asks about claim #2's $580.74 invoice being *underpaid* — until we know which invoice was
  assessed under Treatment 2, we do not know it was assessed at all.
- No claim for Echo. Echo is insured with Bow Wow, not Petcover, and has no claim process defined.
