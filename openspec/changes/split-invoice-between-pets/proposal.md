## Why

One invoice can cover two pets. The Shire Veterinary Caringbah charge of **$407.56** (2026-07-06, claim #1, invoice matched 2026-07-27) was for Aari **and** Echo — Aari's share $35 — but a claim carries exactly one `pet_id`, so the ASSIGN PET card offered Aari *or* Echo and nothing else. Assigning either pet claims the whole $407.56 against that pet: over-claiming for Aari, and silently losing Echo's share.

Justin tried to say so in chat, four times on the evening of 2026-07-27, and got nothing done. The message log (`telegram_messages` 94–100) shows four independent failures on that one path:

| # | What he sent | What happened |
|---|---|---|
| 94 | "This is actually split between echo and Aari. He was $35 out of this" | Groq returned `403 Access denied`. `_try_model` retries only 429s and malformed tool calls, so a single transient 403 ended the turn with **zero retries and no fallback model** (`llm_calls` #1659 — one row). |
| 96 | (edited 94 to name Aari's $35) | Handler crashed: `'NoneType' object has no attribute 'text'`. `filters.TEXT` matches `edited_message`, where `update.message` is `None`. Logged as kind `other` with an empty summary, so the message is invisible in the log too. |
| 97 | "This is actually split between echo and Aari. Aari cost was $35 out of this" | Answered "I need the reference" — he was **replying to the ASSIGN PET card for claim #1**, whose payload carries `Claim #1` and `setpet:1:1`, but reply context never reaches the agent, and no `propose_*` tool takes a claim id. |
| 99 | "The invoice can be used for both Aari and Echo. His cost would be $35" | 3× `tool_use_failed`: with no way to name the claim and no split tool, the model emitted the *schema description strings* as arguments (`claim_detail={"claim_id": "the claim id of the claim you are referring to"}`, `propose_assign_pet,{"merchant":"vet/merchant name to locate the unassigned claim",...}`). Retried verbatim 3 times, then "try rephrasing the question". |

Result: 21 days after the charge, claim #1 still has `pet_id IS NULL`.

## Revision (2026-07-27, after reading the actual documents)

The premise above was wrong in one respect, and the correction is recorded rather than edited away. The $407.56 charge did not pay **one** invoice covering two pets — it paid **two separate invoices**, forwarded by Justin's wife as two emails that afternoon: SHV49c1622284e5 (Aari, visit 19 Jun, **$35.00**) and SHVd5b232905fdb (Echo, visit 30 Jun, **$369.33**), with the remaining **$3.23** being card surcharge.

Two consequences:

- Claim #1's existing match is **wrong**: it points at a payment-list email (extracted as three bare amounts, no items, no services), which is why it could never draft. It needs the ❌ Wrong invoice path and a re-match.
- The apportionment must be **per invoice**, not per share: each claim carries its own email, invoice number, itemization and pet. That is now automatic in the matcher (`_complement_for` / `_apply_match`), and it exposed a second defect — `INVOICE_MATCH_WINDOW_DAYS` is 3 measured on the *service* date, so **both** real invoices were being rejected for the charge that paid them. A receipt's own payment line now satisfies date plausibility.

The manual share-based split still ships: one document billing both pets is a real shape (the same vet's bulk history email does it) and only Justin can say how such a bill divides.

## What Changes

- **New capability: an invoice's claimable amount can be apportioned between pets.** One charge → one claim per pet, each carrying its own share of the claimable subtotal. Shares are supplied by Justin and confirmed by a tap; nothing is inferred from the invoice.
- Echo's share becomes a real claim rather than a rounding error. Echo is insured with **Bow Wow Insurance** and `claim_process_defined = 0`, so her claim lands in the existing blocked pool with the existing "claim process not yet defined" flag — visible, unclaimable, and not silently discarded.
- The agent gets a split tool and, for the first time, **the claim the user is replying to**: a reply to a bot card resolves to that card's claim id, so "this claim" works.
- Every `propose_*` tool accepts an explicit `claim_id`, removing the placeholder-argument failure mode above.
- `edited_message` updates are handled as messages instead of crashing, and get a real `kind` in the message log.
- A transient non-429 provider error (403/408/5xx/network) is retried like the 429 path instead of ending the turn on the first attempt.
- Ceiling rule holds (ADR-0007): shares must not exceed the invoice's claimable subtotal, and each claim's form carries only its own share.
- Not allowed after submission: a claim already `sent`/`acknowledged` or later cannot be split — that has to go to Petcover as a correction.

## Capabilities

### New Capabilities
- `multi-pet-invoice-split`: one invoice apportioned across pets — per-pet claim rows, per-claim claimable share, share arithmetic and its guards, and what happens when one pet's insurer has no defined process.

### Modified Capabilities
- `conversational-agent`: act tools take an explicit claim id; the claim referenced by a replied-to card is part of the turn's context; a split can be proposed from chat.
- `telegram-bot`: an edited text message is processed rather than crashing the text handler, and is recorded with a truthful kind/summary; a reply to a card identifies that card's claim.
- `llm-backend`: transient provider failures other than 429 are retried before the turn is declared failed.
- `invoice-matching`: one invoice carried by several claims is legitimate when the claims are a deliberate per-pet split — the "never re-matched" rule is about accidental duplication, not this.

## Impact

- `app/openclaw/claim_forms.py` — new split entry point beside `assign_pet`; per-claim claimable override read by `_claimable_for`.
- `app/openclaw/agent.py` — `propose_split_between_pets`, `claim_id` on the `propose_*` tools, reply-context line in the turn.
- `app/openclaw/telegram_bot.py` — `effective_message` in the text handler and ack, reply-to-card claim resolution, `split:` confirm action.
- `app/openclaw/message_log.py` — `_describe` covers `edited_message`.
- `app/openclaw/llm.py` — retry classification in `_try_model`.
- `app/openclaw/invoice_matching.py` — `_already_claimed` unaffected by design; covered by a test that pins the intent.
- No schema change: each `vet_claims` row already owns its own `invoice_data`, so the per-pet share is stored there (avoids manual live `ALTER TABLE`).
- `README.md` (matching/claim walkthrough), `docs/adr/` (new ADR for the split model), `openspec/specs/` (sync before archive).
