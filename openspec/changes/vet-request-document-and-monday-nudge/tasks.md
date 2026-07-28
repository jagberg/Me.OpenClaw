Scope note: this absorbs `vet-info-request-chase` task 6.3 (extract the requested document) and supersedes its task 5.6 (fold information requests into the daily nudge) — task 5.1 marks both there. Its `chase_vet` action kind, register and backfill are untouched and remain its own.

## 1. Extract the requested document

- [x] 1.1 Add `claim_status.extract_requested_document(text)`: capture what follows `we need a copy of` / `we require a copy of` / `please provide the following`, stopping at the letter's trailing boilerplate (`Please note`, `You can reach us`, `In line with`), whitespace-collapsed. Return `None` when nothing matches — regex only, no LLM.
- [x] 1.2 Record it on the `info_requested` event's `detail` in `process_reply`, next to `owed_by`; omit the key when `None`. No DDL.

  **Note (2026-07-28): this was already half-built.** `vet-info-request-chase` group 4 had shipped an inline `requested_document` regex in `process_reply` — so this change refined rather than introduced it. What was wrong with the inline version: it took `splitlines()[0]` (right for the one live letter, drops the second item when a letter asks for two) and had no boilerplate terminator. The scope note's claim that task 6.3 was "absorbed" was therefore only partly true; the extraction existed, the date parsing and the multi-item capture did not.
- [x] 1.3 Test with the real letter text (`we need a copy of\nConsultation notes dated 18/05/2026\nPlease note we cannot process…`): the document is exactly `Consultation notes dated 18/05/2026`, boilerplate excluded.
- [x] 1.4 Test a letter with no such phrase records no document, and that the claim's status/flag handling is unchanged.

## 2. The label names it

- [x] 2.1 Add `_short_document()` to `status_labels`: consultation notes → "consult notes", itemized invoice → "itemised invoice", completed claim form → "claim form", referral history → "referral history"; unrecognized → `None`.
- [x] 2.2 Extend `label()`: vet + document → `Vet: consult notes needed`; Justin + document → `Consult notes needed from you`; no document (or unrecognized kind) → today's wording unchanged; `owed_by` unrecorded → `Info requested` regardless of document.
- [x] 2.3 Carry `requested_document` alongside `owed_by` into the ledger rows (`visit_ledger` already reads every event, so no extra query) so the chip and `/basic` can use it.
- [x] 2.4 Show the **full** phrase where there is room: the claim detail and the action card.
- [x] 2.5 Test every combination of document × `owed_by`, including an unrecognized document kind falling back.

## 3. Resolve the requested date to a visit

- [x] 3.1 Parse the date out of the requested document (`dated 18/05/2026`, `dated 18 May 2026`) into an ISO date; record it on the event beside the phrase. No date stated → null, no guess.
- [x] 3.2 Add `invoice_matching.find_visit_by_date(iso_date)`: match claims' `invoice_data` first (invoice date **or** any line-item date), then `email_extractions`. Return every match (claim id where one exists, merchant, invoice number, amount) — never a nearest-date fallback.
- [x] 3.3 Add an optional per-item `date` to the extraction schema (null when the document doesn't state one) and to `find_visit_by_date`'s matching.
- [ ] 3.4 Clear `email_extractions` as one deliberate step, stating the count re-extracted (14 rows as of 2026-07-28) — it spends tokens against the daily budget (ADR-0017), and a failed extraction isn't cached so a partial run resumes. Ask before running it live.
- [x] 3.5 Test with the real data: `18/05/2026` resolves to Kings Vet invoice `1000229`, $351.50, claim **#6** — while the request itself stays on claim **#8**. Assert the two are reported as different claims, since that is the whole point.
- [x] 3.6 Test: a date only in the extraction cache resolves with no claim id; an unmatched date reports "visit unknown"; two invoices sharing a date report both; a line-item date matches even when the invoice's header date differs.
- [ ] 3.7 Test the wording never presents the resolved invoice as the requested document.

## 4. The Monday nudge

- [ ] 4.1 Add `claim_status.unanswered_vet_requests()`: claims whose latest unresolved `info_requested` event has `owed_by = "vet"` (reusing the existing unresolved determination — ADR-0008's confirm-resolved rule), each with clinic name/email, requested document, days outstanding, and days remaining to the treatment-anchored deadline. Exclude past-deadline claims.
- [ ] 4.2 Add `INFO_REQUEST_DEADLINE_DAYS = 365` and `VET_NUDGE_DAY = "mon"` to `config.py`.
- [ ] 4.3 Add `pipeline.nudge_unanswered_vet_requests()`: one message listing each entry (claim id, pet, clinic + email, document, age, days left); returns without sending when the list is empty.
- [ ] 4.4 Register the cron job — `day_of_week=config.VET_NUDGE_DAY`, `hour=config.ACTION_NUDGE_HOUR`, `id="vet-request-nudge"`, `coalesce=True`, `misfire_grace_time=3600` — beside the existing daily nudge, without changing it.
- [ ] 4.5 Test: two vet-owed unresolved requests produce one message naming both clinics and both documents; a Justin-owed request is absent; a confirmed-resolved claim is absent; a past-deadline claim is absent; an empty list sends nothing.
- [ ] 4.6 Test: a vet-owed request with no recorded document still appears, saying the document is unstated.
- [ ] 4.7 Run both suites: `cd app && ./.venv/Scripts/python.exe tests/test_core.py` and `tests/test_telegram.py`.

## 5. Coordinate, deploy, verify

- [ ] 5.1 In `vet-info-request-chase/tasks.md`: mark 6.3 absorbed here, and 5.6 superseded by the Monday job (its daily-nudge folding is no longer the plan).
- [ ] 5.2 Deploy from the `deploy` worktree with `./scripts/deploy.ps1` and confirm `/health`.
- [ ] 5.3 Verify live, read-only from the host (ADR-0018): claim #8's chip reads `Vet: consult notes needed`, and `nudge_unanswered_vet_requests` invoked by hand produces exactly one line — #8, Aari, Kings Vet `info@kingsvet.com.au`, `Consultation notes dated 18/05/2026`, with days outstanding and days left. Record what it actually printed.
- [ ] 5.4 Confirm the Monday job is registered on the live scheduler (job id `vet-request-nudge`, next run a Monday) rather than assuming the cron string is right.
- [ ] 5.5 Optional, ask first: backfill `requested_document` on events 10 and 31 from their own stored emails. Container-side, backup first, dry-run diff reviewed — never from the host, never unattended.

## 6. Docs

- [ ] 6.1 Amend ADR-0021: the label names the requested document, not just who holds the claim (its rule implies this but does not say it), and why the chip carries a short name while the full phrase lives where there is room.
- [ ] 6.2 Amend ADR-0020's addressee section: the same letter also yields *what* was asked for, by regex, and the weekly beat is how an unobservable vet reply gets chased.
- [ ] 6.3 `README.md`: the lifecycle branch now names the document, and a Monday nudge covers unanswered vet requests.
- [ ] 6.4 `app/openclaw/CLAUDE.md`: `requested_document` joins `owed_by` in the event detail; the label's short-name map; two nudge jobs exist with different cadences and scopes — don't merge them.
- [ ] 6.5 Record in `vet-info-request-chase` that both design Open Questions were answered yes on 2026-07-28 (done at proposal time, not deferred to BACKLOG): an exact `(reference, Sr)` hit may attach to a settled claim as history, and the assignment card gets a "No claim on file" button. Nothing about them goes to BACKLOG — whether an exact `(reference, Sr)` hit may attach to a settled claim as history, and whether the assignment card needs a "none of these" button for letters about claims that predate the bank CSV coverage.

### Verification record (groups 1-2, 2026-07-28)

**Verified against the real mailbox** (read-only): both live copies of the 2026-07-27 letter — Gmail `19fa2a00a6beb635` (to Justin) and `19fa2a5603b99676` (to Kings Vet) — yield `requested_document = "Consultation notes dated 18/05/2026"` and `requested_document_date = "2026-05-18"`.

**A defect in this change's own first cut, found by that check and fixed:** an ask with nothing after it (`we need a copy of

Please note we cannot process`) returned `"Please note"` as the requested document, because the ask pattern's trailing `\s*` consumed the blank line so the boilerplate lookahead could never fire at position 0. Now also checked per line (`_BOILERPLATE_LINE`). It would have shown Petcover's own boilerplate to Justin as the document they wanted.

**Unit-tested only, not yet seen live**: the `justin`-owed label wording (no live letter has been addressed to him since the vocabulary shipped), the two-item capture, and `short_document`'s itemised-invoice / claim-form / referral-history kinds — only consultation notes has appeared in real mail.

**Group 3 verified 2026-07-28** against a read-only `.backup` copy of the live DB (see the finding below for why not directly): `find_visit_by_date("2026-05-18")` returns claim **#6**, Kings Vet, invoice **1000229**, $351.50 — the visit the letter names, on a different claim from the one the letter sits on (#8, 2 April). `2026-07-06` correctly falls through to the extraction cache with no claim id; `2026-05-19` and `None` return `[]` with no nearest-date guess.

**Trap found while verifying, now in root `CLAUDE.md` and BACKLOG:** a host-side `db.get_connection()` resolves `DATABASE_PATH=/data/openclaw.db` (the container path, from `app/.env`) to `C:\data\openclaw.db` — a real file holding 2 stale claims. The first run of this resolver returned `[]` for a date the live DB certainly holds. Quiet wrong answers, not a loud failure.

**Not built yet**: task 3.4 (clear the 14 cached extractions so line-item dates are actually populated — approved token spend, not yet run), group 4 (the Monday nudge), 5 (deploy + live verify), 6 (docs). Line-item date matching is implemented and unit-tested but **no stored invoice carries an item date yet**, so it cannot fire on real data until 3.4 runs.
