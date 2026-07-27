## Context

Claim #1 (The Shire Veterinary Caringbah, $407.56, charge 2026-07-06) got its invoice matched on 2026-07-27 with `pet_id IS NULL`. The invoice covers Aari and Echo; Aari's share is $35. The ASSIGN PET card offers one button per pet and nothing else, so either choice is wrong.

Facts the design has to sit on, all verified against the live DB read-only on 2026-07-27:

- `vet_claims` already carries **its own** `invoice_data` JSON per row, and `claim_forms._claimable_for` reads `invoice_data.claimable_amount` (falling back to `amount`). A per-claim share therefore needs **no schema change** — which matters, because a live column addition needs manual `ALTER TABLE` against `app/data/openclaw.db` (`CREATE TABLE IF NOT EXISTS` won't touch an existing table).
- `pets`: Aari → Petcover, `claim_process_defined = 1`, excess $150, cap $10,000. Echo → Bow Wow Insurance, `claim_process_defined = 0`, no claim email. Six claims are already blocked on that undefined process, with a card that says so.
- `split_proposals` is the **opposite** concept — one invoice paid over several bank charges (`claim_ids` merge). Reusing that table would conflate merge with split; the per-pet split is not a matcher proposal at all, it's an operator instruction.
- `invoice_matching.claimable_amount` returns the invoice total when there is no itemization. Claim #1's `invoice_data` has `items: []` and `claimable_amount: 407.56`, so the share arithmetic has to work off the stored claimable subtotal, not off line items.
- `agent._find_claims` filters on pet / reference / status / merchant / unassigned / date only — no `claim_id`. `_propose` collapses matches to a single target or gives up. Nothing in the turn knows what message Justin replied to.
- The four failures in the proposal's table are four independent defects on one path. Fixing only the missing capability would leave the same conversation failing on the 403, the edit, or the placeholder arguments.

## Goals / Non-Goals

**Goals:**
- Justin describes a shared invoice in a Telegram reply and gets one Confirm tap that records both pets' shares.
- Echo's share becomes a visible blocked claim, not a silent loss.
- The exact 2026-07-27 conversation (messages 94–99) succeeds end to end.
- No schema change; no manual live DDL.

**Non-Goals:**
- A dashboard UI for splitting. The trigger is chat + tap; the dashboard already shows the resulting claims. Add it if the chat path proves awkward.
- Undo of a confirmed split. Guarded instead: pre-`sent` only, and both shares are visible immediately. Tracked in `openspec/BACKLOG.md`.
- Splitting by line item ("the ultrasound was Echo's"). Requires per-item pet attribution the invoice does not reliably state, and Justin can always give amounts.
- Defining Bow Wow's claim process. Separate work; Echo's claim stays blocked either way.
- Answering the standing "do X with no tool for X becomes a task" limitation in `conversational-agent`. This change removes one instance of it (split), not the class.

## Decisions

**1. A split is N claim rows, one per pet — not a new share table.**
Everything downstream is already per-pet: form filling, batching (≤4 same pet), excess/cap, correlation, status events. A `claim_pet_shares` table would mean teaching every one of those about a claim with two pets. One row per pet costs one insert and changes nothing else. Alternative rejected: a `pet_shares` JSON blob on the claim — same objection, and it would make `pet_id` lie.

**2. The share lives in each row's `invoice_data.claimable_amount`.**
Zero DDL, and it lands exactly where `_claimable_for` and the form already look, so the PDF carries the share with no new code path. The sibling row copies `matched_email_id`, `invoice_data` (with its own `claimable_amount`), `invoice_file_path` and `claim_file_path`-less state from the original, keeping `invoice_number`/`amount` intact so invoice identity is unchanged. Record the split in the row so it is not mistaken for a mis-extraction: `invoice_data.split_note` naming the sibling claim ids and the full claimable subtotal. Alternative rejected: a new `claim_share_amount` column — cleaner to read, but needs manual `ALTER TABLE` on the live DB for something the JSON already holds.

**3. Shares are stated, with exactly one derivable remainder.**
Two pets and one amount is the real utterance ("Aari cost was $35 out of this"), so `remainder = claimable_subtotal - stated`, computed in Python, never by the model. Two or more unstated → refuse and ask. Sum > subtotal (+1c) → refuse with both figures. Sum < subtotal → proceed, flag the unapportioned remainder like the existing `possible additional invoice — unexplained $X`.

**4. The transaction is not split.** `transaction_id` stays the same on every sibling: one charge, one bank row, and the ceiling applies to the sum of shares (ADR-0007). The dashboard will show two claims against one transaction, which is what happened in reality.

**5. Reply context comes from the replied-to message, parsed for `Claim #N`.**
Every card names the claim in its text or caption (`telegram_bot` gotcha: PDF alerts have caption only), and `#N` is a one-line regex. Callback data (`setpet:1:1`) is a fallback for a card without the text. The resolved id is passed to `agent.handle_message` as a separate argument and injected as one context line, not concatenated into Justin's words — otherwise the model quotes it back at him.
Alternative rejected: threading a conversation-state machine per card. `_pending_condition` / `_pending_split` already show what that costs, and reply metadata is free.

**6. Every `propose_*` tool takes `claim_id`.**
`_find_claims(claim_id=…)` plus `claim_id` in each tool's schema. This is the direct fix for the observed failure where the model, having no way to name the claim, passed the schema's description strings as values. Cost: ~1 line of schema per tool against a 100k-token/day budget, guarded by the existing `test_tools_schema_stays_small`.

**7. `edited_message` uses `update.effective_message`.**
One accessor in `on_text_reply` and `_ack_user_message`; `message_log._describe` falls back to `effective_message` and prefixes the summary with `edit:`. Rejected: `filters.UpdateType.MESSAGE` to exclude edits — it stops the crash by silently ignoring corrections, which is worse than the crash.

**8. Retry classification inverts: retry unless the failure is provably unretryable.**
`_try_model` currently retries only 429 and `tool_use_failed`, so the 403 got one attempt. New rule: fail fast on a per-day cap (switch model) and on a non-`tool_use_failed` 400 (our own request shape); retry everything else with the existing backoff. A whitelist of retryable statuses would have to be extended by the next unknown edge response — this is the same reasoning as `_assistant_turn`'s field whitelist, applied in the opposite direction.

**9. `drafted` claims: clear the draft, re-draft per share.**
A draft stating $407.56 must not be sendable after the split is confirmed. Reuse the existing pre-draft reset (the `unmatch`/re-draft path) rather than editing a draft in place, then let `process_claim` re-draft each splittable claim. `sent` and later are refused outright.

## Risks / Trade-offs

- **A wrong split leaves a stray claim row and no undo** → refuse post-`sent`; the confirm message names every resulting claim, share and pet; the stray is visible on the dashboard and in `/actions`. Undo goes in BACKLOG.
- **Petcover sees a $35 claim against a $407.56 invoice** → this is the ADR-0007 model already in use (claimable subtotal ≠ charge) and the invoice pages are attached in full, so their assessor sees the same arithmetic. Watch the first settlement for an info-request about the difference.
- **`invoice_data.split_note` is an untyped JSON convention** → asserted in the smoke suite and documented in `app/openclaw/CLAUDE.md`; the day it needs querying is the day it earns a column.
- **Broader retry means a genuinely-down provider costs 3 attempts per model** → bounded by the existing `MAX_RETRIES` and backoff, and every attempt is already logged to `llm_calls`.
- **Reply-context claim id could target the wrong claim if a card shows several ids** (submission-level `mark_sent` cards name every member) → take the id only when the replied-to message names exactly one, else supply none and let the existing ambiguity path ask.
- **The agent proposes a split when Justin meant per-item conditions** (`/mark` already splits an invoice across *conditions* — same word, different axis) → the tool description names pets and amounts explicitly, and the confirm message states the shares before anything is written.

## Migration Plan

No schema change, so deploy is the normal `./scripts/deploy.ps1` from the worktree (stamps `APP_VERSION`; a bare `docker compose up` mistags `telegram_messages`). Existing claims are untouched — a claim with no split note behaves exactly as before. Rollback is the previous image: split siblings created in the meantime remain as ordinary claims with reduced claimable amounts, which is still correct, just no longer re-splittable.

First live exercise is claim #1 itself: Aari $35, Echo $372.56, Echo's claim expected to land blocked on Bow Wow's undefined process.

## Open Questions

- Which pet did Justin mean by "$35"? Message 97 says "Aari cost was $35 out of this"; message 99 says "His cost would be $35" (Echo is female, so "his" is likely a slip). **Assumption for the first live run: Aari $35, confirmed at the tap** — the Confirm message states both shares, so a wrong reading costs one tap, not a wrong claim.
- Should the ASSIGN PET card gain an explicit "Shared invoice" button that prompts for amounts, rather than relying on Justin knowing he can reply? Deferred until the reply path has been used once.
