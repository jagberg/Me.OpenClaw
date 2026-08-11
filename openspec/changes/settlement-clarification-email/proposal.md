## Why

`claim_status._check_what_petcover_assessed` (Check B) flags a mismatch between Petcover's stated `claimed_amount` and our recorded `claimable_subtotal` on 5 of 10 live letters, but today that flag just sits on the dashboard with no path forward except Justin manually re-reading letters and emails to guess what Petcover actually needs. He can't tell, per claim, whether the gap is a data error on our side, a genuinely different assessment, or missing detail Petcover never sent. Right now the only way to close it is to ask Petcover directly — and there's no mechanism to do that, or to use their answer once it arrives.

## What Changes

- New dashboard/pipeline action that gathers every claim currently flagged by Check B (or otherwise stuck needing settlement clarification) into **one consolidated email to Petcover**, asking them to confirm the assessed amount and breakdown per claim/serial.
- Email is drafted in Justin's voice (short, first-name-free, one ask per claim, matching the tone of `invoice_matching.INVOICE_REQUEST_BODY`).
- **BREAKING (policy):** this is a second `send()` call site, alongside ADR-0030's `send_invoice_request`. CLAUDE.md's hard rule currently permits exactly one named exception; this proposal asks Justin to extend it to two, or fall back to a Gmail draft he sends himself. Needs his explicit call in this proposal review — noted in Impact below, defaulting to **draft-only** unless he says otherwise, since draft-only carries no policy risk and this review is the point to decide it.
- New inbound-reply handling: when Petcover replies to the clarification thread, parse the confirmed amount(s)/serial(s) per claim referenced, and where the reply resolves the ambiguity unambiguously, auto-apply it (update `claimable_subtotal`/serial link/status) via the existing `claim_status.apply_event` writer — never a second writer.
- Where the reply is ambiguous or partial (some claims answered, not others; numbers still don't reconcile), leave those claims flagged for Justin same as today — no silent guessing.
- New claim-status pending state: `awaiting_petcover_clarification`, surfaced on the dashboard like other "needs your action" cards, distinguishing "we're waiting on Petcover" from "you need to do something".

## Capabilities

### New Capabilities
- `settlement-clarification-email`: consolidated multi-claim email to Petcover requesting settlement-figure confirmation, its trigger/batching logic, and auto-processing of their reply back into claim state.

### Modified Capabilities
- `settlement-validation`: Check B mismatches gain a resolution path (request clarification, apply confirmed reply) instead of being a terminal flag.
- `claim-status-tracking`: new `awaiting_petcover_clarification` status/pending-action kind and its transitions in/out.
- `email-ingestion`: new inbound category (Petcover clarification reply) parsed by `poll_petcover_status`/`process_reply`, and — only if Justin approves the send exception above — a second permitted outbound `send()` site.

## Impact

- `app/openclaw/claim_status.py`: new pending-action kind, `_ACTION_META` entry, transition into/out of `awaiting_petcover_clarification`, reply-parsing branch in `process_reply`.
- `app/openclaw/invoice_matching.py` or `pipeline.py`: new function to batch flagged claims and build/send (or draft) the consolidated email — mirrors `_maybe_send_invoice_request`'s trigger pattern.
- `app/openclaw/pipeline.py`: `poll_petcover_status` gains a route for clarification replies alongside its existing reference/Sr/ack routing.
- Dashboard (`templates/index.html` + `main.py` view): new card for the pending-clarification state.
- `app/tests/test_core.py`: extend `test_nothing_but_the_one_named_exception_can_send_mail_and_no_tool_offers_to` if a second send() site is approved; otherwise no change needed there.
- Docs: ADR needed if the send exception is approved (mirrors ADR-0030); CLAUDE.md's hard-rule line updated either way.
