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

- [x] 5.1 Read-only check of claim #1 before the run: `sqlite3.connect("file:data/openclaw.db?mode=ro", uri=True)` — record `pet_id`, `status`, `invoice_data.claimable_amount` ($407.56).
- [x] 5.2 Deploy with `./scripts/deploy.ps1` from `C:\Code\Me.OpenClaw-telegram-claimquery` and confirm `/health` reports the new `APP_VERSION`.
- [x] 5.3–5.5 **Superseded for this charge** (see section 7 and the live record below): the $407.56 charge turned out to be two invoices, so it was handled by automatic apportionment, not by a share-based split. The figures these tasks named ($372.56 for Echo) came from the wrong premise; the real split is $35.00 / $369.33 + $3.23 surcharge. Echo's claim being blocked on Bow Wow was verified.
- [x] 5.6 Partially verified live: the crashed `edited_message` (update 176928775) replayed through the new code on both deploys — handled, no exception, reached the chat agent, and the reply-to-card context resolved claim #1 by itself. NOT yet observed: a *fresh* edit's log row reading `edit: …` (row 96 keeps its original kind/error by design, since `mark_processed` refuses errored rows).
- [x] 5.7 Recorded below.

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
- [x] 7.6 Live: clear claim #1's wrong match (it points at a payment-list email), re-match, and confirm the two receipts land as two claims — Aari $35.00 and Echo $369.33 on the one 2026-07-06 charge.
- [x] 7.7 ~~Live follow-up: the receipts are inline email text with no PDF attachment~~ — **wrong, corrected 2026-07-27.** Each email carries its own invoice PDF (`SHV49c1622284e5.pdf`, `SHVd5b232905fdb.pdf`, ~115 kB each). The earlier reading came from listing only top-level MIME parts, which misses nested multipart attachments — use `gmail_client._iter_attachment_parts`. `invoice_file_path` was NULL only because `_draft_matched_claims` (which calls `ensure_invoice_file`) had not run since the manual match; running it produced `/data/invoices/claim-1-2026-06-19.pdf` and `/data/invoices/claim-25-2026-06-30.pdf`. Nothing to ask the vet for.

## Verified live 2026-07-27 (task 5.7)

Deployed `df05e0b+feat/telegram-agent-reach`; `/health` returned 200 in ~13s (before the fix, the awaited startup replay held it ~30s and `/health` refused the connection).

Claim #1 before: `pet_id NULL`, status `matched`, matched to email `19f7c8844bdac573` — a payment-list email extracted as three bare amounts (141.87 / 585.39 / 407.56), no items, no services, `invoice_file_path NULL`. That match was wrong, which is why the claim could never draft.

After `unmatch(1)` + `match_claim`, on the one 2026-07-06 charge of $407.56:

| Claim | Pet | Email | Invoice date | Amount | Claimable | Flag |
|---|---|---|---|---|---|---|
| #1 | Aari | `19fa2a26f2b06eb5` | 2026-06-19 | $35.00 | $35.00 | none |
| #25 (new) | Echo | `19fa2a2422c5727e` | 2026-06-30 | $369.33 | $369.33 | none |

Both carry `charge_note: one $407.56 charge paid two invoices: $35.00 + $369.33`. The $3.23 balance is card surcharge and is not claimed. No `unexplained $372.56` flag anywhere — previously that flag was the end of the story and Echo's invoice was never claimed.

Pets were assigned automatically from each receipt naming exactly one dog (`_single_pet_in_text`); the text-extraction prompt returns no `patient`/`invoice_number` field, so identity fell back to amount+date, which is unambiguous here.

Both claims then got their invoice PDFs from the emails' own attachments via `ensure_invoice_file`: `/data/invoices/claim-1-2026-06-19.pdf` and `/data/invoices/claim-25-2026-06-30.pdf`. (An earlier note here claimed the receipts had no PDF attachment — that was a bad reading of the MIME tree, corrected in 7.7.)

Still open: each claim needs a `condition_text` from Justin before it can draft; Echo's #25 stays blocked on Bow Wow's undefined claim process; Aari's $35 is below the $150 per-condition excess. Checked for double-claiming: claim #9 shares the 2026-06-19 invoice date but is Echo's separate $3,147.00 charge with a $580.74 invoice — no overlap.
