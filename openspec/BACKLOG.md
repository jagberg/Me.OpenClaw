# Backlog

Work that is genuinely open, pulled out of changes that were archived because they **shipped**. Without this file these items would disappear: an archived change isn't a tracker, and leaving a change unarchived to hold two stragglers is what left `openspec/specs/` stubbed for months.

Each entry says where it came from, so its original reasoning is still reachable.

## Blocked on Justin

### Echo / Bow Wow Insurance — the claim process itself
*From `vet-claim-automation` task 6.0. Capability: `vet-payment-detection`, `claim-form-automation`.*

Bow Wow's template format, submission method (email vs portal) and required fields are all unknown until Justin asks them. Until then Echo's claims stop at `matched` with a "process not yet defined" flag — deliberately, rather than guessing a process.

**Six claims, ~$6.6k**, of which two account for ~$5.4k. This is the largest outstanding number in the system and no code change can clear it.

## Decisions needed

### Do closed policy years drain the excess on the dashboard?
*Found 2026-07-25 during the baseline sync. Capability: `dashboard-visit-ledger` vs `settlement-validation`.*

Two shipped code paths disagree about the same domain fact:

- the dashboard ledger (`_apply_excess_and_cap`) drains the $150 excess for **every** `(condition, policy year)` group, closed years included;
- settlement validation (`_validate_settlement`, per ADR-0013's amendment) treats a claim whose transaction falls in an **already-closed** policy year as having passed the threshold already, because our history for a closed year is presumed incomplete.

So a last-year claim shows an expected reimbursement $150 lower on the dashboard than settlement validation expects for the same claim.

The closed-year default was Justin's explicit instruction for settlement validation. Whether he meant it to govern the dashboard's estimates too **was never asked**. Not resolved either way, because silently changing either path would fabricate a decision.

### What does "redo claim #N" mean?
*Found 2026-07-25 in live Telegram use. Capability: `conversational-agent`, `claim-form-automation`.*

Justin asked the agent twice to redo claim #7 ("This claim needs to be redone as it doesn't exist anymore", then "The #7 claim needs to be redone"). No tool matches, so the agent fell through to `propose_create_task` and saved tasks #124 and #125 — an honest non-action that reads like action. Nothing in the codebase can rebuild an already-`drafted` claim; the only reset path is `invoice_matching.unmatch` (→ `pending_match`, clears `invoice_data`), reachable via ❌ Wrong invoice, which is wrong here because #7's invoice is correct.

Three different operations are all called "redo" and only Justin can say which he wants:

1. **Rebuild the draft** — same `invoice_data`, regenerate the form PDF and Gmail draft, delete the old draft. For "the draft is wrong or missing".
2. **Re-extract the invoice** — discard `invoice_data`, re-read the source PDF. For "the figures are wrong".
3. **Full reset** — back to `pending_match` and re-hunt the email. For "wrong invoice entirely".

Note the premise was also wrong: #7's draft was never gone (verified live — draft `r-7259758204005672288`, correct recipient, subject and three attachments). See the subject-collision entry below for why it looked missing. Tasks #124 and #125 are open and duplicate; close them when this lands.

**Answering this now also decides something else.** `submission-group-id` ships a submission id *derived* from its claim ids (`S6+7`), which is stable only because nothing can re-split a drafted batch. If the redo semantics chosen here can re-group drafted claims, that token stops being stable and has to become a stored `submission_id` column (manual live `ALTER TABLE` + backfill). Weighed as the rejected alternative in that change's `design.md` — check it before answering.

### No alert exists for "the DB is unreachable", and none can as built
*Found 2026-07-25 during the outage in ADR-0018. Capability: `claims-pipeline-resilience`. See the ADR-0015 amendment.*

`pipeline._alert_rate_limited` reads the `ops_alerts` ledger from the DB before sending, so it raises during a DB outage instead of alerting. Every other push alert routes through it. ERROR-level logs were emitted correctly every tick and reached nobody, and `polling_alive()` reported `true` truthfully while every inbound update died at `message_log.record_inbound`'s write. Justin discovered it by pressing a button and getting silence — the symptom ADR-0014/0015 exist to eliminate.

The repair needs a decision first: **where does the rate-limit state live when the ledger is unreachable?** An in-memory counter loses the restart-can't-re-spam property that `ops_alerts` was chosen for. Alternatives include a file-backed counter under `/data`, an unbounded-but-narrow exception for this one kind, or an external `/health` poller (which would have caught it, since `/health` itself fails without the DB — ADR-0015's Alternative 3 rejected a Docker healthcheck for not restarting anything, which is not an argument against polling).

## Deferred features

### Claim-draft subjects don't name their claims
*Found 2026-07-25 in live Telegram use. Capability: `claim-form-automation`.*

`claim_forms.py:483` and `:643` both build `subject=f"Vet claim — {pet['name']}"`, so every submission for the same pet is indistinguishable in Gmail, and `pipeline.DRAFT_SEARCH_LINK` searches on exactly that subject. On 2026-07-25 two drafts titled `Vet claim — Aari` coexisted — #7+#6 batched, and #12 — and Justin concluded #7's draft had been deleted. It had not.

Fix is one line in each place: include the claim ids, e.g. `Vet claim — Aari (#7, #6)`. Not built because it lands with the redo decision above.

Checked before deferring: this does **not** affect reply correlation. `claim_status.classify` and `extract_reference` run against *Petcover's* reply subject (their own wording — `claim_status.py:22` records the real one as "Petcover Insurance Claim for Ari"), never against the subject we send. Nothing matches on our draft subject except `pipeline.DRAFT_SEARCH_LINK`, which is the thing being fixed.

### Dashboard view of open split/merge proposals
*From `fix-email-matching-gaps` tasks 7.5 and 9.6 — deferred at the time with "at some stage".*

Merge proposals and inadequate-invoice items are actionable from Telegram but have no dashboard list. Not blocking anything; Telegram covers the actual workflow.

### Assistant-side reminders don't push
*Found 2026-07-25 during the baseline sync. Capability: `reminder-scheduling`.*

ADR-0003 chose dashboard-only reminders with no push, deliberately. That deferral was later lifted for the *claims* side (Telegram), but a task reminder coming due is still only visible if Justin opens the dashboard. Whether it should now push was never asked — a gap, not a decision.

### Claim #17 vision-OCR retry never resumed
*Ongoing operational item, no owning change.*

Claim #17's vision-OCR has attempted once in six days despite two attempts remaining and the source email still being found by the live query (maxResults truncation ruled out). Root cause unidentified; needs a live trace of `match_claim`'s vision branch rather than a guess.

### Claim #21 figure discrepancy
*Ongoing operational item. Capability: `settlement-validation`.*

We extracted a claimable of $44.75; Petcover's approval letter states $35.00 claimed and $22.75 paid. The mismatch is flagged and visible via `claim_detail`, but which figure is wrong — our extraction or their assessment — has not been investigated.

### ADR-0012 successor: derive the continuation box from Condition Thread existence
*Recorded in ADR-0012 as future work.*

The continuation box is currently hard-defaulted to ticked. Now that Condition Threads are modelled (ADR-0011), it could be derived. Unbuilt.

### No undo for a confirmed per-pet split
*Deferred by ADR-0019, 2026-07-27. Capability: `multi-pet-invoice-split`.*

A confirmed split inserts a sibling claim and rewrites both rows' claimable shares. Guarded (pre-`sent` only, every resulting claim named in the confirmation) but not reversible: a wrong split leaves a stray claim that nothing removes. Whether undo should merge the shares back or just close the sibling was never decided.

### Does the ASSIGN PET card need an explicit "Shared invoice" button?
*Deferred by ADR-0019, 2026-07-27. Capability: `telegram-bot`.*

Splitting is discoverable only through the card's one-line hint that a reply works. A button that prompts for amounts would be explicit, at the cost of another multi-step tap flow. Deferred until the reply path has been used live at least once.

### Bow Wow Insurance claim process still undefined
*Ongoing operational item. Capability: `claim-form-automation`.*

Echo has no insurer claim email or process on file (`pets.claim_process_defined = 0`), so 6 claims — plus Echo's share of every per-pet split, starting with claim #1's $372.56 — sit blocked with no button that can clear them. Needs Bow Wow's actual claim process from Justin, not code.

### The manual per-pet split has never run live
*Open since 2026-07-27. Capability: `multi-pet-invoice-split`.*

`claim_forms.split_between_pets` and the chat proposal that drives it are tested but unexercised against a real bill: the charge that prompted them turned out to be two invoices, handled by automatic apportionment instead. The path needs one genuine single-document, two-patient invoice (the vet's bulk history email bills Aari and Echo on one document, so the shape exists) before it can be called verified.
