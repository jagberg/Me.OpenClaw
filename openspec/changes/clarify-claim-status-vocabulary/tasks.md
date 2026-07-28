Scope note: classification (`info_requested` vs `suspended`), Unicode-hyphen reference extraction and addressee resolution are **already shipped** in `4a7fb6d` (`vet-info-request-chase` groups 2–4) and are not repeated here. The `chase_vet` action kind, the deadline escalation, the register and the claim #2/#8 data repair remain that change's groups 0–1 and 5–8.

## 1. One definition of "what this claim needs"

- [x] 1.1 Extract the pure part of `claim_status._action_kind` into `_action_kind_from_row(claim)` — everything after the two set-membership checks (`open_split_claim_ids`, `unresolved_event_claim_ids`) — and have `_action_kind` call it. Behaviour unchanged; one definition, callable from a rendering path without DB queries.
- [x] 1.2 Test: `_action_kind` returns exactly what it returned before for a claim of each kind (guard that the extraction was mechanical).

## 2. The shared vocabulary

- [x] 2.1 New `openclaw/status_labels.py`: `LABELS: dict[str, str]` (status → wording) plus `label(claim) -> str`. No import from `claim_status` at module level beyond `_action_kind_from_row`; pure function of the row.
- [x] 2.2 `matched` derives from `_action_kind_from_row`: `set_condition` → "Needs condition", `assign_pet` → "Needs pet", `blocked_insurer` → "Blocked: no claim process", anything else → "Matched".
- [x] 2.3 Test: each `matched` case above, plus a `matched` claim with nothing outstanding still reading "Matched".
- [x] 2.4 `info_requested` wording splits on the `owed_by` already recorded by `vet-info-request-chase`: vet → **More vet info required**, Justin → **Petcover needs info from you**, unrecorded → **Info requested**. The word "suspended" appears in no info-request label.
- [x] 2.5 Test: all three `owed_by` cases, and that a genuinely `suspended` claim still reads "Suspended".

## 3. Delete the duplicates

- [x] 3.1 `claim_card.py`: point `_STATUS_LABELS` at the shared map, delete the "Mirrors templates/index.html's status_chip macro" comment, and re-key `_STATUS_COLOURS` by **status** instead of by label text (it currently keys on `"Info requested"`, `"Below excess"` etc., so a rewording would silently drop a colour).
- [x] 3.2 Expose the label to Jinja (template global, or a per-row field built where the ledger rows are assembled) and delete `templates/index.html`'s `labels` dict.
- [x] 3.3 `templates/basic.html`: replace the `needs` map with the shared label; keep `sev` (severity/colour, already status-keyed).
- [x] 3.4 `pipeline.py`: lifecycle notify text reads the shared map instead of its own wording.
- [x] 3.5 Grep for any remaining status→wording table and remove it or justify it in a comment.
- [x] 3.6 Test: `claim_card` and the ledger both resolve a chosen status through `status_labels` — a guard against a fourth map reappearing.
- [x] 3.7 Run both suites: `cd app && ./.venv/Scripts/python.exe tests/test_core.py` and `tests/test_telegram.py`.

## 4. Coordinate and verify

- [x] 4.1 Amend `vet-info-request-chase` task 5.5 to add `chase_vet`'s wording to `status_labels.LABELS` rather than to `claim_card.py` and `telegram_bot.py` separately — or, if its group 5 has already landed, absorb both of its labels into the map here and delete them from the renderers (design Decision 5).
- [ ] 4.2 Deploy from the worktree with `./scripts/deploy.ps1` (stamps `APP_VERSION`) and confirm `/health`.
- [ ] 4.3 Live check, read-only from the host (`sqlite3.connect("file:data/openclaw.db?mode=ro", uri=True)` — ADR-0018): the seven Echo claims (#3, #9, #10, #15, #16, #20, #25) read **Blocked: no claim process** on the dashboard, in `/basic`, and on a rendered history card. Record what each actually showed.
- [ ] 4.4 Report to Justin — do not silently leave it — that claims #2 and #8 still read as suspensions after this ships: the classifier fix is code-only, those emails are already in the processed set, the live DB still holds zero `info_requested` events and claim #2 still holds the truncated reference `DC1` (confirmed read-only 2026-07-28). Correcting them is `vet-info-request-chase` groups 0–1, whose re-read path is blocked after regressing four claims live.

## 5. Docs

- [x] 5.1 New ADR: status is stored pipeline state, label is who-holds-it; `matched` labels derive from the action determination rather than a new column; name the three maps this change deleted so a fourth reads as a regression. Add it to `docs/adr/README.md`'s table.
- [x] 5.2 `app/openclaw/CLAUDE.md`: labels live in `status_labels.py` — one map, don't add a second; `_STATUS_COLOURS` is status-keyed for that reason; a `matched` claim's label is derived, so the dashboard word and `vet_claims.status` legitimately differ.
- [x] 5.3 `openspec/BACKLOG.md`: whether `_ACTION_META` titles and these labels should converge (they answer different questions today — "what do I do" vs "where is this claim").
