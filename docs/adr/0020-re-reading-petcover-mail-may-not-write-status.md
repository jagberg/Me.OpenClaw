# ADR 0020: Re-reading Petcover mail may not write status; the obliged party comes from the recipients

- Status: accepted
- Date: 2026-07-27

## Context

Petcover's "Further Information Required" letter is the one point in the lifecycle where a claim stops moving until a **third party** acts — usually the vet, asked for consult notes, with Justin only on Cc. Ignored requests have already cost real claims (`ELD-25-2728 - Declined - Invoices over 12 months`, Aug 2024).

Three forces met here on 2026-07-27:

1. **Four defects meant these letters routed to the wrong claim or produced no action.** The letters carry the reference with U+2010 non-breaking hyphens, which truncated `DC1-26-5992` to `DC1`; two serial formats (`Sr.8`, `Serial Number: N`) were unrecognized; the vet-addressed cover note's reference lives only in a free-form subject; and the request letter's own sentence *"Your claim will be suspended until we have the required information"* caused every request to be filed as a suspension.

2. **Fixing a classifier does nothing for mail already read.** `processed_emails` blocks re-reading, so each fix only ever helps the *next* letter. The four misclassified emails would have stayed wrong permanently.

3. **`telegram-agent-reach` had already rejected force-reprocessing**, on stated grounds: *"Replaying a seen email against the append-only event log risks re-applying a status transition — and status is the thing this whole service exists to track correctly. The `_already_processed` guard is load-bearing, not incidental."*

The first attempt treated (3) as an obstacle that event-level idempotency dissolved: skip any `(raw_email_id, claim_id, event_type)` already logged, and a re-read becomes a no-op for anything already applied. That was tried against the real DB and **failed**. Re-reading 23 emails regressed four claims — #6 and #7 from `settled` to `acknowledged`, #18 from `below_excess` to `acknowledged`, #22 from `sent` to `below_excess`. Restored from backup.

The reasoning error is worth naming: a re-read exists *because routing changed*. Claims therefore receive events they never had, so a guard that only suppresses identical triples almost never fires on the events that matter, and replaying oldest-first leaves each claim wherever its last-routed letter points.

## Decision

**1. A re-read appends events and learns references. It never writes `vet_claims.status` and never writes `flag`.**

`poll_petcover_status(reread=True, since=…)` ignores the `processed_emails` guard so a classification or extraction fix can be applied to mail already ingested. Under a re-read, `process_reply` records events and may learn `petcover_reference` / `petcover_sr`, and does nothing else. Status stays owned solely by the forward-only live path. `needs_action` and the information-request register are event-driven, so a corrected `info_requested` still surfaces.

This is a **narrow exception to** the prior no-force-reprocess decision, not a reversal of it. That decision's stated risk is exactly what occurred, and the guard remains load-bearing for the default path.

**2. Who owes a requested document is read from the letter's `To:`/`Cc:`, never from its sender.**

A recipient matching `vet_contacts` means that clinic owes it; any other non-Justin recipient means an unidentified vet owes it, recorded with its raw address; only Justin's own address means Justin owes it. An unrecognized address is **never** attributed to Justin.

The sender cannot answer this: `claims.au@petcovergroup.com` sends both kinds — `GABR-0305-Request for consult note` went to a vet, `GABR-0306 First Request for CF` went to Justin.

**3. A request's outcome is inferred from later Petcover mail, because a vet reply is unobservable.**

All ten vet-addressed request threads in the mailbox contain exactly one message: Justin is Cc'd and the vet replies to Petcover, so the reply never reaches this mailbox. Outcome is therefore derived from whether later Petcover mail cites the same `(reference, Sr)` — resolved / suspended / open / expired, the deadline anchored on the **treatment** date per the letter's own one-year term.

**4. References are extracted by shape first, context phrase second.**

A context phrase captures whatever token follows it. Live subject `Petcover claim for--Aari--DC1-27-5628 Serial Number: 2` yields `for--Aari--DC1-27-5628`, and the first implementation wrote exactly that to a claim. The policy number `GABR-0306-DC1-00000001R` is deleted before shape matching — it is the one string shaped enough to be mistaken for a reference, and the original reason bare patterns were rejected.

## Consequences

- Any future classifier or extractor fix has a supported way to be applied retroactively, which it did not before.
- A claim whose status is genuinely wrong because of the old routing will **not** self-correct on a re-read. Correcting it is a deliberate act — `claim_status.detach_reference()` returns a mis-learned reference to the correlation pool, logged as a `reference_detached` event rather than silently wiped. Reference learning previously had no undo, and a mis-learned reference is self-sealing: the claim stops being an un-referenced candidate, so correlation can never reconsider it.
- Erring toward chasing is deliberate and asymmetric: a vet who did reply to a request Petcover then sat on reads as `open`, costing Justin a wasted email. The inverse error costs a claim.
- **Implementation status, honestly:** decisions 2 and 4 are implemented, unit-tested, and verified against the seven real Petcover email types. Decision 1's constraint is decided but **not yet implemented** — `reread`/`since` exist on `poll_petcover_status` and nothing calls them, because the status suppression is still to be written. Decision 3 is decided and specified; the register that acts on it is not built. See `openspec/changes/vet-info-request-chase`.
- **Known limitation, unresolved:** two requests on one thread sent to *different* clinics minutes apart (`DC1-27-5628` Sr 2 to Kings Vet, Sr 8 to The Shire Vet), with neither serial held by a claim, collide. The first consumes the only un-referenced candidate and the second routes by reference alone onto an unrelated claim. `correlate_ack`'s recency rule assumes one outstanding request per pet. Likely resolution is explicit `(reference, Sr)` assignment by Justin, not a better heuristic.
- Supersedes nothing. Amends ADR-0011's reference/serial formats with two more confirmed serial spellings and the Unicode-hyphen normalization.

## Amendment (2026-07-28) — the same letter also says WHAT was asked for, and the chase needs a weekly beat

The decision stands. Two additions, both from the same letters this ADR was written against.

**The document is extractable, by regex.** Decision 2 established that the letter's `To:`/`Cc:` says *who* owes the document. The letter's body says *what*, in the same template register: `we need a copy of` / `please provide the following`, followed by the item, terminated by the standard footer. It is recorded on the `info_requested` event as `requested_document`, with the treatment date it names parsed to ISO as `requested_document_date`. No LLM, consistent with the rule that classification and extraction stay keyword/regex where they can.

**The date resolves to a visit we already hold, and usually a different claim's.** Live: the request sits on claim #8 (a 2 April charge) and `18/05/2026` is claim #6's Kings Vet invoice **1000229** — a later visit for the same condition, whose invoice PDF was already on disk. Petcover assesses one claim using another visit's notes. `invoice_matching.find_visit_by_date` searches claims first, then the extraction cache for a visit no claim covers, returns **every** match (two invoices can share a date), and returns nothing rather than a nearest-date guess. The resolved invoice is context that makes the chase answerable — it is never attached or offered as the document, because consultation notes are clinical records only the practice holds.

**Chasing needs a weekly beat, because a vet reply is unobservable.** This ADR established that the only signal is later Petcover mail citing the same reference and serial. What it did not settle was cadence. Two vet-directed requests (19 and 27 July) then sat a week producing no message at all: `nudge_stale_actions` fires only past `ACTION_NUDGE_DAYS` and names the single oldest action by charge age, so an information request competes with everything else and is announced once. Justin's instruction was specific — Monday morning. So `nudge_unanswered_vet_requests` runs weekly (`VET_NUDGE_DAY`, default `mon`, at the existing nudge hour), listing every unresolved vet-owed request with the clinic, its address, the document, the resolved visit, days outstanding, and **days remaining against the treatment-anchored one-year deadline** rather than charge age. Silent when nothing is outstanding: a weekly "nothing to do" is how a channel stops being read. Past-deadline requests are excluded — they are history for the register, not an action anyone can still take.

### Correction to the amendment above (2026-07-29) — what "treatment-anchored" actually means, and that it was not true when claimed

The amendment written 2026-07-28 states that the weekly nudge reports "days remaining against the treatment-anchored one-year deadline". **That was the intent, not the behaviour.** The code shipped the same day computed the countdown from `bank_transactions.date` — the bank charge — with the local variable even named `treatment`. Four other documents (this change's `claim-status-tracking` and `telegram-bot` delta specs, `app/openclaw/CLAUDE.md`, `README.md`) repeated the same claim on the same day. So for roughly a day the record asserted a rule the implementation did not follow, and nothing would have revealed the disagreement.

It surfaced because Justin asked whether a specific pair of dates changed anything: the receipts his wife forwarded on 27 Jul show The Shire Vet treated Aari on **19 Jun 2026** (`SHV49c1622284e5`, $35.00) and Echo on **30 Jun** (`SHVd5b232905fdb`, $369.33), while **both** were paid on **06/07/2026**. Measured against the live claims, charge-anchoring over-granted **17 days** on claim #1 and **6 days** on claim #25. Nothing bounds that gap in general — this project's own notes record an invoice arriving a year after its transaction.

The rule, now implemented (`claim_status.treatment_date`):

- The **earliest** date on the invoice wins — its own `date`, or any line item's. An invoice billing several visits expires on its oldest one, so the earliest is the only safe anchor.
- With no invoice attached, it falls back to the transaction date **and says so**: the nudge prints `treated <date>` or `treated <date>, assumed = charge date`. An assumed anchor is visible rather than silently equated with a known one.

**Deliberately not changed, and unresolved:** `claim_status._policy_year_key` still derives the excess/cap policy year from the transaction date. It is the same question — which date is a claim's true anchor — applied to money rather than to a deadline, and moving it would shift settlement expectations. It also touches the already-recorded, still-undecided disagreement about closed policy years in `dashboard-visit-ledger`. Tracked in `openspec/BACKLOG.md`; not decided here, and not quietly aligned.

## Amendment (2026-07-30) — Decision 1 is now partly implemented, and partly not implementable as written

`claim-state-from-event-log` Phase 1 shipped and deployed (`1f49871+deploy`). It changes what Decision 1 above can mean, in two directions, so the "not yet implemented" note in Consequences is superseded rather than merely satisfied.

**Implemented, but by a different mechanism than this ADR proposed.** Decision 1 said status stays "owned solely by the forward-only live path", with a re-read suppressing status writes as a special case. That special case no longer exists and is no longer needed: `claim_status.apply_event` is now the only writer of `vet_claims.status`, and it consults a declared transition table on *every* write, re-read or not. A re-read appends its events and the table decides. The suppression became a property of the machine instead of a rule about one code path — which is strictly better, because it also covers the paths nobody thought to suppress.

**Not implementable as written, for half the incident.** This ADR's Context treats all four of the 2026-07-27 regressions as one failure. They are two:

- Claims #6 and #7 moved `settled` → `acknowledged`. That pair is undeclared, so `apply_event` now refuses it, flags the claim naming both states, and keeps the event as evidence. Covered.
- Claim #22 moved `sent` → `below_excess` and claim #18 `below_excess` → `acknowledged`. **Both are legal transitions and must stay legal** — `below_excess` is non-terminal by decision, the invoice being retained. What was wrong was that the event reached the wrong claim. A state machine cannot refuse a legal move on the grounds that it was misrouted; refusal is not the right instrument for a routing fault.

So the second half has **no demonstrated guard**. `_already_recorded` is the obvious candidate and this ADR already records it as tried and insufficient — its `(email, claim, event)` key deduplicates correctly and says nothing about whether the claim was the right one. Reference/Sr routing precedence is the other candidate and has never been tested against a replay of misrouted mail.

**Consequence accepted:** a re-read whose event is a legal move applied to the wrong claim would still be applied today, and would look entirely legitimate to the machine. This is recorded in `openspec/BACKLOG.md` with the two ways it could close — demonstrate which mechanism rejects it, or decide that Phase 2's `revert_state` is the intended remedy and this is an undo problem rather than a prevention one. It is deliberately not being closed by tightening the transition table, which would make the table lie about the lifecycle to compensate for a different subsystem's bug.

**Correction to this ADR's own framing:** phrases like "the four live misclassifications" were carried into `README.md`, the change's delta spec, `design.md` and a `claim_status.py` docstring, all asserting a guarantee that covers two of the four. Found by an `eval-change` run on 2026-07-30 and corrected in each; noted here because this ADR is where the four-as-one framing started.

## Amendment (2026-07-31) — the correction claimed above was itself incomplete

The 2026-07-30 amendment ends: *"phrases like 'the four live misclassifications' were carried into `README.md`, the change's delta spec, `design.md` and a `claim_status.py` docstring, all asserting a guarantee that covers two of the four … and corrected in each."*

**"Corrected in each" was false when written.** A second `eval-change` run on 2026-07-31 found the retracted guarantee still standing in two of the four:

- `openspec/changes/claim-state-from-event-log/specs/claim-state-machine/spec.md` — *"Those two are guarded by event idempotency and reference/Sr routing precedence … and the system SHALL NOT claim otherwise"*. This is the artifact `sync` copies into the baseline, so it was the one place the error would have become permanent.
- `design.md` Decision 3's correction paragraph — which retracted the claim in its opening sentence and restated it in its conclusion.

The retraction had reached `tasks.md`, `README.md`, the `claim_status.py` docstring and the *sibling* delta (`claim-status-tracking`), and the commit message asserted "the delta spec" singular, which was true of the one edited and false of the one that mattered.

Both are now corrected to state the actual position: those two transitions have **no demonstrated guard**, tracked in `openspec/BACKLOG.md`.

**Why this is worth an amendment rather than a quiet edit.** The same commit that left the delta spec wrong (`b0583ed`) added a row to `docs/failure-modes.md` naming this exact rule — *"correct the delta spec first, since it is the only one that becomes permanent"*. Writing the rule and breaking it in one commit is the more useful record than a clean correction would have been: a claim of completeness ("corrected in each") is itself a factual claim, and it went into an accepted ADR unverified. Enumerate the sites and check them off; do not assert coverage from memory.
