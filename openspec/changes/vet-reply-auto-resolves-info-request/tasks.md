## 1. Correlation

- [x] 1.1 Helper: given a clinic email address, return the claims it currently owes an open vet-directed request for (reuse `unanswered_vet_requests()`'s eligibility, don't re-derive it) — `claim_status.claims_owed_by_clinic`
- [x] 1.2 Match a reply's subject/thread against those claims' `(petcover_reference, petcover_sr)` — exactly one match proceeds to content interpretation, zero/multiple leave every candidate untouched — `claim_status._correlate_vet_reply` (reuses `extract_reference`/`extract_sr`)

## 2. Content interpretation

- [x] 2.1 `llm.extract()` call, scoped to classifying a matched reply into exactly one of: provided / sent_to_petcover / unavailable / unclear — no other fields, mirrors `_extract_clarification_figures`'s tight scoping — `claim_status._classify_vet_reply`
- [x] 2.2 `provided` → call the existing `claim_status.confirm_resolved`, recording that it fired from an auto-matched reply (detail field), not a dashboard/Telegram tap — `confirm_resolved` gained optional `detail`/`email_id` params, defaulted off for the manual-tap callers
- [x] 2.3 `sent_to_petcover` → new `info_requested` event with `owed_by: "petcover"`, carrying the clinic/subject fields forward
- [x] 2.4 `unavailable` → no event; append the vet's stated reason as a visible note (mirrors settlement-clarification-email's "still unresolved" note pattern) — via `_flag_claim`
- [x] 2.5 `unclear` → no action at all

## 3. `owed_by: "petcover"` support

- [x] 3.1 Label for this value in whatever map `info_requested`'s existing `owed_by` labels live in (CLAUDE.md gotcha: "never default it" — this is a fourth explicit branch, not a fallback) — `status_labels._INFO_REQUEST_LABELS`/`_INFO_REQUEST_NEEDS`, plus `claim_status._ACTION_META["confirm_resolved"]` and `_waiting_key` (found live: these also read `owed_by` and would have silently defaulted "petcover" to "justin"'s wording — not named in the task but the same rule)
- [x] 3.2 `unanswered_vet_requests()` excludes a claim whose latest info_requested event has `owed_by: "petcover"` (reads as "no longer vet-owed", same as it already excludes `owed_by: "justin"`) — already true unmodified: the existing check is a positive `== "vet"` test, not a negative one, so a third value falls out by construction; added a comment only
- [x] 3.3 Dashboard/Telegram card rendering distinguishes this from both existing `owed_by` labels — via status_labels, which both surfaces read; no hardcoded owed_by strings found in templates or claim_card.py

## 4. Poller

- [x] 4.1 `pipeline.poll_vet_replies`: query the Gmail account for messages from any clinic address currently owing an open request, since that request was raised
- [x] 4.2 Wire into `pipeline.run_once` ahead of `poll_petcover_status` — same tick position `poll_petcover_status` itself has relative to `gmail_ingest.poll_once`'s separate schedule; see 5.1/5.2 for what actually makes this safe against that schedule racing it
- [x] 4.3 Idempotent on re-poll: a claim no longer in the vet-owed set (resolved, or now `owed_by: petcover`) stops being pulled into this poller's scope next tick — the per-tick clinic-email search list is rebuilt from `unanswered_vet_requests()` fresh every call

## 5. gmail_ingest exclusion

- [x] 5.1 Exclude a sender from task capture only while it currently owes an open vet-directed request (computed fresh, not cached) — narrower than excluding all of `vet_contacts` — `gmail_ingest._currently_owed_clinic_emails`
- [x] 5.2 Do not permanently mark an excluded message processed in a way that blocks task 4.1's poller from reading it on this or a later tick — skip via `continue` before `_mark_processed`, identical to the Petcover carve-out

## 6. Tests

- [x] 6.1 Single open request, reply says provided → resolved via the same path as `confirm_resolved`
- [x] 6.2 Single open request, reply says sent to Petcover directly → new `info_requested` event, `owed_by: "petcover"`, claim not resolved
- [x] 6.3 Single open request, reply says can't find it → stays vet-owed, note recorded, no event
- [x] 6.4 Single open request, reply doesn't answer it → claim completely untouched
- [x] 6.5 One clinic, two open requests, reply names one → only that one is interpreted/acted on; the other untouched
- [x] 6.6 One clinic, two open requests, ambiguous reply → neither claim's content is interpreted, neither changes
- [x] 6.7 `unanswered_vet_requests()` excludes an `owed_by: "petcover"` claim
- [x] 6.8 A clinic's unrelated email (no open request) is unaffected — still eligible for normal task capture
- [x] 6.9 A clinic's email while a request IS open is not captured as a task and is not marked processed in a way that blocks the poller
- [x] 6.10 Re-running the poller after a resolution/owed_by change does not re-process or re-act

## 7. Docs

- [x] 7.1 Sync the delta into `openspec/specs/claim-status-tracking` — appended both ADDED requirements, plus a scenario on the existing "Unanswered vet-directed requests are identifiable" requirement for the `owed_by: petcover` exclusion
- [x] 7.2 Note in `app/openclaw/CLAUDE.md`'s "gotchas" section: a second capability now shares the `processed_emails` dedupe-gate discipline, and `owed_by` gains a third value — both gotchas updated, plus the module-map row for `claim_status.py`
