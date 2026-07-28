## 0. Make the re-read safe (blocks group 1 — added 2026-07-27 after a live failure)

- [ ] 0.1 Suppress the `status` and `flag` writes entirely when `process_reply` is running a re-read: append events and learn reference/Sr only (design Decision 9). Event-level idempotency alone was tried and disproved — it regressed claims #6, #7, #18, #22
- [ ] 0.2 Test: replay real Petcover mail over a populated DB and assert **no** `vet_claims.status` and **no** `flag` value changes, while the corrected `info_requested` events are still appended. This is the test that should have existed before the live trial
- [ ] 0.3 Test: a re-read of an acknowledgement whose routing has changed appends the event to the newly-correct claim without moving that claim's status
**Decided 2026-07-28 (Justin, both yes):**

- **An exact `(reference, Sr)` hit MAY attach to a settled claim as history** — recording the event against it, taking no action and never moving its state. `find_claim_by_reference_and_sr` refusing terminal claims is what left event 10 unlinked when claim #21 held exactly its reference and serial. This does **not** loosen ADR-0011: recording a true fact about a closed claim is not reopening it, and the distinction is now the one `claim-state-from-event-log` makes explicit (state events vs stateless events).
- **The assignment card gets a "No claim on file" button** — marks the event permanently unlinked with a reason, and it is never re-offered. Needed because `bank_transactions` starts 2025-07-17, so letters naming `GABR-0305`, `GABR-0306`, `DC1-26-4751`, `DC1-27-5631` and `DC1-26-5993` (live: event 30) can have no correct claim, and a card with three wrong buttons and no way to say so is a card that gets ignored. "Not now" was rejected — a pre-coverage visit will never gain a claim. Semantics: record that no claim applies, matching the register's `claim_id = NULL` model. Never delete, never hide.

- [ ] 0.4 Give the un-held-serial case an explicit assignment path — Justin picks the claim for a `(reference, Sr)` no claim holds — rather than letting `correlate_ack`'s recency rule guess. Two same-thread requests to different clinics collided live and one landed on an unrelated claim (design "Known limitation")

## 1. Repair the live data the extraction bug already corrupted

- [x] 1.1 Confirm against `app/data/openclaw.db` (read-only) that claim #2 still holds `petcover_reference = 'DC1'` and that event 28 (`Petcover claim for Ari - DC1-26-5992 sr.1`) is still attached to claim #2 rather than claim #8
- [x] 1.2 Clear claim #2's `petcover_reference` and re-point event 28's `claim_id` to claim #8. **Run it inside the container (`docker exec`), not from the host** — ADR-0018 forbids host-side read-write access to the live DB, and this session broke that rule four times without noticing (see BACKLOG). Prefer `claim_status.detach_reference(2)` over raw SQL for the reference half, since it logs the undo; the event re-point has no code path (`link_event` only attaches *unlinked* events) and needs one statement
- [x] 1.3 Re-check claim #2's status: it was set `suspended` by the misrouted letter, so decide from the real letter whether it belongs there or reverts, and record which

### Audit of the existing event log (run read-only 2026-07-28, before any repair)

Every one of the 20 stored `claim_status_events` with a source email was re-fetched from Gmail and re-run through the **fixed** `classify` / `extract_reference` / `extract_sr` / `resolve_owed_by`. **Six disagree with what is stored** — so the repair is wider than claim #2 alone, and the whole set is listed here rather than rediscovered later. Nothing was written.

| Event | Claim | Stored | Re-classified | Reference | Owed by |
|---|---|---|---|---|---|
| 27 | 8 | `suspended` | `info_requested` | `DC1-26-5992` Sr 1 | **Justin** (policyholder copy) |
| 28 | **2** | `suspended` | `info_requested` | stored `DC1` → `DC1-26-5992` Sr 1 | vet, `info@kingsvet.com.au` |
| 10 | — | `unclassified` | `info_requested` | `DC1-27-5628` Sr 2 | vet, `info@kingsvet.com.au` |
| 31 | — | `unclassified` | `info_requested` | `DC1-27-5628` Sr 8 | vet, `admin@theshirevet…` |
| 11 | 19 | `unclassified` | `below_excess` | `DC1-27-5628` Sr 3 | — |
| 12 | 22 | `unclassified` | `below_excess` | `DC1-26-5978` Sr 1 | — |

Both copies of the DC1-26-5992 letter are present (27 to Justin, 28 to the vet), and they landed on **different claims**; the vet copy is the operative one and the document is owed by Kings Vet.

Three further discrepancies are routing, not classification, and are **not** decided here — they are exactly the un-held-serial collision task 0.4 exists for:

- Event 7 → claim 18 (`Sr 4`), but its own text yields `Sr 2`.
- Event 8 → claim 19 (`Sr 3`), text yields `Sr 2`.
- Event 29 → claim 19 (`Sr 3`), text yields `Sr 8`. If 29 is misrouted, claim 19's latest real event is the `below_excess` of event 11 rather than an acknowledgement — which changes its status.
- Event 30 (`acknowledged`, `DC1-26-5993` Sr 1) is attached to no claim at all.

- [x] 1.4 Repair the six mis-typed events **by rewriting `event_type` in place, not by re-reading mail** — the re-read path is group 0 and is unsafe (it regressed four claims live). Rewriting a stored event's type is the append-only log's one legitimate correction: the event happened, we recorded what it was wrongly. Record the before/after for each of the six.
- [x] 1.5 Recompute the affected claims' status from their own corrected event log, one claim at a time with the reasoning recorded: #2 (only event was the misrouted 28 → reverts to its pre-letter status), #8 (`acknowledged` then `info_requested` → `info_requested`, vet owes consult notes dated 18/05/2026), #22 (only event 12 → `below_excess`), #19 (**decide 0.4 first** — its status depends on whether event 29 belongs to it).
**Repair executed 2026-07-28** (`docker exec`, backup `/data/openclaw.db.bak-pre-audit-repair-20260728`, dry-run diff reviewed first, preconditions asserted against the audited state before any write):

- events 10, 31 → `info_requested`; 11, 12 → `below_excess`; 27 → `info_requested`; 28 → `info_requested` **and re-pointed from claim #2 to claim #8**. Each corrected event's `detail` now carries `corrected_from`, `corrected_at`, `corrected_by`, and `owed_by` where the audit resolved it.
- claim #2: `suspended` → `sent`, junk reference `DC1` cleared, logged as a `reference_detached` event (event 34) the way `detach_reference` would — the running image predates that helper.
- claim #8: `suspended` → `info_requested`. Kings Vet owes consult notes dated 18/05/2026.
- claim #22: `sent` → `below_excess`.
- Verified after: **zero** `suspended` events and zero `suspended` claims remain; 4 `info_requested`, 3 `below_excess`. Claim #19 untouched.

- [ ] 1.6 Link events 10, 31 and 30 to their claims, or leave them unlinked deliberately and say so. Two are live vet-owed information requests that have produced no action since 19 and 27 July; 30's thread `DC1-26-5993` is held by no claim on file.
- [x] 1.7 Back up `app/data/openclaw.db` before the repair and snapshot every claim's `(status, flag, petcover_reference, petcover_sr)` before and after; diff them and confirm nothing moved that was not in 1.4–1.6. The last live write on this change regressed four claims silently.
- [x] 1.8 Run the repair **inside the container** (`docker exec`), never from the host — ADR-0018.

## 2. Extraction fixes (standalone value — deployable on their own)

- [x] 2.1 Add `_normalize(text)` to `claim_status.py` mapping U+2010–U+2015 and U+2212 to ASCII `-`; apply at the entry of `classify`, `extract_reference`, `extract_sr`, `extract_settlement_amounts`, `extract_approval_amounts`
- [x] 2.2 Test: the real policyholder-letter text (`Claim Reference: DC1‐26‐5992 Sr 1`, U+2010 hyphens) yields reference `DC1-26-5992` and Sr 1 — this test fails before 2.1 and is the regression guard for the misroute
- [x] 2.3 Extend `extract_sr`: accept `SR[\s.:]*\d+` adjacent to the reference (covers `Sr.8`, `sr.1`) and add the `Serial Number: N` labeled form alongside `Treatment number: N`
- [x] 2.4 Test each of the four live Sr formats: `DC1-27-5628 Sr 3`, `DC1-27-5628 Sr.8`, `DC1-26-5992 sr.1`, `Serial Number: 2`
- [x] 2.5 Add the shape fallback to `extract_reference` (`[A-Z]{2,4}-\d{2}-\d{4}`, `GABR-\d{4}`, case-insensitive), running only after the context phrases fail, rejecting candidates inside a longer hyphen-group chain
- [x] 2.6 Test: subject `Petcover claim for Ari DC1-27-5628 Sr.8` yields `DC1-27-5628`; and text containing only the policy number `GABR-0306-DC1-00000001R` yields **no** reference
- [x] 2.7 Run the full suite (`./.venv/Scripts/python.exe tests/test_core.py` from `app/`) and confirm no existing reference/settlement test regressed on the normalization

## 3. Classification fixes

- [x] 3.1 Move `info_requested` ahead of `suspended` in `SUBJECT_KEYWORDS` and add the live phrases: `further information required`, `information required`, `please provide the following`, `request for consult note`, `request for cf`
- [x] 3.2 Give `classify` the sender, and classify any email from `requiredinfo.au@petcovergroup.com` as `info_requested` on the sender alone
- [x] 3.3 Thread the sender through `process_reply` and `pipeline.poll_petcover_status` (the polled message is already fetched `format=full`, so the header is in hand)
- [x] 3.4 Test with the real text of both templates as fixtures: the policyholder letter (`PetCover - Claim Further Information Required`, whose body says the claim "will be suspended") → `info_requested`; the vet cover note (`Petcover claim for Ari DC1-27-5628 Sr.8`, one-sentence body) → `info_requested`
- [x] 3.5 Test the genuine suspension letter (`Petcover Claim DC1-27-5628 SR1 - Claim suspended`) still classifies `suspended` — this is the pair the reorder must not collapse

## 4. Who owes the document

- [x] 4.1 Add recipient extraction (`To:` + `Cc:`) in `pipeline.poll_petcover_status` and widen `process_reply`'s signature to take them
- [x] 4.2 Add the resolver: recipient in `vet_contacts` → that clinic; other non-Justin address → unidentified vet, raw address kept; Justin's address only → Justin. Never default to Justin
- [x] 4.3 Record the resolved party (and clinic/address) on the status event's `detail`
- [x] 4.4 Test all three resolutions, including the unknown-address case asserting it is **not** attributed to Justin

## 5. Escalation

- [ ] 5.1 Add `INFO_REQUEST_DEADLINE_DAYS = 365` to `config.py` and a `days_to_deadline` field on info-request actions, computed from the claim's transaction date
- [ ] 5.2 Add action kind `chase_vet` to `ACTION_PRIORITY` (first) and `_ACTION_META`; move `confirm_resolved` to second; keep `_action_kind` returning exactly one kind per claim
- [ ] 5.3 Sort info-request actions by `days_to_deadline` ascending ahead of the existing date sort, leaving every other kind's ordering untouched
- [ ] 5.4 Exclude actions whose deadline has passed from `pending_actions` (they belong to the register's expired list)
- [ ] 5.5 Add `chase_vet`'s **wording** to `status_labels.LABELS` (one map, every surface — `clarify-claim-status-vocabulary` landed it) and only its **colour** to `claim_card._STATUS_COLOURS`, which is now keyed by status rather than by label text. Do not add a label in `claim_card.py` or `telegram_bot.py`: that is the duplication the vocabulary change deleted
- [ ] 5.6 `pipeline.nudge_stale_actions`: include info requests regardless of `ACTION_NUDGE_DAYS` and name the one closest to its deadline
- [ ] 5.7 Tests: priority ordering with a mixed action set; two info requests ordered by deadline not charge age; an info request younger than `ACTION_NUDGE_DAYS` still nudged; a past-deadline request yields no action

## 6. The register

- [ ] 6.1 Add `info_requests` to `db.py` (`CREATE TABLE IF NOT EXISTS`): `message_id` PK, `reference`, `sr`, `requested_at`, `owed_by`, `owed_by_email`, `pet_name`, `requested_document`, `claim_id` NULL, `outcome`, `created_at`
- [ ] 6.2 Write the single derivation used by both paths: given a classified info-request email plus the Petcover mail that follows it, produce the register row (including outcome per the spec's four cases)
- [ ] 6.3 Extract the requested document by regex from the letter (`we need a copy of …` / `please provide the following …`) — no LLM; leave it null when the text isn't there rather than guessing
- [ ] 6.4 Write the register row on the live path, in addition to the existing `claim_status_events` write
- [ ] 6.5 Add the register reads: actionable (`open`/`suspended`) and expired, each with the claim link where one exists
- [ ] 6.6 Tests: each of the four outcomes; a row with `claim_id = NULL`; re-deriving the same message id creates no duplicate

## 7. Backfill

- [ ] 7.1 Write `scripts/backfill_info_requests.py`: sweep the three Petcover senders with its own `--since` (default two years), classify, derive, write `info_requests` only
- [ ] 7.2 Assert the safety boundary in the script itself — it must not import or call `process_reply`, must not write `claim_status_events` / `vet_claims` / `processed_emails`
- [ ] 7.3 Retry with backoff and per-message-id caching (both a Gmail rate limit and a TLS `ConnectionResetError` were hit while investigating this change)
- [ ] 7.4 Run it live and reconcile against the ten known vet-addressed requests: `GABR-0305` ×2 (Feb 2025), `GABR-0306` (Feb 2025), `DC1-26-4751` ×2 (Mar 2025), `DC1-27-5628 SR1` and `DC1-27-5631 SR1` (Jan 2026), `DC1-27-5628 Serial 2` (19 Jul 2026), `DC1-26-5992 sr.1` and `DC1-27-5628 Sr.8` (27 Jul 2026)
- [ ] 7.5 Verify the expected outcomes on real data: `DC1-27-5628 SR1` → `suspended` (request 13 Jan, suspension 29 Jan, silence after); `DC1-27-5631 SR1` → `expired`; `DC1-27-5628 Serial 2` → `resolved` (claim #21 settled 24 Jul 2026); and record any that disagree with this prediction rather than adjusting the prediction
- [ ] 7.6 Confirm after the run that no claim's status, flag, or event count changed (snapshot before/after)

## 8. Surfaces

- [ ] 8.1 Dashboard: an information-request block above the ledger showing who owes it, the clinic address, what was requested, and days remaining
- [ ] 8.2 Dashboard: a separate expired list — reference, date, pet, vet, requested document — with no action controls
- [ ] 8.3 Telegram: `chase_vet` action cards carry the clinic name, email, and days remaining; expired requests produce no card
- [ ] 8.4 Test that no expired request reaches either action surface

## 9. Docs and close-out

- [ ] 9.1 New ADR: the addressee signal, and why outcome must be inferred (a vet reply is structurally unobservable — all ten threads single-message, Justin only Cc'd)
- [ ] 9.2 `app/openclaw/CLAUDE.md`: add non-breaking hyphens in Petcover letter text to the repeated-gotchas list; update the `claim_status.py` row
- [ ] 9.3 `README.md`: the lifecycle now branches on who owes the missing document, and the register exists alongside the claim list
- [ ] 9.4 Record in this file what was verified against real mail and the real DB versus only unit-tested

### 9.4 running record (kept current as work lands)

**Verified against the real mailbox** (7 real emails, 2026-07-27, read-only): classification of both Further-Information templates as `info_requested`; the genuine suspension letter still `suspended`; reference and Sr extraction on all four previously-broken emails; no regression on approval / acknowledgement / settlement; policy number yields no reference; auto-reply from `requiredinfo.au@` still `ignore`. Re-confirmed unchanged after the shape-first reordering.

**Verified against the real DB** (write, then rolled back): the re-read path. Outcome was a **failure** — 23 emails replayed, 11 events appended, and four claims regressed (#6, #7 `settled`→`acknowledged`, #18 `below_excess`→`acknowledged`, #22 `sent`→`below_excess`). Restored from `openclaw.db.bak-pre-inforequest`; live DB confirmed back at 20 events with every claim at its original status. Also refuted the prediction that detaching claim #2 would make it the Sr 8 target — it became the Sr 2 target and Sr 8 landed on claim #19.

**Audited read-only against the real mailbox and DB (2026-07-28)**: all 20 stored events re-classified with the fixed code — 6 disagree with what is stored, 3 more show a routing discrepancy, 1 acknowledgement is attached to no claim. Full table under group 1. No writes.

**Unit-tested only, not yet exercised live**: `detach_reference`, the event-idempotency guard, and the addressee resolver (`resolve_owed_by` — its `vet_contacts` matching is tested against seeded rows, not against a real polled header).

**Not built, therefore unverified**: everything in groups 0, 5, 6, 7, 8. The delta specs assert behavior for these; they must not be synced into `openspec/specs/` until built.
- [ ] 9.5 Deploy from `C:\Code\Me.OpenClaw-telegram-claimquery` (`docker compose up -d --build --force-recreate`) and confirm on the next tick that today's three real information requests appear as escalated actions with the right claim, the right clinic, and a deadline
