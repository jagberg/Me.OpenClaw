## Why

Petcover's letter does not say "further information". It says exactly what it wants:

> To assess your claim, we need a copy of **Consultation notes dated 18/05/2026**

The label currently reads **More vet info required** — accurate, and useless for acting on. Justin cannot chase a clinic for "information"; he chases them for consult notes dated 18 May. The letter already carries the answer, in a fixed template phrase, so nothing needs inferring.

Second, the chase has no heartbeat. Two vet-directed requests (19 and 27 July 2026) sat for a week and produced nothing, because a vet's reply to Petcover never reaches Justin's mailbox — the only signal is a *later* Petcover email citing the same reference and serial (ADR-0020). The existing `nudge_stale_actions` job fires daily but only past `ACTION_NUDGE_DAYS` and reports the oldest action by charge age, so an information request competes with every other pending item and is announced once. Justin asked for a specific rhythm: **Monday morning, list the vet requests nobody has answered.** A weekly beat matches how vet practices actually work — chasing a clinic on a Sunday achieves nothing — and one predictable message beats a daily reminder he learns to ignore.

## What Changes

- **The requested document is extracted and recorded.** A regex over the letter's own template phrasing (`we need a copy of …`, `please provide the following …`) writes `requested_document` onto the `info_requested` event's `detail`, next to the `owed_by` already there. Null when the phrase isn't present — no LLM, no guessing (this absorbs `vet-info-request-chase` task 6.3, which specified the same extraction for the register).
- **The label names the document**: `Consult notes needed` when the letter asked for consultation notes, and more generally the document's own short name. It falls back to today's wording — **More vet info required** / **Petcover needs info from you** — when the document is unknown. The document *and* who owes it both matter, so the vet-owed form reads `Vet: consult notes needed`.
- **A Monday-morning nudge for unanswered vet requests.** A new weekly job (`cron`, Monday, `ACTION_NUDGE_HOUR`) sends one message listing every claim whose outstanding information request is vet-owed and unresolved: claim id, pet, clinic name and email, the document requested, how long it has been outstanding, and days left against the treatment-anchored one-year deadline. Nothing outstanding → no message. This does **not** replace the daily stale-action nudge; it is a second, narrower beat, and it supersedes `vet-info-request-chase` task 5.6's plan to fold information requests into the daily one.
- **Deadline knob**: `INFO_REQUEST_DEADLINE_DAYS = 365` stays as the exclusion boundary (a past-deadline request is history, not an action). The Monday message reports days remaining so the pressure is visible weeks out rather than at the cliff.
- **The document's date is resolved to a visit we already hold.** `Consultation notes dated 18/05/2026` names a date, and that date is findable: it is Kings Vet invoice **1000229**, $351.50, whose line items include *"Consultation - Standard (Mon - Fri)"* — carried by claim **#6**, with its invoice PDF already on disk. The letter, though, sits on claim **#8** (a 2 April charge, thread `DC1-26-5992` Sr 1): Petcover is assessing the April claim and wants the notes from a *later* visit for the same condition (both are Raised ALT). So the system SHALL resolve the requested date against claims' stored invoices first and the `email_extractions` cache second — the Kings Vet bulk-history email already holds all eleven of Aari's invoices, including this one — and name the visit in the label's detail, the card and the Monday nudge: *"consult notes for the 18 May 2026 visit — Kings Vet invoice 1000229, claim #6"*. A clinic asked for "the notes from invoice 1000229" can answer in one look; asked for "further information", it does nothing.
- **What we hold is the invoice, not the notes.** Consultation notes are clinical records only the practice has; nothing here can produce or substitute them, and the resolved invoice is context for the chase, never an attachment sent in their place.
- **Line-item dates are captured, because today they are dropped.** Justin's point that an invoice's header date and its line items' dates differ is correct and currently untestable: of 41 stored line items, **zero** carry a date — the extraction schema keeps only `description` and `amount`. A requested date can therefore only be matched against invoice-level dates, which is exactly the case that misses a multi-visit invoice. The extraction schema gains an optional per-item `date`, and the resolver checks item dates as well as the invoice's own. **Cost, stated rather than discovered:** `email_extractions` caches successful extractions forever, so the 14 cached rows must be invalidated to pick up item dates — that is 14 re-extractions of real emails, spending LLM tokens against the daily budget (ADR-0017), and it is the only part of this change that does.
- **Still no chase email is drafted.** Read as confirmed: Justin gets flagged with the clinic's address and chases himself. `invoice_matching.draft_invoice_request` remains the upgrade path if the Monday nudge alone still doesn't move a clinic. Sending stays forbidden regardless (hard rule).

## Capabilities

### New Capabilities
<!-- none — this extends three existing capabilities -->

### Modified Capabilities
- `claim-status-tracking`: an information request records **what document** was requested, alongside who owes it.
- `claim-status-vocabulary`: an information request's label names the requested document when the letter states it, and only falls back to the generic wording when it does not.
- `telegram-bot`: a weekly Monday nudge lists every unanswered vet-directed information request with the clinic, the document, the visit it refers to, and the days remaining on the treatment deadline.
- `invoice-matching`: an extracted invoice's line items may carry their own date, because an invoice's header date is not always the date of the treatment on it.

## Impact

- Code: `claim_status.py` (`extract_requested_document`, recorded in `process_reply`; a query for unanswered vet requests), `status_labels.py` (document-aware wording), `pipeline.py` (`nudge_unanswered_vet_requests` + the Monday cron), `config.py` (`INFO_REQUEST_DEADLINE_DAYS`, `VET_NUDGE_DAY`).
- Data: **no schema change.** `requested_document` joins `owed_by` in the existing `claim_status_events.detail` JSON. Events already recorded carry no document; the two live ones (10, 31) can be backfilled by re-reading their own stored emails read-only, or left null — either way the label degrades to the current wording rather than breaking.
- Tests: `app/tests/test_core.py` — the real letter's phrase (`we need a copy of Consultation notes dated 18/05/2026`), a letter with no such phrase, the label for each combination of document × `owed_by`, and the Monday job (fires only on vet-owed unresolved requests, silent when there are none, excludes past-deadline).
- Docs: amend ADR-0021 (the label now names the document, which its "who holds it" rule implies but does not state) and ADR-0020's addressee section; `README.md` lifecycle; `app/openclaw/CLAUDE.md`.
- Coordination: `vet-info-request-chase` tasks 5.6 and 6.3 are absorbed here and should be marked as such; its group 5's `chase_vet` action kind is unaffected and still its own.
- No third-party call changes, no LLM, $0.
