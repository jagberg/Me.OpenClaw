## 1. Schema

- [x] 1.1 Add `claim_status_events` table (`vet_claims.id` FK, `event_type`, `raw_email_id`, `detail` JSON, `created_at`)
- [x] 1.2 Add `petcover_reference` column to `vet_claims`
- [x] 1.3 `status` is free-text (no CHECK constraint) — new values (`sent`, `acknowledged`, `info_requested`, `suspended`, `settled`, `declined`, `unclassified`) are just new strings the code writes, no schema change needed
- [x] 1.4 Live-DB migration applied (`ALTER TABLE vet_claims ADD COLUMN petcover_reference`, `CREATE TABLE claim_status_events`)

## 2. Reference extraction and classification

- [x] 2.1 `claim_status.py`: `REFERENCE_CONTEXT_PATTERNS` extracts via context phrase ("Claim Number"/"Claim Reference"/"Petcover Claim") rather than a bare ref-shaped regex, since bare `GABR-####` would also match inside the policy number
- [x] 2.2 `SUBJECT_KEYWORDS` classifier (acknowledged/suspended/info_requested/settled/declined) per design.md's mapping
- [x] 2.3 `classify()` falls back to body text when subject doesn't match — same keyword table, reused rather than duplicated
- [x] 2.4 Confirmed via real dry-run (Loki claim ELD-24-2146): settlement amount breakdown exists ONLY in the PDF attachment, body/HTML cuts off before the numbers — PDF extraction is required for `settled` events in v1, not deferrable (see design.md Dry-Run Findings)
- [x] 2.5 `IGNORE_KEYWORDS` bucket ("automatic reply") checked before `SUBJECT_KEYWORDS` — returns `"ignore"`, `process_reply()` returns immediately, never reaches `unclassified`

## 3. Correlation

- [x] 3.1 `find_claim_by_reference()` — exact match against `vet_claims.petcover_reference`
- [x] 3.2 `find_claim_by_pet_and_date()` — pet name (+ real nickname exception: "Ari" for "Aari", confirmed in live data) within a 60-day window of the claim's transaction date
- [x] 3.3 `find_claim_by_pet_and_date()` returns `(None, ambiguous=True)` on >1 match; `process_reply()` records the event with `claim_id=NULL` and a "needs manual link" flag rather than guessing

## 4. Pipeline integration

- [x] 4.1 `pipeline.poll_petcover_status()`: queries each of `PETCOVER_STATUS_SENDERS` individually (`marketing.au@` never queried, not just filtered) — called at the end of `run_once()`
- [x] 4.2 Per message: `_full_message_text` (subject + body + PDF) → `claim_status.process_reply()` (classify → correlate → record event → update `vet_claims`)
- [x] 4.3 Reuses `processed_emails` (same table `gmail_ingest.py` already uses, `task_id` left NULL for these)

## 5. Sent-status trigger

- [x] 5.1 `POST /claims/{id}/sent` → advances a `drafted` claim to `sent`

## 6. Dashboard surfacing

- [x] 6.1 `dashboard()` computes `needs_action`: claim's most recent info_requested/suspended event with no later `confirmed_resolved` event — a later `settled`/`declined` event doesn't clear it, shown alongside instead
- [x] 6.2 `POST /claims/{id}/confirm-resolved` → `claim_status.confirm_resolved()` records the event, which is what actually clears the claim from `needs_action` (via 6.1's logic)
- [x] 6.3 `settled_reconciliation` list: `claimed_amount`/`paid_amount` pulled from each `settled` event's stored detail JSON
- [x] 6.4 `unclassified` list: events with `claim_id IS NULL` (couldn't correlate — includes the "needs manual link" flag from `claim_status.process_reply`)

## 6b. Gap-review fixes (post-implementation review, 2026-07-18)

Reviewed the whole change against the real first-use case — the 3-claim Aari batch (one draft, txn dates Aug-Sep 2025, submitted Jul 2026) — and found 6 real gaps, all fixed:

- [x] 6b.1 Fallback correlation required txn date within 60 days of the email — would have failed our actual batch (txn ~1 year before submission). Replaced date window with a status filter (`CORRELATABLE_STATUSES`: sent and later) — claims awaiting a reply are the candidate pool, regardless of how old their transactions are
- [x] 6b.2 Batch submissions: one Petcover reference covers several `vet_claims` rows (shared `draft_id`). `find_claims_by_reference` now returns all of them; pet-fallback treats matches sharing one draft_id as ONE submission (not ambiguous); events + learned reference apply to every claim in the group
- [x] 6b.3 First-run backfill: unbounded Gmail query would ingest years of historical replies and could mis-correlate them onto open claims. Added `PETCOVER_STATUS_SINCE` config (`after:` clause, defaults to ship date)
- [x] 6b.4 Status regression: Gmail lists newest-first, so ack+settlement arriving in one poll would process settlement first then regress status to `acknowledged`. Poll now collects unprocessed messages and sorts oldest-first by `internalDate`
- [x] 6b.5 "Mark sent" now advances every `drafted` claim sharing the draft_id — one click per submission, not per claim (missing a sibling would leave it invisible to correlation)
- [x] 6b.6 Spec promised manual linking for unclassified replies but no route/UI existed. Added `claim_status.link_event()`, `POST /events/{id}/link`, and a link form on the unclassified list
- [x] 6b.7 Tests for all of the above: batch correlate+learn-reference (incl. year-old txn dates), ambiguity refusal → manual link flow, nickname matching ("Ari"→Aari). 24 tests, all passing

## 7. Verification

- [x] 7.1 `test_reference_regex_*` — real (redacted) samples, both ref formats, plus a negative test confirming a bare policy number doesn't false-match
- [x] 7.2 `test_classify_*` — one real (redacted) sample per event type incl. `ignore` and the body-fallback path, all pass
- [x] 7.3 Live test: fed the real Loki `ELD-24-2146` ack + settlement text (fetched earlier this session) through `claim_status.process_reply()` against a temporary test fixture (Loki isn't a real tracked pet — cleaned up after). **Found and fixed a real bug**: fallback correlation only triggered when NO reference text was present, but the actual first-event case is a reference *present in the email* that's simply not yet *stored* on any claim — exactly the scenario design.md's dry-run flagged as necessary. Fixed by falling back whenever reference-based lookup finds no claim, not only when no reference string exists. After the fix: ack event correctly learned `ELD-24-2146` via pet+date fallback, settlement event correctly correlated via exact reference match with `claimed_amount=624.89`/`paid_amount=324.97` matching the real PDF numbers exactly. Test fixture fully deleted afterward (0 rows remaining, verified).
