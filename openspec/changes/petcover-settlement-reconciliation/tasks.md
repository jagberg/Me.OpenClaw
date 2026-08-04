## 0. The letters were never reaching the claims service (found while confirming the figures)

- [x] 0.1 `gmail_ingest.poll_once` skips `config.PETCOVER_STATUS_SENDERS` and leaves those messages **unmarked** in `processed_emails` — the mark is the lockout, so skipping without marking is the fix. `PETCOVER_STATUS_SENDERS` moved to `config` (importing `pipeline` from `gmail_ingest` would be circular); `pipeline.PETCOVER_STATUS_SENDERS` kept as an alias.
- [x] 0.2 `test_petcover_letters_are_never_taken_by_the_task_ingest` — asserts every configured sender is skipped, and asserts that `claims.au@` matches *neither* `_is_noise` branch, which is why nothing else was going to stop it.
- [x] 0.3 Live evidence, read-only: five approval letters (28/07–03/08) carry `processed_emails.task_id` (139, 152, 161, 162, 163) and **no claim status event at all**; the five that worked carry `task_id: NULL`. Latest `approved` event is #55 (2026-07-30) against a mailbox holding ten approval letters. Claim #13 carries `DC1-27-5628 sr 7` — the exact serial the 28/07 letter cites, so this was never a routing or matching failure.
- [ ] 0.4 Run the recovery re-read against the live DB: `poll_petcover_status(reread=True, since=…)`. **Justin's call, deliberately not done here** — it is a live write, and it must run after this change is deployed or the five recovered letters get flagged by the old `claimable − 150` formula. `process_reply` skips already-logged (email, claim, event) triples, so it records only what is new and cannot resurrect claim #2's dismissal.

## 1. The claimable-subtotal accessor and its guard

- [x] 1.1 `claim_status.claimable_subtotal(invoice_data) -> (value, recorded)`. Takes the raw column or a parsed dict; no fallback to `amount`; a stored `0.0` returns `(0.0, True)`.
- [x] 1.2 `_validate_settlement` routes through it; the `amount` fallback is gone.
- [x] 1.3 `visit_ledger` routes through it and carries `claimable_recorded` plus `invoice_present`. The note **was** wrong: `_apply_excess_and_cap` said `no invoice yet` for a claim that has an invoice and no recorded subtotal, sending Justin after a document he already holds. Now `invoice on file, but no claimable subtotal recorded`.
- [x] 1.4 `claim_detail` routes through it and adds `claimable_amount_recorded` so the agent answers "not recorded" rather than reading a null as zero.
- [x] 1.5 `test_no_module_substitutes_the_invoice_total_for_a_claimable_subtotal` — three matchers (inline default, two-statement, `or`-chain), each self-checked against a known-violating string and against the accessor call, then a scan of `app/openclaw/*.py`. **It immediately found two sites this plan did not list** (see 1.7).
- [x] 1.6 Live read-only: accessor reports not-recorded for #2, #16, #18, #19, #21 and `(0.0, True)` for #20. Confirmed against a `src.backup()` copy — `#2 invoice.amount=580.74 accessor=(None, False)`, `#8 accessor=(446.5, True)`, `#20 accessor=(0.0, True)`.
- [x] 1.7 Two further sites, found by the guard, not by reading: `dashboard_lists`'s settled-reconciliation row fell through to `detail.get("claimed_amount")` — Petcover's own figure, in a row that exists to compare against it — and `claim_forms.apportion_between_pets` split the invoice total. The split now **refuses** when the subtotal is unrecorded rather than apportioning the wrong base. Behaviour change, recorded in the proposal.

## 2. Capture the letter's full breakdown

- [x] 2.1 `non_claimable_stated` added (`Non‐claimable amount $0.00`, U+2010, `_normalize` maps it).
- [x] 2.2 `age_contribution_percent` added from the bracketed rate; absent bracket means absent key.
- [x] 2.3 Ran `extract_approval_amounts` over the live letters read-only through the container. **The five ids in this plan were not all of them — the mailbox holds ten approval letters.** All ten give up seven keys. Recorded:

| letter | reference | claimed | fixed excess | non-claimable | age contrib | % | paid |
|---|---|---|---|---|---|---|---|
| `19f92024cabed6ac` | `DC1-27-5628` Tr 2 | 35.00 | 0.00 | 0.00 | 12.25 | 0.35 | 22.75 |
| `19fa131a64fa488b` | `DC1-27-5628` Tr 5 | 446.50 | 49.26 | 0.00 | 139.03 | 0.35 | 258.21 |
| `19fa131b1857eb00` | `DC1-27-5628` Tr 6 | 45.00 | 0.00 | 0.00 | 15.75 | 0.35 | 29.25 |
| `19fa67bc840eaf82` | `DC1-27-5628` Tr 7 | 132.50 | 0.00 | 0.00 | 46.38 | 0.35 | 86.13 |
| `19fc4f93cd570a7b` | `DC1-27-5628` Tr 8 | 580.74 | 0.00 | **135.00** | 156.01 | 0.35 | 289.73 |
| `19fb1035e89a8360` | `DC1-26-5992` Tr 1 | 351.50 | 150.00 | 0.00 | 70.53 | 0.35 | 130.97 |
| `19fb10367f2443da` | `DC1-26-5992` Tr 2 | 35.00 | 0.00 | 0.00 | 12.25 | 0.35 | 22.75 |
| `19fc4ff8acc16ada` | `DC1-26-5992` Tr 3 | 2521.46 | 0.00 | 0.00 | 882.51 | 0.35 | 1638.95 |
| `19fc4ff987644163` | `DC1-26-5992` Tr 4 | 135.00 | 0.00 | 0.00 | 47.25 | 0.35 | 87.75 |
| `19fb4d361c76f24d` | `DC1-26-5993` Tr 1 | 944.50 | 150.00 | 0.00 | 278.08 | 0.35 | 516.42 |

  Every letter also states `Percentage Excess: $0.00 [0%]` — a fifth deduction this plan did not know about. Captured as `percentage_excess_stated` (2.5).
- [x] 2.4 No backfill of the five stored `approved` events. Backlog note added.
- [x] 2.5 `percentage_excess_stated` added. $0.00 on all ten, so its position in the order of operations is unverified — the code comment names that as the first thing to re-read if Check A ever fires on a sound letter.

## 3. Split the settlement check in two

- [x] 3.1 Reference and Sr are learned **before** `_validate_settlement`, and the claim row is re-read so the sibling query sees them. Nothing else in `process_reply` changed order.
- [x] 3.2 Check A implemented, with `percentage_excess_stated` as a fourth deduction. Skipped when the letter states no percentage. Flag names claimed, excess, non-claimable, the rate, the computed expectation and the paid amount.
- [x] 3.3 Check B implemented: stated `claimed_amount` vs the recorded claimable subtotal, skipped when unrecorded. Flag names claim id, reference, Sr, submitted, assessed, and asks which invoice Petcover assessed.
- [x] 3.4 A stated `fixed_excess_stated` is used and suppresses both excess phrases entirely, in the letter path and in the older fallback.
- [x] 3.5 The transaction-date-bucketed excess/cap fallback is intact for the older style; its existing tests still pass (signature changed from `paid_amount` to `detail`).
- [x] 3.6 An unrecorded subtotal flags that fact, naming the letter's figures. No expectation computed.
- [x] 3.7 `test_settlement_arithmetic_matches_petcovers_own_figures` — all ten letters as fixtures, plus the reversion guard: on Tr 8 the post-mortem's `(claimed − excess) × 0.65` gives $377.48 against their $289.73.
- [x] 3.8 `test_assessment_difference_is_a_separate_flag_from_arithmetic` — claim #8's figures: exactly one flag, of kind `assessment`, `fresh $150 excess` absent.

## 4. Dismissal keeps an open question visible

- [x] 4.1 `dismiss_mismatch` records claimed, paid, stated excess, stated non-claimable, the recorded claimable subtotal (with its recorded flag) and `check` — read from the event that actually carried the figures, not from the flag's prose.
- [x] 4.2 An assessment dismissal keeps the claim in `dashboard_lists()`'s review queue; an arithmetic one leaves. No new table. The queue's template previously offered "Link to claim" for every row, which is a no-op for an already-linked event, so a linked row now renders the figures and who is waiting instead.
- [x] 4.3 `test_dismissing_an_assessment_difference_keeps_it_reviewable` — asserts the figures on the event, that the assessment claim stays queued and the arithmetic one does not.
- [x] 4.4 Asserted, not assumed: a second `dismiss_mismatch` returns `ok: False`, `_already_recorded` still blocks the re-read, and the flag stays NULL.

## 5. Card figures and waiting party

- [x] 5.1 Third element on every `_ACTION_META` entry. `confirm_resolved` corrected — it now reads `owed_by` (`Petcover is waiting on you` vs `you're waiting on the vet`), which is what Justin's complaint was about; `dismiss_mismatch` splits arithmetic/assessment.
- [x] 5.2 `test_every_action_kind_declares_a_waiting_party` — every kind in `ACTION_PRIORITY` declares one of the four values, and `waiting_party` raises for an undeclared situation instead of defaulting.
- [x] 5.3 `pending_actions()` carries `claimable`, `claimable_recorded` and `expected` from the ledger row already in hand.
- [x] 5.4 `commands._action_card_text` prints `Claim amount:` and `Expected payment:`, with `Not recorded` plus `expected.note` where unavailable, and `$0.00` only for a recorded zero.
- [x] 5.5 The `Blocks:` line is now `<waiting party> · <what stalls>`. Claim `#id` still on every card.
- [x] 5.6 `test_action_card_never_shows_the_invoice_total_as_the_claim_amount` — claim #2's shape: `Not recorded`, and `580.74` absent from the card.
- [x] 5.7 `claim_card.render_actions_summary` needs no change — per-kind aggregate, no per-claim figures. Unedited.

## 6. Verify against real data

- [x] 6.1 Cards rendered from a `src.backup()` copy of the live DB. Acceptance evidence:

```
🔍 REVIEW SETTLEMENT
Claim #2 · The Shire Veterinary Ca… · $585.39
Aari · Arthritis; Raised ALT · 2026-06-19 (46d ago)
Claim amount: Not recorded
Expected payment: Not recorded (invoice on file, but no claimable subtotal recorded)
nobody is waiting — this is for your review · a paid-vs-expected difference is unreviewed

✅ CONFIRM RESOLVED
Claim #8 · Kings Vet KINGSGROVE NSW · $446.50
Aari · Raised ALT · 2026-04-02 (124d ago)
Claim amount: $446.50
Expected payment: $296.50
you're waiting on the vet · a Petcover request is still open

• DEFINE CLAIM PROCESS
Claim #20 · Kings Vet KINGSGROVE NSW · $152.50
Echo · 2025-08-11 (358d ago)
Claim amount: $0.00
Expected payment: Not recorded (no policy excess/cap on file)
nobody is waiting — this is for your review · every claim for this pet is stuck

• DEFINE CLAIM PROCESS
Claim #3 · The Shire Veterinary Ca… · $141.87
Echo · 2026-06-17 (48d ago)
Claim amount: $141.87
Expected payment: Not recorded (no policy excess/cap on file)
nobody is waiting — this is for your review · every claim for this pet is stuck
```

  Claim #2 reads `Not recorded` where it used to imply $430.74. Claim #8's card is `confirm_resolved` (it outranks `dismiss_mismatch`) and now names the vet, not Petcover. Claim #20 shows a recorded `$0.00`, distinct from absent. Echo cards carry the `no policy excess/cap on file` note.

- [x] 6.2 Suite green: `./.venv/Scripts/python.exe tests/test_core.py`, ALL TESTS PASSED. This worktree has no `.venv`, so it was run with the **main checkout's** interpreter (`C:\Code\Me.OpenClaw\app\.venv\Scripts\python.exe`) against this worktree's `tests/` and `openclaw/`. Ten tests added; three pre-existing call sites updated for the `detail` signature and two fixtures for the new card keys.
- [x] 6.3 `_validate_settlement` re-run over every live letter's figures, read-only. **The plan's expectations were wrong in three of six cases**, because they were derived before the mailbox sweep:

| letter | claim | expected in plan | actual |
|---|---|---|---|
| `5628` Tr 2 | #21 | assessment only | **subtotal-not-recorded** (#21 has no `claimable_amount`) |
| `5628` Tr 5 | #7 | assessment only | assessment (submitted $132.50 vs assessed $446.50) |
| `5628` Tr 6 | #6 | assessment only | assessment (submitted $351.50 vs assessed $45.00) |
| `5628` Tr 7 | #13 | not in plan | assessment (submitted $944.50 vs assessed $132.50) |
| `5992` Tr 1 | #8 | assessment only | assessment (submitted $446.50 vs assessed $351.50) |
| `5992` Tr 2 | #2 | subtotal-not-recorded | subtotal-not-recorded |
| `5992` Tr 3, Tr 4, `5628` Tr 8, `5993` Tr 1 | — | not in plan | **no claim carries these serials** |

  **No arithmetic flag fired on any of the ten.** Petcover's own figures add up every time; what does not reconcile is which invoice sits under which serial, which is the question for them.
- [x] 6.4 Nothing in this work wrote to the live DB. Every host read used `file:…?mode=ro`; the only host-side writes went to a `src.backup()` copy in the scratchpad with `DATABASE_PATH` pointed at it. `openclaw.db-wal` and `openclaw.db-shm` both still present. `C:\data\openclaw.db` last modified 30/07 — untouched. (The app container writes to the live DB continuously on its own; the claim here is only about this session.)

## 8. The 65% benefit rate on the dashboard estimate (Justin, 2026-08-04)

- [x] 8.1 `config.PETCOVER_BENEFIT_RATE = 0.65`, env-overridable. One rate for the one Petcover-insured pet; a per-pet rate needs a column and so a hand-run `ALTER TABLE`, which waits for a second insured pet.
- [x] 8.2 `_apply_excess_and_cap` applies it after the excess and before the cap — the order the letters use ($(580.74 − 135.00) × 0.65 = $289.73 on Tr 8). Note reads `est. after $150 excess, 65% benefit`.
- [x] 8.3 Reversal of design.md Decision 3 recorded there and in a new `dashboard-visit-ledger` delta, with the corrected premise: 65% is the policy's benefit rate, known independently of the letters, so the spec's "no fabricated deduction" rule never covered it. Closed-policy-year disagreement untouched.
- [x] 8.4 Two existing ledger assertions updated ($50.00 → $32.50; $100.00 → $65.00) with the reason in a comment. A settled claim's actual paid amount still overrides the estimate untouched, and a claim wholly inside the excess is still $0.00.
- [ ] 8.5 Re-render the live cards after deploy to confirm the estimate matches what Petcover pays — claim #8's card read `Expected payment: $296.50` in 6.1 above and should now read `$192.72` (`round()` on $192.725 lands down — the estimate is a rounded projection, not money, so a cent either way is not worth exact-decimal arithmetic).

## 7. Documentation

- [x] 7.1 `post-mortem.md` re-read and corrected where the code and the mailbox contradicted it — four more letters, the fifth deduction line, five substitution sites not three, the `Blocks:`-line attribution, and the delivery failure that explains the silence since 30/07.
- [x] 7.2 `petcover-questions.md` updated: four more assessed amounts to ask about, and four serials we hold no claim for.
- [x] 7.3 `README.md`: the pipeline diagram names the two checks, and a new paragraph explains what each asks, that the claim amount is always the recorded claimable subtotal, and why Petcover's mail is excluded from task capture.
- [x] 7.4 `openspec/BACKLOG.md`: both Open Questions, the five under-recorded `approved` events, and the four unclaimed serials ($4,181.70 claimed / $2,532.85 paid).
- [x] 7.5 Delta specs updated with what was actually verified (`settlement-validation`, `claimable-subtotal-provenance`) and one added (`email-ingestion`). Sync into `openspec/specs/` before archiving.
- [x] 7.6 No ADR created or amended.
