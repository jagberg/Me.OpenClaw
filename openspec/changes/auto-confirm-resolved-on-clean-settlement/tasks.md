## 1. Hook into settlement validation

- [x] 1.1 In the caller of `_validate_settlement` (inside `process_reply`, around `claim_status.py:1135`), capture the precise "clean" condition: `claim["status"] == "settled"` AND `detail.get("paid_amount") is not None` AND `_validate_settlement(...)` returned `None` — NOT "returned None" alone, which also covers a dollar-less event with nothing to validate (see design.md's Decisions). Implemented as `outcome["state"] == "settled"` (the post-transition state `apply_event` returns) rather than re-reading `claim["status"]`, since `claim` is the pre-event row fetched before `apply_event` runs — see "Design deviations" in the final report.
- [x] 1.2 On that precise clean result, find any currently-unresolved `info_requested`/`suspended` event on the same claim (reuse whatever `dashboard_lists()["needs_action"]`/`unresolved_event_claim_ids` already uses to determine "unresolved" — don't re-derive). Implemented by calling `confirm_resolved` directly: its own "nothing outstanding to confirm" check (`claim_status.py:1622-1626`) already *is* that determination, scoped to one claim, so a second lookup would duplicate the eligibility rule rather than reuse it.
- [x] 1.3 Call the existing `confirm_resolved(claim_id, detail=..., email_id=...)` for each such claim, with a detail noting this fired from clean settlement (mirrors `vet-reply-auto-resolves-info-request`'s own auto-confirm detail shape)
- [x] 1.4 A mismatch result, OR a dollar-less event (nothing validated), makes NO change here — the existing manual-confirm requirement is untouched

## 2. One-off backfill (existing backlog, e.g. claim #13)

- [ ] 2.1 Throwaway script (not committed as a permanent module/endpoint), run via `docker exec` into the running app container
- [ ] 2.2 Eligibility over full event history: `status == "settled"`, at least one historical `approved`/`settled` event carried a non-null `paid_amount` with `_validate_settlement` returning `None` for it, current `flag IS NULL`, and an unresolved `info_requested`/`suspended` event still exists
- [ ] 2.3 Dry-run mode first: print each candidate with claim id, pet, reference/Sr, the outstanding request's subject/`owed_by`/requested document, and the settlement figures (claimed/paid/excess/age-contribution) that make it clean
- [ ] 2.4 Only after explicit go-ahead, re-run to actually call `confirm_resolved` on the reviewed list
- [ ] 2.5 Delete the script once run — it is not part of this change's shipped code

## 3. Tests

- [x] 3.1 Claim with unresolved `info_requested` reaches `settled` clean (real `paid_amount`, Check A/B pass) → auto-confirmed via the same path as an explicit tap (`test_settled_clean_auto_confirms_outstanding_info_request`)
- [x] 3.2 Claim with unresolved `info_requested` reaches `settled` with a mismatch → NOT auto-confirmed, stays on the needs-action list exactly as today (`test_settled_with_mismatch_does_not_auto_confirm`)
- [x] 3.3 A dollar-less `settled` event (no `paid_amount`) → NOT auto-confirmed, even though `_validate_settlement` returns `None` for it (`test_dollarless_settled_event_does_not_auto_confirm`)
- [x] 3.4 Auto-confirm fires the same way regardless of `owed_by` (vet / justin / petcover) (`test_settled_clean_auto_confirms_regardless_of_owed_by`)
- [x] 3.5 A claim not yet settled (still `acknowledged`/`suspended`) is unaffected — no auto-confirm before settlement (`test_not_yet_settled_no_auto_confirm`, an `approved`-only clean reply)
- [x] 3.6 Idempotent: replaying the same clean settlement event twice does not double-confirm or double-record (`test_settled_clean_auto_confirm_is_idempotent_on_replay`)

## 4. Docs

- [x] 4.1 Sync the MODIFIED requirement into `openspec/specs/claim-status-tracking/spec.md`, replacing the existing requirement text in place (not a second copy)
- [x] 4.2 Note in `app/openclaw/CLAUDE.md` that `confirm_resolved` now has a second automatic caller (settlement-clean), alongside the vet-reply one
