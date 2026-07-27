## 1. Reliability fixes on the path that failed (independent, ship first)

- [x] 1.1 `llm.py`: invert `_try_model` retry classification — fail fast only on a per-day cap and on a 400 that is not `tool_use_failed`; retry everything else (403 / 408 / 5xx / transport) with the existing backoff.
- [x] 1.2 Test: a 403 on attempt 1 then success on attempt 2 returns the answer; a non-`tool_use_failed` 400 raises after exactly one attempt; both assert the `llm_calls` row count.
- [x] 1.3 `telegram_bot.py`: `on_text_reply` and `_ack_user_message` read `update.effective_message` instead of `update.message`.
- [x] 1.4 `message_log.py`: `_describe` falls back to `effective_message` and marks an edit (`edit: <text>`), so an edited message is never a kind-`other` row with an empty summary.
- [x] 1.5 Test: an `edited_message` update (no `.message`) is acked, dispatched to the chat path, and logged with kind `text` and its text — the update payload from `telegram_messages` id 96 is the fixture.

## 2. Reply context and id-addressable act tools

- [x] 2.1 `telegram_bot.py`: helper resolving the claim id of a replied-to bot message — `Claim #N` from text or caption, `setpet:N:…`-style callback data as fallback; returns `None` when the message names zero or more than one claim.
- [x] 2.2 `agent.handle_message` takes an optional `claim_id` context argument and injects it as one system-side context line ("Justin is replying to the card for claim #N"), not appended to his text.
- [x] 2.3 `agent._find_claims` accepts `claim_id`; add `claim_id` to `propose_mark_sent`, `propose_set_condition`, `propose_assign_pet`, `propose_mark_resolved` implementations and schemas.
- [x] 2.4 Prompt: when a current claim is supplied, act on it by id and do not ask for a reference.
- [x] 2.5 Test: `test_tools_schema_stays_small` still passes; a turn with reply context proposes `assign_pet` for that claim with no reference supplied; an explicit id in the message beats the reply context.

## 3. The split itself

- [x] 3.1 `claim_forms.split_between_pets(claim_id, shares)` — `shares` is `[(pet_id, amount|None)]`, at most one `None`. Validates: claim exists and is matched with `invoice_data`; status not `sent` or later; pets exist; ≤1 missing amount; sum ≤ claimable subtotal + $0.01.
- [x] 3.2 Apply: original row keeps its `transaction_id`/`matched_email_id`/`invoice_number` and gets pet + its share in `invoice_data.claimable_amount`; one new `vet_claims` row per additional pet copying `transaction_id`, `matched_email_id`, `invoice_data` (own share), `invoice_file_path`, status `matched`; every row records `invoice_data.split_note` with the sibling ids and the full claimable subtotal.
- [x] 3.3 Shortfall: when the shares sum to less than the claimable subtotal, flag each row with the unapportioned remainder in the existing `unexplained $X` wording.
- [x] 3.4 `drafted` input claim: clear the pre-split draft state before re-drafting, then run `process_claim` per resulting claim so no draft can state the pre-split amount.
- [x] 3.5 Return a result dict naming every claim id, pet and share for the confirmation message.
- [x] 3.6 Tests (assert-based, `app/tests/test_core.py`): two pets one amount → shares $35 / remainder, same invoice number both rows, one transaction; over-subtotal refused with both figures; two missing amounts refused; `sent` claim refused; shortfall flags the remainder; a pet with `claim_process_defined = 0` gets a claim with no draft and appears blocked in `pending_actions()`.

## 4. Wiring it to Telegram

- [x] 4.1 `agent.propose_split_between_pets(claim_id, shares)` — validates via the same `claim_forms` guards *before* proposing, so an impossible split is refused in the reply rather than at the tap; queues one proposal.
- [x] 4.2 Tool schema + prompt line: pets and amounts come from Justin; never infer a share; never split by line item; do not turn a split request into a task.
- [x] 4.3 `telegram_bot._execute_action` handles `split` through `claim_forms.split_between_pets` and appends the per-claim result to the card.
- [x] 4.4 ASSIGN PET card text says a shared invoice can be described in a reply.
- [x] 4.5 Test: the four messages from 2026-07-27 (`telegram_messages` 94/96/97/99) as a scripted turn sequence — stubbed LLM — end in one split proposal for claim #1 with Aari $35 and no `tool_use_failed`-shaped argument fabrication.

## 5. Verify live (do not tick from unit tests alone)

- [ ] 5.1 Read-only check of claim #1 before the run: `sqlite3.connect("file:data/openclaw.db?mode=ro", uri=True)` — record `pet_id`, `status`, `invoice_data.claimable_amount` ($407.56).
- [ ] 5.2 Deploy with `./scripts/deploy.ps1` from `C:\Code\Me.OpenClaw-telegram-claimquery` and confirm `/health` reports the new `APP_VERSION`.
- [ ] 5.3 In Telegram, reply to the ASSIGN PET card for #1 with the real wording ("This is actually split between echo and Aari. Aari cost was $35 out of this") and confirm the proposal names Aari $35 / Echo $372.56.
- [ ] 5.4 Tap Confirm; verify read-only: #1 = Aari with claimable $35, sibling = Echo with claimable $372.56, both on transaction of 2026-07-06, same invoice number, split note present on both.
- [ ] 5.5 Verify Aari's claim drafts with $35 on the form and the full invoice pages attached; verify Echo's claim is blocked on Bow Wow's undefined process and shows in `/actions` as unactionable.
- [ ] 5.6 Send an edited message and a reply-to-card message; confirm both are handled and logged with real kinds (no kind-`other` empty rows).
- [ ] 5.7 Record in this file what was verified live, with the claim ids and figures actually observed.

## 6. Documentation and trail

- [x] 6.1 New ADR: one invoice, several pets — per-pet claim rows with the share in `invoice_data`, why not a shares table and not a new column; cross-reference ADR-0007 (ceiling / claimable subtotal).
- [x] 6.2 `README.md`: the shared-invoice case in the matching/claim walkthrough.
- [x] 6.3 `app/openclaw/CLAUDE.md`: `invoice_data.claimable_amount` is the per-claim share and `split_note` is its record; `split_proposals` is charge-merge, not pet-split; edited messages arrive as `edited_message`.
- [x] 6.4 `openspec/BACKLOG.md`: undo for a confirmed split; whether the ASSIGN PET card needs an explicit "Shared invoice" button; Bow Wow's claim process still undefined (6 claims + Echo's share now blocked on it).
- [ ] 6.5 Sync these deltas into `openspec/specs/` before archiving the change.

## 7. Automatic apportionment (added 2026-07-27 after the live diagnosis)

- [x] 7.1 `_paid_on_charge_date`: an invoice is date-plausible when one line of its own text carries both the charge's date (ISO + d/m/y forms) and that invoice's amount; wired into `_pick_invoice` and the complement search without widening `INVOICE_MATCH_WINDOW_DAYS`.
- [x] 7.2 `_complement_for`: the one other pooled invoice that closes the charge within the surcharge margin — ceiling, date, already-claimed and distinct-identity gates each applied, ambiguity (2+ candidates) refused.
- [x] 7.3 `match_claim` pools every extracted invoice and keeps scanning for a complement instead of returning on the first acceptable one; `_apply_match` writes either the single match (unchanged flag behaviour) or the apportioned pair.
- [x] 7.4 Sibling claim carries its own `matched_email_id`, invoice, claimable subtotal and pet (printed patient field / single pet in text); both rows record `invoice_data.charge_note`.
- [x] 7.5 Tests: the real Shire numbers apportion into two claims with the right pets and invoice numbers; every refusal case (gap unclosed, pair over the charge, wrong date window, two candidates, same invoice twice, nothing to explain); the receipt-payment-line gate including the different-lines rejection.
- [ ] 7.6 Live: clear claim #1's wrong match (it points at a payment-list email), re-match, and confirm the two receipts land as two claims — Aari $35.00 and Echo $369.33 on the one 2026-07-06 charge.
- [ ] 7.7 Live follow-up: the receipts are inline email text with no PDF attachment, so `invoice_file_path` stays NULL and neither claim can draft (Petcover requires the invoice attached). Decide whether to ask the vet for PDFs or generate one from the receipt text.
