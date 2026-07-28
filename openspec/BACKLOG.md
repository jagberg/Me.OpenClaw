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

### ADR-0018's read-only rule was broken again — build the enforcement it deferred
*Recurred 2026-07-27. Capability: `claims-pipeline-resilience`. Escalates ADR-0018 Alternative 4, which was left "unbuilt, and worth building if this recurs".*

ADR-0018 requires every host-side connection to the live DB to be `file:…?mode=ro`, because a plain read-write `connect()` checkpoints and deletes `openclaw.db-wal`/`-shm` on close and took the container down for good on 2026-07-25. On 2026-07-27 an agent (this one) opened the live DB read-write from the host **four times** in one session — two investigation scripts, plus the backup and restore around a repair trial — despite the rule being in `CLAUDE.md` and the ADR being read later in the same session.

No outage resulted this time; verified from inside the container afterwards (`journal_mode = wal`, event count and claim state intact). That is luck, not compliance, and it is the second occurrence of the exact habit ADR-0018 says convention cannot prevent: *"Nothing prevents the next plain `connect()`."*

Two things to build, neither designed here:
- The `scripts/query_db.py` read-only helper from ADR-0018 Alternative 4 — noting the ADR's own objection that an ad-hoc one-liner bypasses it, so a helper alone is insufficient.
- A mechanical guard, since the failure mode is an agent writing `sqlite3.connect(<live path>)` inline. Candidates: a hook that rejects a Bash/PowerShell command containing the live DB path without `mode=ro`, or moving repair operations inside the container entirely (`docker exec`), which is where a *write* belongs regardless — ADR-0018 covers reads and says nothing about deliberate host-side writes, which is a gap in the rule as written.

### Should action titles and status labels be the same words?
*Open since 2026-07-28. Capability: `claim-status-vocabulary`.*

`claim_status._ACTION_META` titles ("Set condition", "Assign pet", "Define claim process") and `status_labels` chips ("Needs condition", "Needs pet", "Blocked: no claim process") read the same determination but answer different questions — "what do I do" vs "where is this claim" — so ADR-0021 kept them separate. `status_labels.needs()` already reuses the `_ACTION_META` titles for `/basic`, so two of the three vocabularies are joined; whether the chip should follow is a judgement call worth revisiting if they drift.

### `_action_kind`'s extraction was never verified per kind
*Open since 2026-07-28 (found by a trail audit the same day). Capability: `claim-status-vocabulary`.*

`clarify-claim-status-vocabulary` task 1.2 claimed a before/after assertion for every action kind and was ticked without one. Measured: 4 of 9 kinds are asserted anywhere (`blocked_insurer`, `set_condition`, `assign_pet`, `mark_sent`, `dismiss_mismatch`). **Zero** exist for `split_proposal`, `unmatch`, `confirm_resolved`, `invoice_request_sent` — and `unmatch`/`confirm_resolved` are the two the extraction actually moved, since their precedence is preserved by a guard added when the set-membership checks stayed in `_action_kind`. Both suites pass and the refactor is believed mechanical, but that pair is untested. The fix is one assertion per kind, not more label tests. Corrected in the archived tasks.md rather than left standing.

### ADR-0018's read-only rule held on 2026-07-28 — one data point, still no enforcement
*Evidence for the entry above. Capability: `claims-pipeline-resilience`.*

Every host-side DB read this session used `file:…?mode=ro`, and the live repair ran `docker exec` inside the container with a backup and a reviewed dry-run diff. So the convention was followed unaided the session after it was broken four times — which is worth recording, and is *not* evidence that convention is sufficient. The mechanical guard the previous entry asks for is still unbuilt, and the rule still says nothing about deliberate host-side *writes* (the gap that made "run it in the container" a judgement call rather than a rule).

### The host resolves the app's DB path to a stale phantom DB
*Found 2026-07-28. Capability: `claims-pipeline-resilience`. Sharpens the ADR-0018 entries above.*

`app/.env` sets `DATABASE_PATH=/data/openclaw.db` for the container, and `config` loads `.env` from cwd, so **any host-side `db.get_connection()` opens `C:\data\openclaw.db`** — a file created 2026-07-22, last written 2026-07-25, containing 2 stale `vet_claims`, 2 stale `bank_transactions` and no `telegram_messages` table at all.

This is worse than the failure ADR-0018 guards against. A read-write open of the *live* DB breaks loudly (the container loses the WAL sidecars). This breaks *quietly*: the query succeeds, returns rows, and the rows are wrong. It surfaced only because `find_visit_by_date` returned `[]` for `2026-05-18`, a date the live DB certainly holds — had the phantom contained a plausible row instead of none, the wrong answer would have been reported as fact.

Two things worth deciding, neither designed here:
- Should the phantom be deleted? It is not referenced by anything, but deleting it converts silent-wrong-answers into a loud "unable to open database file", which is the better failure. Left in place for now because nothing verified what else may have written to it.
- The correction recorded earlier today — "ADR-0018's read-only rule held on 2026-07-28" — needs the caveat that a read-write open *was* attempted from the host that day. It hit the phantom rather than the live file, so no harm resulted, and that was path resolution rather than discipline.

### The model fallback chain cannot survive a provider-level block
*Found 2026-07-28 during a live outage. Capability: `llm-backend`. Extends ADR-0017.*

Groq began returning `403 {"error":{"message":"Access denied. Please check your network settings."}}` to every request at 12:39 UTC; the last successful call was 05:34 UTC. Probed from inside the container with **and without** the API key — both 403, so this is IP/network-level denial by Groq, not the key, not a quota, and not the request shape.

ADR-0017's fallback walks four models — `llama-3.3-70b-versatile`, `openai/gpt-oss-120b`, `openai/gpt-oss-20b`, `llama-3.1-8b-instant` — and **all four are Groq**. The chain is designed for per-model daily budget exhaustion (TPD), so a provider-level rejection defeats every link, and every LLM path in the app (invoice extraction, the chat agent) fails together. `llm.py` correctly classifies the 403 as transient and retries with backoff, which is right for a blip and useless for a block.

Gemini credentials already exist and are already used for vision OCR (ADR-0010), so a cross-provider fallback is available without new accounts or new spend: on a provider-level failure — 403, or repeated connection refusal, as distinct from a 429 — fall through to Gemini for text extraction and chat rather than failing the tick. Worth an ADR since it changes what "fallback" means in 0017.

Not resolved here: **why** Groq is blocking this egress. That is a network/account question for Justin (VPN, ISP address reputation, or a region block), and no code change fixes it.

### Re-extracting the cached invoices to pick up line-item dates is still owed
*Blocked since 2026-07-28. Capability: `invoice-matching`. Task 3.4 of `vet-request-document-and-monday-nudge`.*

The extraction prompts now request a per-line-item date, and the 14 cached extractions must be re-run to populate it (approved token spend). The attempt on 2026-07-28 failed all 14 on the Groq 403 above. **No data changed and no tokens were consumed** — the script replaces a row only when the fresh extraction is non-empty, precisely so a vision-sourced row is never overwritten with nothing, and a backup was taken first (`/data/openclaw.db.bak-pre-item-dates-20260728`).

Re-run `reextract_item_dates.py --apply` inside the container once the provider is reachable. Until then, line-item date matching is implemented and unit-tested but **cannot fire on real data**: `find_visit_by_date` matches invoice header dates only, which is exactly the case Justin raised (a consult on the 18th billed on an invoice dated the 30th).

### Which date anchors a claim — the deadline says treatment, the excess math still says the charge
*Open since 2026-07-29. Capabilities: `settlement-validation`, `dashboard-visit-ledger`. Follows the ADR-0020 correction of the same date.*

`claim_status.treatment_date` now anchors the submission deadline on the date the pet was treated, because Petcover's letter says *"within one year of your pet receiving treatment"* and the charge is a different date by an unbounded amount (live: treated 19 Jun / 30 Jun 2026, both charged 06/07/2026 — 17 and 6 days of slack that anchoring on the charge silently granted).

`claim_status._policy_year_key` still derives the excess and annual-cap policy year from the **transaction** date. Same question, applied to money instead of a deadline. Whether that is right is **not recorded anywhere and is not inferable from the code** — the excess design (ADR-0011/0013) predates this distinction being noticed, so treating the charge date as the anchor there may have been deliberate or may simply never have been asked.

Left alone on purpose: changing it moves expected-reimbursement figures and settlement-mismatch flags, and it compounds the *already recorded, still undecided* disagreement about closed policy years in `openspec/specs/dashboard-visit-ledger/spec.md` ("Known inconsistency — closed policy years"). Two open questions about the same anchor should be decided together, by Justin, not resolved one at a time by whoever is editing.

Concrete case to reason about when it is decided: Aari's policy anniversary is 09-23, so a treatment on 19 Jun charged 06/07 sits in the same policy year either way. A treatment in mid-September charged in early October would not.
