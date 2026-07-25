# Tasks

## 1. The group id

- [x] 1.1 `claim_status.submission_group_id(claim_ids)` → `S<ids ascending joined by +>`, `S<id>` for a lone/draftless claim. Sorted, so read order can't change the token
- [x] 1.2 Agrees with `pipeline._batch_key`'s grouping for `drafted` claims — the one state where both rules are live at once. No refactor of the four existing call sites (`_batch_key`, `_submission_key`, `submissions_awaiting_reply`, `agent._single_target`); they keep working as they are

## 2. Collapse submission-level actions

- [x] 2.1 `SUBMISSION_LEVEL_ACTIONS = ("mark_sent",)` next to `ACTION_PRIORITY`, with the reasoning in a comment rather than inferred from `draft_id IS NOT NULL`
- [x] 2.2 `_collapse_submissions()` folds those kinds by `draft_id`, emitting `claim_ids` + `claim_id` (lowest, the representative) + `group_id` + `members`
- [x] 2.3 Sort order unchanged — oldest charge first, `ACTION_PRIORITY` tiebreak. A collapsed entry sorts on its **oldest** member's date, not the representative's: a visit stops being claimable at a year, so the batch expires with its eldest
- [x] 2.4 Collapsed `amount` is the batch total, so `claim_card.render_actions_summary`'s "$ total charged" is unchanged (members are replaced, not added) — **no edit needed to `claim_card.py`**, and the caption/summary counts follow the collapsed list automatically because both derive from `pending_actions()`

## 3. Honest already-sent response

- [x] 3.1 `mark_sent` on a `sent` claim → `Submission S6+7 (#6, #7) was already marked sent — nothing to do.` `ok` stays `False` (nothing was done)
- [x] 3.2 Statuses past `sent` keep reporting their real status — verified with an `acknowledged` claim
- [x] 3.3 Success message now names the group too: `Submission S6+7 (#6, #7, 2 claims in this submission) marked sent — Petcover replies now tracked.` The group id *supplements* the `#id`s; the standing regression test on claim ids still holds

## 4. Surfaces

- [x] 4.1 `telegram_bot._action_card_text` renders a multi-claim action with the group id and one line per member — they differ in date, amount and condition, so one summary line would hide what's in the email
- [x] 4.2 `_action_keyboard` untouched — `sent:{claim_id}` on the representative already advances the group (verified by tapping it in a test)
- [x] 4.3 See 2.4 — no `claim_card.py` change was needed
- [x] 4.4 `pipeline._submission_label` leads with the group id until Petcover's reference exists, then the reference leads as before
- [x] 4.5 `_summarize_drafted`'s header carries the group id: `Aari's vet claim S6+7 — ready to send (2 items, $484.00)`

## 5. Tests

- [x] 5.1 `test_batched_mark_sent_is_one_action_per_submission` — one entry for the batch, both ids, group id, representative = lowest, total $484, oldest date, member conditions
- [x] 5.2 Same test — a solo `drafted` claim keeps its own entry and carries no `members`
- [x] 5.3 `test_only_submission_level_actions_collapse` — two batch siblings each flagged `settlement mismatch` stay two `dismiss_mismatch` entries; also pins `SUBMISSION_LEVEL_ACTIONS`
- [x] 5.4 `test_batch_action_card_is_one_card_naming_every_member` — one card, one button, group id + both `#id`s, tap advances both
- [x] 5.5 `test_second_tap_on_a_sent_batch_reads_as_a_no_op` — already-sent wording, no `isn't drafted`
- [x] 5.6 Same test — an `acknowledged` claim reports `acknowledged`, not already-sent
- [x] 5.7 `test_submission_group_id_is_order_independent` — `[7, 6]` and `[6, 7]` both give `S6+7`
- [x] 5.8 `tests/test_core.py` → **ALL TESTS PASSED**
- [x] 5.9 New `test_telegram.py` tests pass individually. **Pre-existing failure, not caused by this change:** `test_process_advances_ready_claim` fails at `HEAD` too (verified by stashing this change and re-running) — `claim_forms.process_and_report` returns `ok: False`. That file has no per-test isolation, so the run aborts there and the rest of `test_telegram.py` (including these new tests) never executes under a plain `python tests/test_telegram.py`. Worth its own fix; out of scope here

## 6. Live verification

- [x] 6.1 Read-only query of the live batches via `sqlite3.connect("file:data/openclaw.db?mode=ro", uri=True)` (ADR-0018). Three drafts hold multiple claims:
  - `19f9789889397dff` → #6 (`Raised ALT`, $351.50, 2026-05-18) + #7 (`Arthritis`, $132.50, 2026-04-17) — both `sent`
  - `19f8c0147e14f07a` → #8 + #22 — both `sent`
  - `19f6fdd2cf13fb75` → #18 `below_excess`, #19 `acknowledged`, #21 `settled`
- [x] 6.2 **Correction to what the proposal assumed:** there is currently **no `drafted` batch**, so no live `mark_sent` action exists to collapse — the earlier claim that `#18+#19+#21` would be the live test was wrong (all three are past `drafted`). The collapse is proven by tests built on the live shape (two charges, two dates, two conditions, one draft), not yet by a live card
- [ ] 6.3 Deploy with `./scripts/deploy.ps1` from `C:\Code\Me.OpenClaw-telegram-claimquery` — never bare `docker compose up`, which leaves `APP_VERSION` `unknown` and mistags every `telegram_messages` row
- [ ] 6.4 Confirm on the next real batch: `/actions` shows one card headed `S<a>+<b>`, one tap advances every member, and a second tap on an old card says "already marked sent". Until a new batch drafts, tapping the existing #6/#7 cards exercises the already-sent wording on its own

## 7. Docs

- [x] 7.1 `app/openclaw/CLAUDE.md` — recorded that `pending_actions` is per-claim *except* `SUBMISSION_LEVEL_ACTIONS`, and where that list lives
- [x] 7.2 `README.md` — no change needed; the group id is a label, and the process description doesn't name action cards
- [x] 7.3 No ADR: this implements the existing Submission concept rather than deciding anything new. The one decision that *would* need one — replacing the derived token with a stored `submission_id` — is deferred, with its trigger (a redo path that re-splits a drafted batch) recorded in `design.md` against the open `BACKLOG.md` question, and repeated as a `ponytail:` note on the helper itself
