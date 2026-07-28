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
- [x] 3.4 Clear `email_extractions` as one deliberate step, stating the count re-extracted (14 rows as of 2026-07-28) — it spends tokens against the daily budget (ADR-0017), and a failed extraction isn't cached so a partial run resumes. Ask before running it live.
- [x] 3.5 Test with the real data: `18/05/2026` resolves to Kings Vet invoice `1000229`, $351.50, claim **#6** — while the request itself stays on claim **#8**. Assert the two are reported as different claims, since that is the whole point.
- [x] 3.6 Test: a date only in the extraction cache resolves with no claim id; an unmatched date reports "visit unknown"; two invoices sharing a date report both; a line-item date matches even when the invoice's header date differs.
- [ ] 3.7 Test the wording never presents the resolved invoice as the requested document.

## 4. The Monday nudge

- [x] 4.1 Add `claim_status.unanswered_vet_requests()`: claims whose latest unresolved `info_requested` event has `owed_by = "vet"` (reusing the existing unresolved determination — ADR-0008's confirm-resolved rule), each with clinic name/email, requested document, days outstanding, and days remaining to the treatment-anchored deadline. Exclude past-deadline claims.
- [x] 4.2 Add `INFO_REQUEST_DEADLINE_DAYS = 365` and `VET_NUDGE_DAY = "mon"` to `config.py`.
- [x] 4.3 Add `pipeline.nudge_unanswered_vet_requests()`: one message listing each entry (claim id, pet, clinic + email, document, age, days left); returns without sending when the list is empty.
- [x] 4.4 Register the cron job — `day_of_week=config.VET_NUDGE_DAY`, `hour=config.ACTION_NUDGE_HOUR`, `id="vet-request-nudge"`, `coalesce=True`, `misfire_grace_time=3600` — beside the existing daily nudge, without changing it.
- [x] 4.5 Test: two vet-owed unresolved requests produce one message naming both clinics and both documents; a Justin-owed request is absent; a confirmed-resolved claim is absent; a past-deadline claim is absent; an empty list sends nothing.
- [x] 4.6 Test: a vet-owed request with no recorded document still appears, saying the document is unstated.
- [x] 4.7 Run both suites: `cd app && ./.venv/Scripts/python.exe tests/test_core.py` and `tests/test_telegram.py`.

## 5. Coordinate, deploy, verify

- [ ] 5.1 In `vet-info-request-chase/tasks.md`: mark 6.3 absorbed here, and 5.6 superseded by the Monday job (its daily-nudge folding is no longer the plan).
- [x] 5.2 Deploy from the `deploy` worktree with `./scripts/deploy.ps1` and confirm `/health`.
- [x] 5.3 Verify live, read-only from the host (ADR-0018): claim #8's chip reads `Vet: consult notes needed`, and `nudge_unanswered_vet_requests` invoked by hand produces exactly one line — #8, Aari, Kings Vet `info@kingsvet.com.au`, `Consultation notes dated 18/05/2026`, with days outstanding and days left. Record what it actually printed.
- [x] 5.4 Confirm the Monday job is registered on the live scheduler (job id `vet-request-nudge`, next run a Monday) rather than assuming the cron string is right.
- [x] 5.5 Optional, ask first: backfill `requested_document` on events 10 and 31 from their own stored emails. Container-side, backup first, dry-run diff reviewed — never from the host, never unattended.

## 6. Docs

- [x] 6.1 Amend ADR-0021: the label names the requested document, not just who holds the claim (its rule implies this but does not say it), and why the chip carries a short name while the full phrase lives where there is room.
- [x] 6.2 Amend ADR-0020's addressee section: the same letter also yields *what* was asked for, by regex, and the weekly beat is how an unobservable vet reply gets chased.
- [x] 6.3 `README.md`: the lifecycle branch now names the document, and a Monday nudge covers unanswered vet requests.
- [x] 6.4 `app/openclaw/CLAUDE.md`: `requested_document` joins `owed_by` in the event detail; the label's short-name map; two nudge jobs exist with different cadences and scopes — don't merge them.
- [x] 6.5 Record in `vet-info-request-chase` that both design Open Questions were answered yes on 2026-07-28 (done at proposal time, not deferred to BACKLOG): an exact `(reference, Sr)` hit may attach to a settled claim as history, and the assignment card gets a "No claim on file" button. Nothing about them goes to BACKLOG — whether an exact `(reference, Sr)` hit may attach to a settled claim as history, and whether the assignment card needs a "none of these" button for letters about claims that predate the bank CSV coverage.

### Verification record (groups 1-2, 2026-07-28)

**Verified against the real mailbox** (read-only): both live copies of the 2026-07-27 letter — Gmail `19fa2a00a6beb635` (to Justin) and `19fa2a5603b99676` (to Kings Vet) — yield `requested_document = "Consultation notes dated 18/05/2026"` and `requested_document_date = "2026-05-18"`.

**A defect in this change's own first cut, found by that check and fixed:** an ask with nothing after it (`we need a copy of

Please note we cannot process`) returned `"Please note"` as the requested document, because the ask pattern's trailing `\s*` consumed the blank line so the boilerplate lookahead could never fire at position 0. Now also checked per line (`_BOILERPLATE_LINE`). It would have shown Petcover's own boilerplate to Justin as the document they wanted.

**Unit-tested only, not yet seen live**: the `justin`-owed label wording (no live letter has been addressed to him since the vocabulary shipped), the two-item capture, and `short_document`'s itemised-invoice / claim-form / referral-history kinds — only consultation notes has appeared in real mail.

**Group 3 verified 2026-07-28** against a read-only `.backup` copy of the live DB (see the finding below for why not directly): `find_visit_by_date("2026-05-18")` returns claim **#6**, Kings Vet, invoice **1000229**, $351.50 — the visit the letter names, on a different claim from the one the letter sits on (#8, 2 April). `2026-07-06` correctly falls through to the extraction cache with no claim id; `2026-05-19` and `None` return `[]` with no nearest-date guess.

**Trap found while verifying, now in root `CLAUDE.md` and BACKLOG:** a host-side `db.get_connection()` resolves `DATABASE_PATH=/data/openclaw.db` (the container path, from `app/.env`) to `C:\data\openclaw.db` — a real file holding 2 stale claims. The first run of this resolver returned `[]` for a date the live DB certainly holds. Quiet wrong answers, not a loud failure.

**Not built yet**: task 3.4 (clear the 14 cached extractions so line-item dates are actually populated — approved token spend, not yet run), group 4 (the Monday nudge), 5 (deploy + live verify), 6 (docs). Line-item date matching is implemented and unit-tested but **no stored invoice carries an item date yet**, so it cannot fire on real data until 3.4 runs.

### Live verification, groups 4-5 (2026-07-28, `4e99b4d+deploy`)

The Monday nudge, run by hand inside the container against real data:

```
1 vet info request(s) unanswered:
 * #8 Aari - Kings Vet KINGSGROVE NSW (info@kingsvet.com.au)
   needs: Consultation notes dated 18/05/2026
   visit: 2026-05-18 - invoice 1000229 (claim #6)
   asked 1d ago, 248d until the 1-year claim deadline
```

Dashboard chip for #8 now reads **`Vet: consult notes needed`**. Cron job `nudge_unanswered_vet_requests` confirmed registered from the app's own startup log, not from an assumption about the cron string.

**Task 5.5 (document backfill) was run**, container-side, backup `/data/openclaw.db.bak-pre-document-backfill-20260728`, dry-run reviewed first: events 27 and 28 gained `Consultation notes dated 18/05/2026` + `2026-05-18`; event 31 gained `Consult notes dated` (its body prints no date); event 10 was left null, because its body ends mid-sentence and the detail is in an attachment we get no text for.

**A regression this change introduced was caught by that dry run**, before it wrote to the live DB: the refactored ask pattern had dropped the original's consumption of the letter's filler, so two vet cover notes yielded `information in order for us to review the` and `for us to review the claim Consult notes dated` as the document to chase. Fixed in `719009f`, with the two real phrasings quoted at the pattern and a minimum-length guard for a stray `the`. The lesson is the project's existing one: the dry run against real mail is what found it, not the unit tests, which were passing throughout.

**Still not done:** task 3.4 (clear the 14 cached extractions so line-item dates populate — approved, deliberately not run at the tail of a long session because the next tick re-extracts and could alter matching for the 3 `pending_match` claims) and group 6 (docs: amend ADR-0020/0021, README, module CLAUDE.md, BACKLOG). Line-item date matching remains implemented and unit-tested but **cannot fire on real data** until 3.4 runs.

### Task 3.4: blocked, then done (2026-07-28)

Attempted, and it failed all 14 on Groq `403 {"error":{"message":"Access denied. Please check your network settings."}}`. **No data changed and no tokens were consumed.** The script re-extracts in place and replaces a row only when the fresh result is non-empty — deliberately, so a vision-sourced row can never be overwritten with nothing — and it took a backup first (`/data/openclaw.db.bak-pre-item-dates-20260728`).

Diagnosed from inside the container: the same 403 comes back **with and without** the API key, so this is IP/network-level denial by Groq, not the key, not a quota, and not the request shape. Last successful call 05:34 UTC; first 403 at 12:39 UTC. Why the egress is blocked is a network/account question for Justin — no code change fixes it.

It also exposed a real gap, now in `openspec/BACKLOG.md`: ADR-0017's fallback chain walks four models that are **all Groq**, so a provider-level rejection defeats every link and takes invoice extraction and the chat agent down together. Gemini credentials already exist for vision OCR and would serve as a cross-provider fallback.

Until 3.4 runs, line-item date matching is implemented and unit-tested but **cannot fire on real data** — `find_visit_by_date` matches invoice header dates only, which is exactly the case Justin raised (a consult on the 18th billed on an invoice dated the 30th). Re-run `reextract_item_dates.py --apply` in the container once the provider is reachable.

**3.4 completed later the same day.** The 403 was Justin's VPN — Groq denies VPN egress. With it off, `api.groq.com` returned 200 and the re-extraction ran: **11 rows replaced, 3 kept, 0 failed.**

The keep-if-empty guard earned itself: `19f7c8412410fadd` (the Kings Vet bulk history, 11 invoices) returns nothing on the text path, so a delete-and-refill would have destroyed the richest row in the cache. Two rows changed invoice COUNT on re-extraction (9→10, 9→8) — the model does not partition a multi-invoice email identically every time. No claim's status or flag moved (snapshot before and after identical), because matched claims are never re-matched; the risk lives with the 3 `pending_match` claims on future ticks.

**A second self-inflicted failure, worth recording because of how it presented.** The first attempt after the VPN came off failed 13 of 14 with `LLMUnavailableError: database is locked`, which reads exactly like a provider outage. It was not: the script held one write transaction open across every LLM call, so `gemini.py`'s own `llm_calls` logging insert could not acquire the write lock. Restructured into read → call → write, with that reasoning at the top of the script. Any container-side script that calls the LLM while holding a write transaction will hit this.

**What the data actually says about line-item dates — the honest answer.** Only **3** line items across all 14 emails carry their own date, and **all three equal their invoice's header date**:

| email | invoice date | item date | item |
|---|---|---|---|
| 19fa2a26 | 2026-06-19 | 2026-06-19 | Prescription fee |
| 19fa2a24 | 2026-06-30 | 2026-06-30 | CLINDAMYCIN 150MG CAPSULES |
| 19fa2a24 | 2026-06-30 | 2026-06-30 | ENROFLOXACIN 150MG TABLETS |

So the premise behind this task — an invoice's header date differing from the date of a treatment on it — is sound in principle and **has no instance in the mail on file**. The capability is now in place and populated where documents state it; it has not yet changed a single resolution. Recorded rather than presented as a win.
