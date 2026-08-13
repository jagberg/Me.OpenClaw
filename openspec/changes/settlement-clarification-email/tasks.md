## 1. Data model

- [x] 1.1 Add `clarification_batches` table (batch id, gmail draft id, gmail thread id, created_at, sent_at nullable) plus a join to the `vet_claims` rows it covers
- [x] 1.2 Add `awaiting_petcover_clarification` as a recognized pending-action state alongside `info_requested`/`suspended` in `claim_status.py` (`_ACTION_META`, `_action_kind`, transitions) — entered only when a claim is queued into an open draft, not while its pre-send review card is merely showing
- [x] 1.3 Add `clarification_requested` event type to the append-only `claim_status_events` vocabulary (no other new event type needed — the post-reply "still unresolved" case writes to `flag` only)

## 2. Settlement-review card

- [x] 2.1 Write the eligibility query: open, undismissed Check B assessment-difference flags and unrecorded-claimable-subtotal flags (excludes Check A arithmetic differences)
- [x] 2.2 Card template (extend `.qcard` pattern, `templates/index.html:194-210`): claim id, reference/serial, pet, condition (or "not set"), submitted vs. assessed figures, line items (`invoice_data.items`, reusing the `NON_CLAIMABLE_KEYWORDS` check from `claimable_amount()`) when fewer than 5, else a PDF link
- [x] 2.3 New route `GET /claims/{id}/invoice` serving `vet_claims.invoice_file_path` (`FileResponse`) — nothing to reuse, net-new
- [x] 2.4 Two actions on the card: `POST /claims/{id}/settlement/acceptable`, `POST /claims/{id}/settlement/more-info`

## 3. Acceptable (terminal dismiss)

- [x] 3.1 `acceptable` endpoint dismisses the flag via the existing manual-dismiss mechanism, recording the card's figures — reuse, don't duplicate, the dismissal path `settlement-validation` already has
- [x] 3.2 Confirm dismissal is one-way per existing semantics: a later, distinct settlement event on the same claim still validates and can flag independently

## 4. More Info — pre-send (queues clarification draft)

- [x] 4.1 `more-info` endpoint: if the claim is not yet `awaiting_petcover_clarification`, find-or-create the single open `clarification_batches` draft (`drafts().create`/`update`, never `send()`), append this claim's details to the draft body in Justin's tone (mirror `INVOICE_REQUEST_BODY`)
- [x] 4.2 Record `clarification_requested`, persist the draft's thread id against the claim, move claim to `awaiting_petcover_clarification`
- [x] 4.3 Multiple claims queued before the draft is sent all land in the same open draft (dedupe by open/unsent batch, not by claim)

## 5. Reply correlation + auto-resolve

- [x] 5.1 Extend `pipeline.poll_petcover_status` (or a sibling function) to check incoming mail's thread id against open `clarification_batches` before falling through to the general reference/Sr/pet-condition router
- [x] 5.2 Add `llm.extract()` call scoped to `{claim identifier, confirmed amount}` pairs from the reply body — no other fields, no guessing
- [x] 5.3 Per extracted pair: exact-match against the claim's recorded `claimable_subtotal` → same terminal dismissal as Acceptable, recording the reply's figures; no match/ambiguous → leave `awaiting_petcover_clarification`, resurface the review card with the reply's figure shown
- [x] 5.4 Claims in the batch not addressed in the reply remain `awaiting_petcover_clarification`

## 6. More Info — post-reply (no further automation)

- [x] 6.1 `more-info` endpoint: if the claim is already `awaiting_petcover_clarification` (i.e. this is the resurfaced card after an unresolved reply), write a `flag` note that Justin reviewed it and it's still unresolved — no draft, no new event type, state unchanged

## 7. Dashboard surfacing

- [x] 7.1 Render `awaiting_petcover_clarification` claims distinctly from `info_requested`/`suspended` ("waiting on Petcover" vs "needs your action")
- [x] 7.2 Pre-send flagged claims and resurfaced post-reply claims both render the same card component

## 8. Tests

- [x] 8.1 `app/tests/test_core.py`: eligibility query includes Check B + unrecorded-subtotal, excludes Check A
- [x] 8.2 Acceptable dismisses without rewriting `claimable_subtotal`/paid amount; a later distinct event can still flag the same claim independently
- [x] 8.3 First More Info click creates a draft and moves claim to `awaiting_petcover_clarification`; a second claim's More Info click before the draft is sent joins the same draft
- [x] 8.4 Reply exact-match resolves identically to an Acceptable click
- [x] 8.5 Reply with no/ambiguous match resurfaces the card, leaves state untouched
- [x] 8.6 Partial-batch reply resolves only the addressed claims
- [x] 8.7 More Info on an already-`awaiting_petcover_clarification` claim writes a flag note only, no new draft/event
- [x] 8.8 Unrelated event on an `awaiting_petcover_clarification` claim does not clear it

## 9. Docs

- [x] 9.1 Sync deltas into `openspec/specs/settlement-validation`, `openspec/specs/claim-status-tracking`, and add `openspec/specs/settlement-clarification-email`
- [x] 9.2 Update README's matching-algorithm section with the clarification loop
- [ ] 9.3 If Justin approves a second `send()` exception during review: write the ADR (mirrors ADR-0030), update the CLAUDE.md hard-rule line, and extend `test_nothing_but_the_one_named_exception_can_send_mail_and_no_tool_offers_to`
      **Blocked on Justin's explicit sign-off** — not authorized in this implementation session. This change ships draft-only (design.md's default); no ADR written, no CLAUDE.md hard-rule edit made, no second `send()` call site added anywhere.

## 10. Telegram surface (added after initial ship — original scope was dashboard-only)

**Correction (2026-08-13): this change was marked complete without a Telegram surface.** The dashboard shipped the settlement-review card; Telegram/gateway taps for these same claims still showed the pre-existing generic `dismiss_mismatch` button ("👍 Reviewed") with no way to trigger More Info at all, and the new `awaiting_petcover_clarification` action kind had no button case whatsoever (silently rendered no buttons). Found live, post-deploy, by Justin.

- [x] 10.1 New verb `moreinfo` in `button_commands.BUTTON_COMMANDS` and `gateway-plugin/index.js`'s `COMMANDS` (same position in both — the order-preserving guard test checks this)
- [x] 10.2 `commands.handle_more_info` → `claim_status.queue_clarification`, wired into `dispatch()`
- [x] 10.3 `commands._clarification_buttons(claim_id)` — the shared Acceptable/More Info pair (ADR-0031: one card, one pair of actions, reused everywhere it appears, not a Telegram-specific third UI)
- [x] 10.4 `_action_buttons`: `dismiss_mismatch` now checks `claim_status._clarification_eligible(flag)` — eligible gets the pair, Check A (arithmetic) keeps the old single "Reviewed" button; `awaiting_petcover_clarification` gets the pair unconditionally
- [x] 10.5 `commands._ACTION_EMOJI` and `claim_card._STATUS_COLOURS`/`_ACTION_COLOURS` — `awaiting_petcover_clarification` entries (the status/action-card colour dicts were already covered for the dashboard; the action-card one was not)
- [x] 10.6 Tests: button split by eligibility, `awaiting_petcover_clarification`'s buttons, `/moreinfo` dispatch end-to-end — plus the two pre-existing guard tests (`test_every_card_button_names_a_command_the_plugin_registered`, `test_the_plugin_registers_exactly_the_commands_a_button_may_emit`) passed unmodified, proving the new verb is registered consistently
