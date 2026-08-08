# Tasks

Ordered so the four unblocked sections finish without waiting on the two gated ones. Sections 1–4 touch no live data. Sections 5–7 are gated on Justin.

Per the repo rule: record what was *actually verified live*, not what was coded.

## 1. Host-side DB guard (ADR-0018 Alternative 4)

- [x] 1.1 Write `scripts/query_db.py` — read-only helper, `file:…?mode=ro` with `uri=True`, takes SQL and prints rows. Stdlib only, so any python can run it
- [x] 1.2 Add a `PreToolUse` hook rejecting any Bash/PowerShell command that references the live DB path without `mode=ro`, naming the read-only form in the rejection
- [x] 1.3 Verify the guard rejects a real read-write one-liner, and does **not** reject a `mode=ro` one. Both directions — a guard only tested on the failing case is untested
- [x] 1.4 Verify the guard does not fire on `docker exec` writes, which are the sanctioned path for a deliberate write
- [x] 1.5 Make `db.get_connection()` raise when it resolves outside the intended data dir, naming the phantom `C:\data\openclaw.db` and the two supported alternatives
- [x] 1.6 Confirm the phantom's current contents before changing anything, read-only, so the change is not made blind
- [x] 1.7 Both suites pass: `tests/test_core.py` **and** `tests/test_telegram.py`

## 2. `_action_kind` per-kind assertions

- [x] 2.1 Add a before/after assertion for `split_proposal`
- [x] 2.2 Add one for `unmatch` — one of the two the extraction actually moved
- [x] 2.3 Add one for `confirm_resolved` — the other one moved, and its precedence rests on a guard added during the extraction
- [x] 2.4 Add one for `invoice_request_sent`
- [x] 2.5 Confirm all nine kinds are now asserted; the fix is one assertion per kind, not more label tests

## 3. Read-only verifications (no decision needed)

- [ ] 3.1 Claim #8's card should read `Expected payment: $192.72`, not `$296.50` — the whole point of the 65% benefit rate, never checked since it shipped. Read-only against the live DB, full path, `mode=ro`
- [ ] 3.2 Claim #21: we extracted $44.75, Petcover's letter states $35.00 claimed / $22.75 paid. Determine which is wrong. Note the prior: amounts matched 7 for 7 elsewhere, so our extraction is the likelier suspect
- [ ] 3.3 Claim #17: vision-OCR attempted once in six days with two attempts remaining and the source email still found by the live query. Trace `match_claim`'s vision branch live rather than guessing — maxResults truncation is already ruled out
- [ ] 3.4 Record each finding in `openspec/BACKLOG.md` against its entry, including any that turn out to be non-issues

## 4. Draft subjects name their claims

- [x] 4.1 `claim_forms.py:483` and `:643` — include claim ids in the subject, e.g. `Vet claim — Aari (#7, #6)`
- [x] 4.2 Confirm `pipeline.DRAFT_SEARCH_LINK` still resolves with the new subject
- [x] 4.3 Confirm reply correlation is untouched — it matches on Petcover's reply subject, never ours

## 5. GATED — the serial → claim remap (money-affecting)

**Blocked on Justin: go-ahead, and confirmation that the post-mortem's true map is the one to apply.**

- [ ] 5.1 Back up the live DB from inside the container
- [ ] 5.2 Produce the full nine-row dry-run diff — old link, new link, evidence for each. Enumerate every row; no summaries
- [ ] 5.3 Justin reviews the diff
- [ ] 5.4 Apply in-container. Never from the host
- [ ] 5.5 Record the superseded links rather than overwriting silently
- [ ] 5.6 Verify each of the nine post-write, read-only
- [ ] 5.7 Note in `BACKLOG.md` that the five-letter recovery re-read is now unblocked — as separate work with its own go-ahead, not carried by this change

## 6. GATED — redo semantics

**Blocked on Justin: which of the three operations "redo claim #N" means. Recommendation on record: option 1, rebuild the draft — it fits both observed uses, since #7's figures and invoice were correct and only the draft was in doubt.**

- [ ] 6.1 Record the chosen meaning in `design.md` before writing code
- [ ] 6.2 Implement that one operation
- [ ] 6.3 Refuse redo on a claim already `sent`, with an explanation
- [ ] 6.4 When the premise is wrong — the draft exists — say so and name the draft instead of rebuilding
- [ ] 6.5 Confirmation names which operation ran and what changed
- [ ] 6.6 Agent routes "redo claim #N" and close variants to it
- [ ] 6.7 A claim operation with no matching tool no longer falls through to `propose_create_task`; the agent says it cannot do it
- [ ] 6.8 Task capture still works when a task is what was actually asked for
- [ ] 6.9 Close tasks #124 and #125, the open duplicates from the original miss
- [ ] 6.10 Live: exercise redo from Telegram, and record what was verified

## 7. `submission_id` — decided as a consequence of section 6

- [ ] 7.1 Determine whether the chosen redo semantics can re-group a drafted batch
- [ ] 7.2 If it cannot: record "stays derived, no change" with the reasoning. This is a real outcome, not a skipped task
- [ ] 7.3 If it can: hand-run `ALTER TABLE` on the live DB plus backfill — `CREATE TABLE IF NOT EXISTS` will not touch an existing table — and switch `S<a>+<b>` to the stored column

## 8. Close-out

- [ ] 8.1 Sync delta specs into `openspec/specs/` **before** archiving, or the baseline rots
- [ ] 8.2 Move anything still open to `BACKLOG.md` with its reasoning, rather than archiving with open tasks
- [ ] 8.3 Update `README.md` if behaviour changed
- [ ] 8.4 ADR for the redo semantics if the chosen meaning would surprise a newcomer
- [ ] 8.5 `ruff format` then `ruff check` clean
