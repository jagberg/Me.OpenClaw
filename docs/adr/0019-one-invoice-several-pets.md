# ADR-0019: One charge covering several pets — a claim per pet

**Date**: 2026-07-27
**Status**: accepted (amended same day — see "Amendment: the live case was two invoices, not one")
**Deciders**: Justin

## Context

The Shire Veterinary Caringbah charged **$407.56** on 2026-07-06 (claim #1). The invoice, matched on 2026-07-27, covers **two pets**: Aari's share is $35, Echo's the rest. A `vet_claims` row carries exactly one `pet_id`, so the ASSIGN PET card offered Aari *or* Echo and nothing else. Either tap claims the full $407.56 against one pet — over-claiming for Aari, and losing Echo's share with no record that it existed.

ADR-0007 already recorded the neighbouring case: one charge covering **several invoices** for different pets ($177.50 = a $35 and a $142.50 invoice). That splits cleanly, because each pet gets its own invoice. This one does not: one invoice document, two patients, one bank charge.

Justin's attempt to say so in chat (four messages, 2026-07-27, `telegram_messages` 94–99) failed for four separate reasons — no tool for the operation, no way to name the claim he was replying to, an `edited_message` crash, and a single un-retried `403`. Those are fixed alongside this decision but are not this decision; only the data model is recorded here.

Constraints that shaped it:

- Live schema changes to an existing table need a **manual `ALTER TABLE`** against `app/data/openclaw.db` — `CREATE TABLE IF NOT EXISTS` in `db.py` won't touch it. A design needing a new column costs a hand-run migration on the live DB.
- Everything downstream of a claim is already per-pet: form filling, batching (≤4 invoices, same pet), excess/cap math (ADR-0011), correlation, status events.
- `claim_forms._charge` already puts `invoice_data.claimable_amount` on the form, per ADR-0007 (claimable subtotal, never the bank amount).
- `split_proposals` is a **different** concept with a colliding name: one invoice paid over several bank charges, merged on confirmation.

## Decision

**One claim row per pet, and the per-pet share lives in each row's own `invoice_data.claimable_amount`.**

- The original claim keeps the first share; one new `vet_claims` row is inserted per additional pet, copying `transaction_id`, `matched_email_id`, `invoice_data` (with its own `claimable_amount`) and `invoice_file_path`, at status `matched`.
- `condition_text` is **not** copied. The other pet's condition is not knowable from this one, and inventing a required field is forbidden.
- Every row records the split in `invoice_data.split_note`: the full claimable subtotal and each sibling's id, pet and share.
- The **transaction is not split**: siblings share one `transaction_id`, and the bank charge remains the ceiling on the **sum** of the shares (ADR-0007).
- Shares come from Justin. Exactly one may be left unstated and derived as the remainder; two unknowns are refused. Shares totalling more than the claimable subtotal are refused, naming both figures. A shortfall splits but flags the unapportioned remainder.
- Splitting is **pre-submission only** (`matched`, `drafted`). Anything `sent` or later is refused — that correction has to go to the insurer.
- A `drafted` claim's existing Gmail draft states the pre-split amount, so it is **deleted** (`claim_forms.discard_claim_draft`, mapping our stored message id through `drafts().list`) and re-drafted per share. Deleting our own unsent draft is not the forbidden operation; `send()` is. A deletion failure flags the claim rather than leaving a wrong draft silently sendable.
- The write is gated by a Confirm tap, via the existing chat-proposal harness (ADR-0016). `claim_forms.check_split` runs every guard with no writes so the agent refuses an impossible split in its reply instead of after the tap.

## Consequences

- No schema change and no manual live DDL. A claim with no `split_note` behaves exactly as before.
- A pet whose insurer has no defined process still gets its share recorded: Echo (Bow Wow Insurance, `claim_process_defined = 0`) lands in the existing blocked pool with the existing flag. Visible and unclaimable beats invisible.
- Petcover receives a $35 claim against a $407.56 invoice, with the full invoice attached. That is the ADR-0007 model already in use, but the first such settlement is worth watching for an info-request about the difference.
- `invoice_data.split_note` is an untyped JSON convention, asserted in the smoke suite. The day it needs querying is the day it earns a column.
- The dashboard now shows two claims under one bank charge. That is what happened in reality.
- **No undo.** Guarded by the pre-`sent` restriction and by naming every resulting claim, pet and share in the confirmation; a wrong split leaves a visible stray claim. Tracked in `openspec/BACKLOG.md`.

## Amendment: the live case was two invoices, not one (2026-07-27)

The decision above stands, but the case that prompted it was diagnosed wrong. Reading the actual documents that afternoon — two receipts Justin's wife forwarded — showed the $407.56 charge paid **two separate invoices**, one per pet, each its own document:

| Invoice | Patient | Service date | Total |
|---|---|---|---|
| SHV49c1622284e5 | Aari | 19 Jun 2026 | $35.00 |
| SHVd5b232905fdb | Echo | 30 Jun 2026 | $369.33 |

$35.00 + $369.33 = $404.33; the remaining **$3.23 is card surcharge** (0.79%, inside `SURCHARGE_MARGIN_PCT`). ADR-0007's ceiling rule already covers this shape ("one $177.50 charge = a $35 + a $142.50 invoice, different pets") — what was missing was any code that *acted* on it.

Two further decisions follow, both automatic (no confirmation tap), because unlike the merge case nothing is closed or overwritten — a second claim is added, and a wrong one is reversible with the existing ❌ Wrong invoice button:

1. **The matcher apportions a charge across two invoices itself.** When the matched invoice leaves an unexplained remainder and exactly ONE other candidate closes the charge within the surcharge margin, the claim takes one invoice and a sibling claim on the same transaction takes the other, each with its own `matched_email_id`, invoice, claimable subtotal and pet (read off the invoice's printed patient field). Both must independently clear the ceiling, date and already-claimed gates, and **ambiguity is refused** — if two candidates would each complete the sum, we cannot know which invoice the charge paid, so nothing is apportioned. Previously the first invoice matched and the rest of the charge became the flag `possible additional invoice — unexplained $372.56`, leaving the second pet's invoice unclaimed indefinitely.

2. **A receipt's payment line stands in for the service-date window.** `INVOICE_MATCH_WINDOW_DAYS` is 3, measured on the invoice's service date, which rejected *both* real invoices: the visits were 19 Jun and 30 Jun, the card was charged 6 Jul. Each receipt prints `06/07/2026 Credit Card $35.00`. So an invoice is also date-plausible when a single line of its own document carries **both** the charge's date and that invoice's amount. The window is not widened — it exists because an open-ended one let one Shire Vet claim take another Shire Vet visit's invoice — and requiring both facts on one line stops a bulk email lending its payment dates to the wrong invoice.

`split_between_pets` (shares of ONE invoice) is retained: a single invoice listing both patients is real — the same vet's bulk history email bills Aari and Echo on one document — and only Justin can say how such a bill divides. Automatic apportionment handles the two-document case; the manual split handles the one-document case.

## Alternatives considered

- **A `claim_pet_shares` table.** Every per-pet consumer downstream (forms, batching, excess/cap, correlation) would have to learn that a claim can have two pets. One row per pet costs one insert and teaches nothing new.
- **A new `claim_share_amount` column.** Cleaner to read than JSON, but needs a hand-run `ALTER TABLE` on the live DB to hold a number the existing `invoice_data` already carries in the exact place the form reads.
- **Reusing `split_proposals`.** Same word, opposite direction (several charges → one invoice). Conflating them would make both harder to reason about.
- **Splitting by line item** ("the ultrasound was Echo's"). Needs per-item pet attribution the invoice does not reliably state; Justin can always give amounts.
- **Editing the existing Gmail draft in place** instead of delete-and-redraft. `create_claim_draft` only creates; making it update would change the single path every claim uses to fix one case.
