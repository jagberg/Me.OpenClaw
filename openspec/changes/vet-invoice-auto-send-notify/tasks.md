## 1. Send path

- [x] 1.1 Rename `invoice_matching.draft_invoice_request` to `send_invoice_request`; swap `service.users().drafts().create()` for `service.users().messages().send()`. Vet-email lookup and body templating unchanged.
- [x] 1.2 Wrap the send call so any exception is caught and returns/raises in a way the caller can flag (never a silent no-op).
- [x] 1.3 Rename `pipeline._maybe_draft_invoice_request` to `_maybe_send_invoice_request`. On success: `flag = "invoice_request_auto_sent"`, `invoice_request_sent_at = now`, `draft_id` left `NULL`. On failure: `flag = f"invoice request send failed: {exc}"`. On no vet email: unchanged existing flag text. (Flag renamed from the originally-planned `invoice_request_sent` to `invoice_request_auto_sent` mid-implementation — collides with an existing, unrelated dashboard action-kind literal `"invoice_request_sent"` in `claim_status.py`'s `_action_kind_from_row`, which means something different: "ask Justin to confirm he manually sent a legacy draft.")
- [x] 1.4 Add a code comment at the flag assignment site (and at `notify_claim_states`'s exclusion list, task 2.2) stating that `invoice_request_auto_sent` must never be added to that exclusion list — it is the notify trigger.

## 2. Notification

- [x] 2.1 Add a branch in `pipeline._summarize_group`'s `pending_match` case for `flag == "invoice_request_auto_sent"`: render `"✅ {merchant}: invoice request sent — ${amount} ({date})"`, no `⚠` prefix.
- [x] 2.2 Confirm (add the paired comment from 1.4) that `notify_claim_states`'s existing `pending_match` OR-clause exclusion list does not list `invoice_request_auto_sent` — no query change needed, just guard against future drift.
- [x] 2.3 Confirm no `markup` (action button) is attached for this flag/status combo — verify none of the existing `elif` branches in `notify_claim_states` match `status == "pending_match"` regardless of flag. Also confirmed `claim_status._action_kind_from_row` doesn't match this flag either (falls through to `assign_pet`/`set_condition`/`None`, same as any other pending_match claim) — no unwanted dashboard action card.

## 3. Tests

- [x] 3.1 `app/tests/test_core.py`: `send_invoice_request` calls `messages().send()` not `drafts().create()` (stub the Gmail service).
- [x] 3.2 `_maybe_send_invoice_request` sets `flag="invoice_request_auto_sent"`, `invoice_request_sent_at` non-null, `draft_id` null, on a successful send.
- [x] 3.3 `_maybe_send_invoice_request` on a raised exception sets a visible failure flag, not a silent no-op, and leaves `invoice_request_sent_at` null.
- [x] 3.4 `notify_claim_states` pushes a notification for a `pending_match` claim flagged `invoice_request_auto_sent` (regression test for the string-coupling risk in design.md), with no button attached.
- [x] 3.5 `notify_claim_states` still stays silent for a claim flagged `invoice_request_drafted` (legacy path unaffected).
- [x] 3.6 No separate `test_telegram.py` test added — already covered by 3.4's `markups[0] is None` assertion plus the existing `test_nothing_but_the_one_named_exception_can_send_mail_and_no_tool_offers_to` guard (updated for ADR-0030, see below), so a redundant test was skipped.
- [x] 3.7 Ran both `app/tests/test_core.py` and `app/tests/test_telegram.py` — ALL TESTS PASSED, both suites.

**Unplanned but required:** `test_nothing_but_the_one_named_exception_can_send_mail_and_no_tool_offers_to`
(renamed from `test_nothing_in_the_app_can_send_mail_and_no_tool_offers_to`, `app/tests/test_core.py`) was an
existing hard-gate test that greps the whole `openclaw` package for any Gmail send call and asserts zero exist —
it would have failed against this change by design, since it predates the ADR-0030 exception. Updated to allow
exactly one send call site (`invoice_matching.py`) and assert it's actually present (so the guard can't pass by
the send path being silently deleted), while every other file is still held to zero.

## 4. Docs and decision trail

- [x] 4.1 Write the ADR recording Justin's override of the never-send hard rule, scoped to invoice-request emails only (done directly in this conversation, ahead of `/opsx:apply`, per decision-trail's "capture the why at the moment of decision").
- [x] 4.2 Update CLAUDE.md's "Never send email" hard-rule bullet to name the one exception explicitly, pointing at the ADR.
- [x] 4.3 Amend `gmail-isolation-boundary`'s spec Purpose section (which currently reads "'never send' is enforced only by the absence of `send()`") to note the one exception, cross-linking the ADR — amend in place per `docs/adr/README.md`'s convention, since the boundary's actual requirements (credential isolation, no agent tool reach) are unchanged.
- [x] 4.4 Update `app/openclaw/CLAUDE.md` module map / relevant docstrings referencing "drafts (never sends)" language for `invoice_matching`.

## 5. Verification

- [x] 5.1 Live-verified (read-only, 2026-08-11) against the real DB (`C:\code\Me.OpenClaw\app\data\openclaw.db`): all 4 current `pending_match` claims (#4, #5, #17, #26) already carry a non-null `draft_id` from the legacy path, so `_maybe_send_invoice_request`'s existing guard (`if invoice_request_sent_at or draft_id: return`) skips every one of them — nothing sends unexpectedly on deploy.
- [ ] 5.2 NOT YET DONE — requires an actual deploy. After deploy, verify one real send end-to-end (or the next real claim that ages past the window) produces a real sent Gmail message and a real Telegram notification, per this repo's "prove the path works, a silent result is not a finding" rule.
