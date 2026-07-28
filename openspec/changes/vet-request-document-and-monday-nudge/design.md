## Context

Three things are already in place and this change only leans on them:

- `claim_status.resolve_owed_by` records `owed_by` (+ clinic email) on every `info_requested` event, from the letter's `To:`/`Cc:` matched against `vet_contacts` (`4a7fb6d`).
- `status_labels` is the single vocabulary; `label(claim, owed_by)` already branches on who owes the document (ADR-0021).
- `pipeline.nudge_stale_actions` is the state-based counterpart to the change-feed, running daily at `ACTION_NUDGE_HOUR` via an APScheduler cron job. Adding a second cron job is one `scheduler.add_job` call.

Two facts constrain the nudge:

- **A vet's reply is structurally unobservable.** All ten historical vet-addressed request threads contain exactly one message: the clinic replies to Petcover, Justin is only Cc'd, and that reply never lands in his mailbox (ADR-0020). "Unanswered" therefore cannot mean "no reply seen" — it can only mean "our claim still sits at an unresolved information request".
- **The deadline is treatment-anchored**, not request-anchored: *"your claim must be submitted within one year of your pet receiving treatment"*. A request made late in that year has less slack than its own age suggests, so age alone is the wrong number to report.

Live state this was written against (read-only, 2026-07-28): claim #8 `info_requested`, owed by Kings Vet, document *Consultation notes dated 18/05/2026*; events 10 and 31 are two further vet-owed requests not linked to any claim.

## Goals / Non-Goals

**Goals:**

- The label names the document, so it can be acted on without opening the letter.
- The requested date resolves to a visit we already hold, so the chase names an invoice number instead of a month.
- One predictable weekly message listing every vet request nobody has answered, with the clinic, the document and the days remaining.
- No DB schema change, no new third-party call. One bounded token spend: re-extracting 14 cached emails to pick up line-item dates.

**Non-Goals:**

- Drafting or sending a chase email. Confirmed: flag only.
- Detecting a vet's reply. It is not observable; the absence of later Petcover mail is the only proxy and it already drives the register's outcome derivation in `vet-info-request-chase`.
- Assigning the two unlinked requests (events 10, 31) to claims — that is task 0.4 of the other change, and this change's Open Questions carries the data for deciding it.
- Changing the daily stale-action nudge. It keeps its cadence and its scope.

## Decisions

### 1. The document comes from the letter's own template phrase, by regex.

The letter reads:

```
Further Information Required
Thank you for submitting your claim for treatment provided to Ari. To assess your claim, we need a copy of
Consultation notes dated 18/05/2026
Please note we cannot process the claim without the information requested.
```

`extract_requested_document(text)` captures what follows `we need a copy of` / `we require a copy of` / `please provide the following`, up to the next sentence-ending template line (`Please note`, `You can reach us`, `In line with`), then collapses whitespace. Returns `None` when no phrase matches — a null document costs nothing (the label falls back), whereas a wrong document sends Justin chasing the wrong paperwork.

Not an LLM call: the phrasing is template-generated and the standing rule is that classification and extraction stay keyword/regex where they can. Not stored in a column either — `detail` already carries `subject`, `owed_by`, `clinic_email` and the settlement figures, and a column needs hand-run DDL on the live DB.

### 2. The label names the document; who owes it stays in front.

| `owed_by` | document known | label |
|---|---|---|
| vet | yes | `Vet: consult notes needed` |
| vet | no | `More vet info required` |
| justin | yes | `Consult notes needed from you` |
| justin | no | `Petcover needs info from you` |
| unrecorded | either | `Info requested` |

The document is shortened for a chip: the stored value is the letter's full phrase (`Consultation notes dated 18/05/2026`), but a table chip and a Telegram card row cannot carry a date. `_short_document(text)` maps the recognized kinds — consultation notes, itemized invoice, completed claim form, referral history — to a short noun phrase, and falls back to the generic label when it recognizes none. The **full** phrase appears where there is room: the Monday nudge, the action card, the claim detail.

Alternative: put the full phrase in the chip and let the table wrap. Rejected — the ledger's density is a stated requirement of `dashboard-visit-ledger`, and a wrapping chip pushes every other column around.

Alternative: drop `owed_by` from the label now that the document is there. Rejected — the document says *what*, not *who*, and getting the who wrong is the failure that loses a claim.

### 3. The requested date is resolved to a visit, in two passes, and never guessed.

`Consultation notes dated 18/05/2026` carries a date, and the date is the useful part — it identifies the visit the clinic has to look up. Measured read-only on 2026-07-28:

| Where | What holds 18 May 2026 |
|---|---|
| `vet_claims.invoice_data` | claim **#6** — Kings Vet, invoice **1000229**, $351.50, items include *"Consultation - Standard (Mon - Fri)" $96.50*; invoice PDF on disk; claim settled at `DC1-27-5628` Sr 6 |
| `email_extractions` | the Kings Vet bulk-history email (`19f7c8412410fadd`) — all eleven of Aari's invoices, 1000229 among them |

The letter is on claim **#8** (`DC1-26-5992` Sr 1, 2 April charge, Raised ALT). So Petcover is assessing an April claim using a **later** visit's notes for the same condition. Nothing about that is inferable from claim #8 alone, which is why the resolution is worth doing rather than leaving Justin to work out which visit "18/05/2026" was.

Two passes, most authoritative first:

1. **Claims** — an invoice date, or (once captured) a line-item date, equal to the requested date. Returns the claim id, merchant, invoice number and amount.
2. **`email_extractions`** — the same match over cached extractions, for a visit no claim covers (real: the threads predating the bank CSV coverage). Returns the invoice's own details with no claim id.

No match → the label and nudge name the document and the date, and say the visit is unknown. Never a nearest-date fallback: an adjacent visit is a different consultation, and sending a clinic after the wrong one wastes the only chase Justin gets.

Deliberately **not** attaching anything. Consultation notes are clinical records held by the practice; the resolved invoice is context that makes the chase answerable, and using it as a substitute attachment would be answering a different question than Petcover asked.

### 4. Line-item dates are captured, and the cache cost is paid once.

Justin's premise — an invoice's header date and the dates of the treatments on it differ — is right and currently unrepresentable. Of 41 stored line items, **zero** carry a date; the schema keeps `description` and `amount` only. A multi-visit invoice therefore matches only on its header date, which is precisely the case that misses.

So the extraction schema gains an optional per-item `date`, null when the document doesn't state one. `email_extractions` caches successful extractions forever (and the standing rule is to invalidate when what extraction must return changes), so the 14 cached rows are cleared and re-extracted — 14 real-email extractions against the daily token budget (ADR-0017), the only token spend in this change. Done as one deliberate step with the count stated, not as a silent side effect of the first tick after deploy.

Alternative: leave the schema alone and match only header dates. Cheaper today, and it fails the exact case he raised — the one where a consult on the 18th sits on an invoice dated the 30th.

### 5. "Unanswered" = an unresolved vet-owed information request, and the number reported is days-to-deadline.

The Monday job selects claims where the latest unresolved `info_requested` event has `owed_by = "vet"`, using the same unresolved determination the dashboard's needs-action list uses (latest `info_requested`/`suspended` event with no later confirm-resolved — ADR-0008). Past-deadline requests are excluded: they are history, not an action, and their home is the register.

Each line carries claim id, pet, clinic name and email, the requested document, days since the request, and days remaining to the treatment-anchored deadline. Nothing outstanding → no message at all; a weekly "nothing to do" is how a person learns to ignore a channel.

### 6. Monday, at the existing nudge hour, as a second cron job.

`scheduler.add_job(nudge_unanswered_vet_requests, "cron", day_of_week=config.VET_NUDGE_DAY, hour=config.ACTION_NUDGE_HOUR, id="vet-request-nudge", coalesce=True, misfire_grace_time=3600)`. Same `coalesce` + grace as the daily job, for the same reason: a machine asleep at the fire time otherwise skips the run entirely, and a skipped weekly beat is a week of silence rather than a day's.

Reusing `ACTION_NUDGE_HOUR` rather than adding a second hour knob: one fewer thing to keep aligned, and both messages want the same "morning" definition. `VET_NUDGE_DAY` defaults to `mon` and exists so the day can move without a code change.

Alternative: fold it into the daily job and filter by weekday. Rejected — two jobs with different scopes and cadences are easier to read, and the daily one's message ("N actions still waiting, oldest is #X") is deliberately a summary, not a per-item list.

### 7. Existing events keep a null document.

Events 10 and 31 predate the extraction. Their source emails are still in Gmail, so a read-only re-run can backfill `requested_document` the way the 2026-07-28 audit backfilled `owed_by` — but it is not required for correctness, and a live write is a deliberate, backed-up, container-side operation (ADR-0018). Listed as a task with that framing, not assumed.

## Risks / Trade-offs

- **The template phrasing changes and the document stops extracting** → the label and the nudge fall back to the generic wording, which is exactly today's behaviour; nothing breaks silently, and the null is visible in the event detail.
- **`_short_document` doesn't recognize a new document kind** → generic label, full phrase still visible in the nudge and the card. Adding a kind is one map entry, from a real letter.
- **A vet answers and Petcover stays silent** → the claim keeps appearing every Monday until a later Petcover letter moves it or Justin confirms it resolved. That is the same unobservability ADR-0020 accepts; the confirm-resolved tap is the escape hatch and it already exists.
- **Two nudges land the same Monday morning** (daily stale-action + weekly vet) → tolerable, and the vet one is the narrower list; if it grates, the daily job can skip the kinds the weekly one covers. Not built now.
- **Re-extraction after the cache clear costs tokens on a shared daily budget** → 14 emails, counted and stated; run as one deliberate step, and the fallback chain (ADR-0017) covers exhaustion by switching models. A failed extraction isn't cached, so a partial run resumes.
- **A resolved invoice gets mistaken for the requested document** → the wording says "notes for the 18 May 2026 visit — invoice 1000229", never "notes attached"; nothing is attached and the spec says so.
- **The requested date matches two visits** (same date, two invoices — real for a two-pet charge) → report both rather than picking; the clinic can tell them apart and we cannot.
- **Past-deadline exclusion hides a claim Justin might still want to fight** → it is not dropped, only demoted out of the action beat; the register (its own change) lists it explicitly with no buttons.

## Migration Plan

1. Ship code + tests; no DDL, no data migration.
2. Deploy from the `deploy` worktree with `./scripts/deploy.ps1`.
3. Verify live, read-only: claim #8's label reads `Vet: consult notes needed`, and `nudge_unanswered_vet_requests` invoked by hand produces one line for #8 naming Kings Vet, `info@kingsvet.com.au`, the document, and the days remaining.
4. Optionally backfill `requested_document` on events 10 and 31, container-side, backup first, diff reviewed.

Rollback is a revert; the only persisted addition is an extra key in an existing JSON bag.

## Open Questions

**Task 0.4 of `vet-info-request-chase` — assigning a letter to a claim.** Justin asked what information exists to decide the shape. Measured read-only, 2026-07-28:

| Stray event | What it cites | Candidate claims | How many |
|---|---|---|---|
| 10 — `Petcover claim for--Aari--DC1-27-5628 Serial Number: 2` | `DC1-27-5628` **Sr 2** | claim #21 holds exactly that reference and serial | **1 — no choice to make** |
| 31 — `Petcover claim for Ari DC1-27-5628 Sr.8` | `DC1-27-5628` **Sr 8** | no claim holds Sr 8; Aari's un-referenced sent claims are #1, #2, #12, and the letter's clinic (`admin@theshirevet.com.au`) rules out #12 (MediPaws) | **2** |
| 30 — `PetCover - Acknowledgement Letter` | `DC1-26-5993` Sr 1 | no claim holds that thread; same three un-referenced Aari claims, and an acknowledgement names no clinic | **3** |

So the worst real case is **three** candidates, and the facts that separate them are the ones a card row already shows: date, merchant, amount, condition. A Telegram card with one button per candidate is sufficient — no search, no dropdown, no paging. Event 10 needs no UI at all: it failed only because `find_claim_by_reference_and_sr` refuses terminal claims, and an information request that arrived *before* a settlement is history worth recording against the settled claim rather than an action.

Two questions left for Justin, neither blocking this change:

- Should an exact `(reference, Sr)` hit attach to a **settled** claim as history (recording it, taking no action), or stay unlinked? Recording it looks right — it is a true fact about that claim — but it edges on ADR-0011's rule that a terminal claim is never reopened, so it needs saying explicitly.
**Elaborating the second question — does the assignment card need a "none of these" button?**

The case is not hypothetical. Petcover's mail reaches back further than our data does:

- `bank_transactions` starts **2025-07-17** (first NetBank CSV). Every claim exists because a charge exists, so no claim can ever be created for a visit before that date.
- Real letters that name threads with no possible claim: `GABR-0305` ×2 (Feb 2025), `GABR-0306` (Feb 2025), `DC1-26-4751` ×2 (Mar 2025), and `DC1-27-5631 SR1` (Jan 2026 — a request that got no reply and the claim was never submitted at all).
- Live example already in the log: **event 30**, an acknowledgement citing `DC1-26-5993` Sr 1. No claim holds that thread and, on the dates, none can.

Without a "none of these" option the card presents three buttons, all of them wrong, and offers no way to say so. Three consequences follow, and this is the part worth deciding rather than discovering:

1. **A wrong tap is worse than no tap.** Attaching an acknowledgement to an unrelated claim teaches that claim a reference from another Condition Thread — exactly the `DC1` failure the audit just repaired, where a mis-learned reference then routed a second letter onto the wrong claim and was self-sealing (the claim stops being an un-referenced candidate, so correlation can never reconsider it). `detach_reference` exists as the undo, but the point of the button is to not need it.
2. **The card will keep coming back.** An unassignable event stays in the manual-link queue; whatever surfaces these will re-offer it every time until something clears it. A dismiss path is what stops a permanent, un-actionable card — the same reason `dismiss_mismatch` exists for settlement differences.
3. **The event is still worth keeping.** These letters are the two-year history `vet-info-request-register` is being built for, where a row with `claim_id = NULL` is normal and expected, not a failure. So "none of these" means *record that no claim applies* — not delete, not hide.

Three shapes, if it is built:

- **"No claim on file"** — marks the event permanently unlinked, with a reason, and stops it being re-offered. Cheapest, and it matches the register's `claim_id = NULL` model.
- **"Not now"** — defers, and the card returns next week. Only useful when a claim genuinely might appear later, which for a pre-2025-07-17 visit it cannot.
- **Nothing** — leave it to the register's own list and never offer these on a card at all. Defensible: if no tap can be right, don't ask.

Recommendation: build **"No claim on file"**, because the alternative is a card Justin has to ignore repeatedly, and ignoring cards is the habit this whole change exists to avoid. It is one callback and one event, and it wants a spec line stating that an unlinked-on-purpose event is never re-offered.
