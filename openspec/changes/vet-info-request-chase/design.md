## Context

Petcover's "Further Information Required" letter is the one point in the claim lifecycle where the claim stops moving until a **third party** acts. Two templates carry it, both confirmed live on 2026-07-27:

- **Policyholder letter** — `To: jagberg@gmail.com`, full body: policy number, pet, `Claim Reference: DC1‐26‐5992 Sr 1`, condition, the document wanted (`Consultation notes dated 18/05/2026`), the sentence *"Your claim will be suspended until we have the required information"*, and the one-year submission warning.
- **Vet cover note** — `To: info@kingsvet.com.au`, `Cc: jagberg@gmail.com`, sent from `requiredinfo.au@petcovergroup.com`. One sentence of body (*"Dear The Shire Vet, We recently received a claim for treatment provided to Ari … please provide the following"*), the reference only in the **subject**, the detail in an attachment whose text does not come back through `full_message_text`.

Ten of these went to vets in the last two years. Every one of those threads has exactly one message in it — Justin is only Cc'd, and the vet answers Petcover directly. **There is no observable "the vet replied" event in his mailbox.** Any design that waits for one waits forever.

Constraints inherited from the project, not chosen here: never send email (drafts only); never guess a required field; failures must be visible; classification and reference parsing stay regex/keyword (no LLM); a live `ALTER TABLE` needs manual DDL, so new state prefers a new table.

## Goals / Non-Goals

**Goals:**

- An information request routes to the claim it actually names.
- The register records **who owes the document** — the vet (with the clinic and address to chase) or Justin.
- Urgency is measured against the deadline that actually kills the claim: one year from the **treatment date**.
- Two years of history is inventoried, including requests that can never be linked to a claim, with anything past the deadline listed for manual handling rather than presented as actionable.
- Backfilling history cannot mutate current claims.

**Non-Goals:**

- Drafting or sending a chase email to the vet. Justin asked to be flagged; the card carries the clinic address. `invoice_matching.draft_invoice_request` is the upgrade path if flagging alone still gets ignored.
- Detecting that a vet replied. Structurally impossible from this mailbox (see Context).
- Any LLM call.
- Retroactively rewriting `claim_status_events` history. One deliberate, recorded data repair only.

## Decisions

### 1. Normalize Unicode dashes once, at the extraction seam

The letters render the reference with U+2010 non-breaking hyphens (`DC1‐26‐5992`). `extract_reference`'s `[A-Za-z0-9-]+` stops at the first one and learns `DC1`. That single character is the root cause of today's live misroute: the letter about claim #8 failed its exact `(reference, Sr)` lookup, fell through to recency correlation, and attached to claim #2.

A shared `_normalize(text)` maps U+2010–U+2015 and U+2212 to `-` and is applied at the entry of `classify`, `extract_reference`, `extract_sr`, and the settlement/approval extractors.

*Alternative rejected*: widening each character class to `[A-Za-z0-9\-‐-―]`. Same fix in five places, and the captured reference would then carry a non-ASCII hyphen into `petcover_reference`, where it would never match a stored ASCII one. Normalizing at the seam keeps stored data canonical.

*Why the settlement extractors too*: their patterns match `$` amounts, not references, so they are not broken today — but they read the same PDF text, and one letter using an en dash in `Less Fixed excess` would fail silently. Normalizing everything costs nothing and removes the class of bug rather than the instance.

### 2. Shape-anchored reference extraction, with the policy number explicitly excluded

**Shape first, context phrase second** (revised 2026-07-27 after a live failure — see Changelog).

The shape is `[A-Za-z]{2,4}\d?-\d{2}-\d{4}` or `GABR-\d{4}`, case-insensitive, matched after the policy number has been deleted from the text. The policy number `GABR-0306-DC1-00000001R` is the one string shaped enough to be mistaken for a reference — the documented reason bare patterns were rejected here originally — and stripping it first is cheaper and more honest than a regex that steps around it.

Context phrases (`Claim Reference:`, `Claim Number`, `Petcover Claim`) run second, as the fallback for a format the shape doesn't know, where a loose capture beats none. A phrase capture must still contain a hyphen and a digit, because a phrase can sit next to a bare word (`Petcover claim for Ari …` captures `for`).

*Alternative rejected*: adding more context phrases as they appear. Petcover has used at least five subject shapes in two years; each new one is another lost claim before it is discovered. Shape is stable across all of them.

### 9. A re-read of already-ingested mail must write no status at all

A classification fix is useless against mail already in `processed_emails` — every fix would only ever help the *next* letter, and the four live misclassifications would stay wrong permanently. So `poll_petcover_status` gains `reread=True`, which ignores that guard.

The guard exists for a stated reason. `telegram-agent-reach` design.md, Decision 3: *"Replaying a seen email against the append-only event log risks re-applying a status transition — and status is the thing this whole service exists to track correctly. The `_already_processed` guard is load-bearing, not incidental."* That decision stands. This is a narrow exception to it, not a reversal, and it is only safe under a hard constraint:

**A re-read appends events and learns references. It never writes `vet_claims.status` and never writes `flag`.**

Event-level idempotency alone is *not* sufficient, which was the original (wrong) form of this decision — the trial that disproved it is in the Changelog. The reason is that a re-read exists because routing *changed*: claims legitimately receive events they never had, so suppressing only identical `(email, claim, event)` triples still lets a replayed older letter set a claim's status behind where its lifecycle actually is.

`needs_action` and the register are event-driven, so a corrected `info_requested` still surfaces without touching lifecycle state. Status remains owned solely by the forward-only live path.

## Changelog

## 2026-07-27 - Reference extraction is shape-first, not context-phrase-first

**Decision:** Match the reference by its own shape before trying context phrases, inverting Decision 2's original order.
**Reasoning:** A context phrase captures whatever token follows it. Live subject `Petcover claim for--Aari--DC1-27-5628 Serial Number: 2` yielded `for--Aari--DC1-27-5628`, and a live trial wrote exactly that string to claim #2 as its reference — replacing one junk reference with another. A shape match is unambiguous when it hits; a phrase capture never is.
**Trade-off accepted:** A reference in a genuinely new format that the shape doesn't recognize now falls through to the phrase path, which is looser. Verified against all seven real Petcover email types: identical, correct references either way.
**Supersedes:** Decision 2's "context phrases stay first — they are the highest-confidence signal", preserved below.

## 2026-07-27 - A re-read must write no status, not merely dedupe events

**Decision:** `reread=True` appends events and learns references only. Status and flag writes are suppressed entirely on a re-read.
**Reasoning:** The first implementation assumed event-level idempotency was sufficient. A live trial disproved it: re-reading 23 emails regressed claims #6 and #7 from `settled` to `acknowledged`, #18 from `below_excess` to `acknowledged`, and #22 from `sent` to `below_excess`. A re-read exists precisely because routing changed, so claims receive genuinely new events, and replaying oldest-first leaves each claim wherever its last-routed letter points. The DB was restored from backup; no email was sent.
**Trade-off accepted:** A claim whose status is genuinely wrong because of the old routing will not self-correct on a re-read. Correcting it stays a deliberate act (`detach_reference`, manual confirm), which is the right default for the field this service exists to track.
**Supersedes:** n/a — `telegram-agent-reach` Decision 3 is *upheld*, not superseded. Its stated risk is exactly what occurred.

## Evolution of thinking

### Context phrases are the highest-confidence signal, shape is the fallback (SUPERSEDED 2026-07-27)

> Context phrases (`Claim Reference:`, `Claim Number`) stay first — they are the highest-confidence signal and they are what the letters use. They fail on the vet cover note, where the reference lives in a free-form subject (`Petcover claim for Ari DC1-27-5628 Sr.8`) and the existing patterns are additionally case-sensitive.
>
> A shape fallback runs second … So the fallback rejects a candidate that is immediately preceded or followed by `-` plus another alphanumeric group, which is exactly what distinguishes `GABR-0306-DC1-…` from a standalone `GABR-0305`.

**Superseded by:** Changelog, 2026-07-27, "Reference extraction is shape-first".
**Why it changed:** "Highest-confidence" conflated *the phrase being a reliable marker* with *the capture after it being a reliable reference*. The phrase is reliable; the capture is not. Also, the preceded/followed-by-hyphen guard was wrong in both directions — it would have rejected the real standalone reference in `GABR-0305-Request for consult note`, whose reference is followed by `-Request`.

### Event-level idempotency makes a re-read safe (SUPERSEDED 2026-07-27, same day)

> `_record_event` skips when `(raw_email_id, claim_id, event_type)` already exists, and `process_reply` skips the status/flag write for a duplicate. That makes re-reading old mail safe — nothing double-logs, and a settlement mismatch you already dismissed can't come back.

**Superseded by:** Decision 9 and the Changelog entry above.
**Why it changed:** Tested live rather than reasoned about further. The guard only suppresses *identical* triples, and a re-read's whole purpose is to produce *different* routing — so the duplicate test almost never fires on the events that matter, and four claims regressed.

### 3. Classification: `info_requested` outranks `suspended`, and the sender decides on its own

The request letter contains the word "suspended" — about its own future — so ordering `suspended` ahead of `info_requested` in `SUBJECT_KEYWORDS` misclassifies it. The live DB has zero `info_requested` events and two `suspended` ones, both of which are actually requests. A genuine suspension letter also exists (`DC1-27-5628 SR1 - Claim suspended`, 29 Jan 2026), so conflating them destroys the distinction between *"a document is missing"* and *"we have stopped assessing"*.

Two changes: `info_requested` moves ahead of `suspended`, and gains the live phrases (`further information required`, `information required`, `please provide the following`, `request for consult note`, `request for cf`). Separately, **any** email from `requiredinfo.au@petcovergroup.com` is an information request by sender alone — it is a dedicated channel and needs no text match, which is what rescues the one-sentence vet cover note that classifies as `unclassified` today.

This requires `process_reply` to receive the sender, which it does not today.

### 4. The `To:` header is the addressee signal

`process_reply(email_id, subject, body)` never sees headers, so nothing can distinguish "the vet owes consult notes" from "you owe a completed claim form" — a distinction that decides who Justin has to contact. The signature gains recipients, and `pipeline.poll_petcover_status` passes them from the message it already fetches in full.

Resolution order: a recipient matching `vet_contacts.email` → that clinic owes it (name and address available for the card); any other non-Justin address → an unknown vet owes it, flagged with the raw address rather than guessed at; only Justin's address → Justin owes it.

*Alternative rejected*: inferring the party from which sender it came from. `claims.au@` sends both kinds — `GABR-0305-Request for consult note` went to a vet and `GABR-0306 First Request for CF` went to Justin, both from `claims.au@`. The `To:` header is the only reliable discriminator.

### 5. Outcome is inferred from later Petcover mail, never from a vet reply

Since a vet reply is unobservable, outcome is derived from whether the claim moved afterwards — a later Petcover email citing the same `(reference, Sr)`:

| Outcome | Derivation | Surface |
|---|---|---|
| `resolved` | a later `acknowledged` / `approved` / `settled` cites it | closed, no action |
| `suspended` | a later suspension letter cites it, nothing after | actionable, escalated |
| `open` | nothing cites it after, treatment under one year old | actionable, escalated |
| `expired` | nothing cites it after, treatment over one year old | listed only, no buttons |

The `DC1-27-5628 SR1` sequence (request 13 Jan → suspension 29 Jan → silence) is the reference case this table is built from.

*Trade-off accepted*: a vet who replied to a request Petcover then sat on reads as `open`. Justin chases, learns nothing was needed, and confirms it resolved — a wasted email. The inverse error loses a claim. Asymmetric, so the design errs toward chasing.

### 6. A separate `info_requests` table, not more `claim_status_events`

Most of the history cannot attach to a claim at all: `GABR-0305`, `GABR-0306`, `DC1-26-4751`, `DC1-27-5631` and `DC1-27-5628 Sr1` predate every transaction on file (bank CSV coverage starts 2025-07-17). Recording them as `claim_id IS NULL` status events would drop them into the "needs manual link" review queue, whose whole premise is that a human links them to a claim — and these have no claim to link to, now or ever. The queue would never drain.

A new table with a nullable `claim_id` says what is true: this is a request that exists, whose claim may be unknown. It is a new table, so `CREATE TABLE IF NOT EXISTS` in `db.py` is sufficient — no manual live DDL.

Live requests write to **both**: `claim_status_events` (the claim's history, unchanged) and `info_requests` (the register). One derivation function builds the row, used by the live path and the backfill, so the two cannot disagree.

### 7. The backfill never touches claim state

`scripts/backfill_info_requests.py` reads Gmail and writes `info_requests` only. It does not call `process_reply`, does not write `claim_status_events`, does not update `vet_claims`, and does not mark `processed_emails`. `PETCOVER_STATUS_SINCE` exists because replaying old mail through the live path fabricates status events on current claims and mis-correlates acknowledgements; a backfill that ignored that would corrupt working claims to inventory dead ones.

It links to a claim opportunistically — exact `(reference, Sr)`, then reference-only — and leaves `claim_id NULL` otherwise. Re-runnable: keyed on the Gmail message id.

### 8. Escalation is deadline-driven, not age-driven

`pending_actions` sorts by transaction date and `ACTION_PRIORITY` puts `confirm_resolved` third, so an information request competes on charge age with a missing condition. The new `chase_vet` kind and `confirm_resolved` move to the top of `ACTION_PRIORITY`, and info-request actions carry `days_to_deadline` = (treatment date + 365) − today, sorted ascending so the nearest-to-expiry is first.

The daily `nudge_stale_actions` currently filters on `age_days >= ACTION_NUDGE_DAYS` and reports the oldest action. Info requests bypass that threshold — the point of the whole change is that these are ignored — and the nudge names the request closest to its deadline.

*Alternative rejected*: a separate scheduled job for info requests. `nudge_stale_actions` already runs daily with Telegram wiring and card rendering; a second job is a second thing to watch die.

## Risks / Trade-offs

- **Shape fallback matches something that isn't a reference** → It runs only after the context phrases fail, requires the full `XXX-##-####` / `GABR-####` shape, and rejects policy-number context. A wrong reference is worse than none, so a regression test asserts the real policy number `GABR-0306-DC1-00000001R` yields no reference from the shape path.
- **Reordering `SUBJECT_KEYWORDS` reclassifies genuine suspensions** → The real suspension subject (`DC1-27-5628 SR1 - Claim suspended`) carries no information-request phrase, and the request letter's only `suspended` hit is its own forward-looking sentence. Both real emails are pinned as test fixtures.
- **Existing `suspended` events are really requests** → They stay as recorded; the append-only log is not rewritten. The register derives from the mail, so it reports them correctly regardless of what the historical event says.
- **Claim #2 still holds `petcover_reference = 'DC1'`** → The fix prevents new corruption but does not undo this. Repaired by hand, and until then a stray `DC1` extraction could cross-link unrelated threads. First task in the list.
- **`expired` items look like a to-do list of losses** → That is what Justin asked for, and they are rendered as a plain list with no action buttons so they cannot be mistaken for recoverable work.
- **Backfill hits Gmail rate limits or a TLS reset** → Observed live during this investigation. Retry with backoff, and cache per message id so a re-run resumes rather than restarts.
- **A vet address changes and no longer matches `vet_contacts`** → Falls back to "unknown recipient" with the raw address shown, never to "Justin owes it". Silently reassigning the obligation to Justin is the failure that loses the claim.

### Known limitation: two requests on one thread, to different clinics, collide (found live 2026-07-27)

Petcover sent two information requests for thread `DC1-27-5628` minutes apart, to *different* clinics — Sr 2 to `info@kingsvet.com.au`, Sr 8 to `admin@theshirevet.com.au`. Neither serial was held by any claim. In the live trial the first request consumed the only un-referenced awaiting candidate (learning the reference onto it), so the second fell through to reference-only routing and attached to claim #19, which the letter has nothing to do with.

This is not fixed by anything in this design. `correlate_ack`'s recency rule assumes one request per pet at a time, and a thread whose serials arrive out of band breaks that assumption. A prediction made earlier in this change — that clearing claim #2's junk reference would make it the correct target for the Sr 8 letter — was **refuted live**: it became the target for the Sr 2 letter instead.

Likely resolution is explicit `(reference, Sr)` assignment by Justin for an unheld serial, rather than any correlation heuristic. Recorded as a task, not designed here.

## Migration Plan

1. Repair claim #2 by hand (clear `petcover_reference`, re-point event 28 to claim #8), recorded in `tasks.md`.
2. Ship the extraction and classification fixes with their tests — these stand alone and are worth deploying even if nothing else lands.
3. Add the table and the live register write.
4. Run the backfill, review its output against the ten known vet-addressed requests before trusting it.
5. Surface the register on the dashboard and Telegram.

Rollback: the table is additive and the register is read-only to the rest of the system, so reverting is dropping the surfaces. The extraction fixes are the only behavioral change to the live path and are covered by tests against real email text.

## Open Questions

- Does Petcover ever send a *second* request for the same `(reference, Sr)`? `GABR-0306` has a "First Request"/"Second Request" pair addressed to Justin, so likely yes for vets too. The register keys on message id, so both are recorded; whether the surface should collapse them into one entry with a count is deferred until a real vet-addressed pair is observed. **Partially answered 2026-07-27**: two requests on one thread with *different* serials to *different* clinics are confirmed real (see Known limitation). Same `(reference, Sr)` twice is still unobserved.
- The vet cover note's detail lives in an attachment `full_message_text` returns nothing for. Worth determining whether it is a non-PDF attachment or a PDF that fails extraction — the requested-document line would otherwise be unavailable for exactly the requests that matter most. Not blocking: the subject carries the reference and Sr, which is what routing needs.
