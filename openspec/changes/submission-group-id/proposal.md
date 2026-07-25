## Why

Claims #6 and #7 are one submission — one claim form, one Gmail draft, one email to Petcover. Justin sent that single email and then had to tap "Mark sent" on two separate `/actions` cards, one per claim. The second tap did nothing useful: `claim_status.mark_sent` is already batch-aware, so the *first* tap advanced both claims, and the second returned `Claim #7 isn't drafted (status: sent)` — a rejection message for work that had already succeeded.

Two distinct defects behind one symptom:

1. **`pending_actions()` counts claims, not submissions.** It iterates `visit_ledger()` per claim and emits one `mark_sent` action each, so a batch of N claims produces N identical "Send Gmail draft" cards for one email. Verified live: three multi-claim batches exist right now (`#6+#7`, `#8+#22`, `#18+#19+#21`), so this is 3 duplicate cards on the current data, not a one-off.
2. **A submission has no name Justin can say.** Its only identity is the opaque Gmail `draft_id` (`19f9789889397dff`) until Petcover's reply teaches us a claim reference. Justin asked for "a group ID like a number or alphanumeric that's simple" so a batch can be referred to as one thing on a card, in a message, and in conversation with the agent.

## What Changes

- **A submission group id, derived not stored.** `S<ids ascending joined by +>` — `S6+7`, `S18+19+21`. Minted from the claims sharing a `draft_id`; a claim with no draft is its own submission (`S12`). No schema change, no manual live `ALTER TABLE`, and the token *contains* the claim ids Justin already acts by (`/mark 6 …`), so it composes with the existing "every message carries the claim #id" requirement instead of competing with it. Alternatives (a stored `submission_id` sequence, a hash of `draft_id`) are weighed in `design.md`.
- **`pending_actions()` collapses submission-level actions to one entry per submission**, carrying `claim_ids` and the group id. Only `mark_sent` is submission-level today; every other kind fires before the draft exists (no `draft_id` yet) or is per-claim by nature. So `/actions` sends one card with one button for #6+#7, and the count in the summary card ("N to action") stops double-counting.
- **`mark_sent` on an already-sent sibling reports the truth.** Instead of `isn't drafted (status: sent)`, it says the submission was already marked sent and names the group. Same `ok: False` (nothing needed doing), honest wording — matching `confirm_resolved`'s existing nothing-to-do precedent.
- **The group id appears wherever a submission is already named as a unit**: the drafted-batch notification (`pipeline._submission_label`), the `/actions` card, and the mark-sent result message.

## Capabilities

### New Capabilities

None. This sharpens an existing concept (the Submission) that `claim-form-automation`, `claim-status-tracking` and `telegram-bot` already model.

### Modified Capabilities

- `claim-status-tracking`: the shared outstanding-work derivation gains a requirement that submission-level actions yield one entry per submission, not per claim; and mark-sent's already-sent response is specified.
- `telegram-bot`: the actions view shows one tap-to-resolve card per submission, and messages naming a submission carry its group id.

## Impact

Code:
- `app/openclaw/claim_status.py` — `pending_actions()` (collapse), `mark_sent()` (already-sent wording), a `submission_group_id()` helper next to the existing `_batch_key`-shaped logic at `claim_status.py:186`.
- `app/openclaw/pipeline.py` — `_submission_label` (line 146) carries the group id; `_batch_key` (line 136) is the existing grouping rule the new helper must agree with rather than duplicate.
- `app/openclaw/telegram_bot.py` — `_action_card_text` / `_action_keyboard` handle an action carrying several `claim_ids`.
- `app/openclaw/claim_card.py` — `render_actions_summary` counts submissions consistently with the caption.
- `app/tests/test_core.py`, `app/tests/test_telegram.py` — batch fixtures already exist (`draft-batch-1`, `d-batch`).

No schema change, so **no manual live DDL** and no backfill — the constraint that makes stored ids expensive here (root `CLAUDE.md`: live schema changes need a hand-written `ALTER TABLE` against `app/data/openclaw.db`).

Not in scope, deliberately:
- The **conversational agent** inherits the collapse for free (its outstanding-work answer is required to come from `pending_actions()`), and `agent._single_target` already resolves a batch to one target. Naming the group in chat prose is a follow-on, not a requirement change.
- The **dashboard** keeps per-claim mark-sent buttons. The already-sent wording fix covers the confusion; grouping the dashboard list is a separate UI change nothing is blocked on.
- **Re-batching.** A derived id is stable only because nothing in the codebase can re-batch a drafted claim — which is exactly the open "What does 'redo claim #N' mean?" question in `openspec/BACKLOG.md`. If a redo tool ever re-splits a batch, the derived token changes and a stored `submission_id` becomes the right answer. Recorded in `design.md`, not pre-built.
