# Backlog

Work that is genuinely open, pulled out of changes that were archived because they **shipped**. Without this file these items would disappear: an archived change isn't a tracker, and leaving a change unarchived to hold two stragglers is what left `openspec/specs/` stubbed for months.

Each entry says where it came from, so its original reasoning is still reachable.

## Blocked on Justin

### Echo / Bow Wow Insurance — the claim process itself
*From `vet-claim-automation` task 6.0. Capability: `vet-payment-detection`, `claim-form-automation`.*

Bow Wow's template format, submission method (email vs portal) and required fields are all unknown until Justin asks them. Until then Echo's claims stop at `matched` with a "process not yet defined" flag — deliberately, rather than guessing a process.

**Six claims, ~$6.6k**, of which two account for ~$5.4k. This is the largest outstanding number in the system and no code change can clear it.

## Decisions needed

### Should there be a `/link` verb for unlinked Petcover letters?
*Moved out of `HANDOFF.md` 2026-08-08 when that file was deleted; open since 2026-08-06. Capability: `telegram-bot`, `condition-thread-tracking`.*

Unlinked letters are visible — `unlinked_letters()` surfaces them as `/actions` cards — but they are **not tappable**: the cards carry `actionable: False`, so linking one to a claim is a dashboard-only act.

The reason it was not just added: a command button is deterministic *only while its command is registered*, and an unregistered verb is not an error — it reaches the agent as an ordinary chat turn and spends tokens. So adding the button and adding the verb are one change, not two, and the 58-byte command-button budget applies.

Six such rows exist live (10, 30, 31, 88, 91, 92). Note 91 is the one whose linking to claim #2 is **rejected** on separate grounds — see "One invoice, two Condition Threads" — so a `/link` verb must not imply that every unlinked letter should be linked.

### Should the compose project name be corrected?
*Moved out of `HANDOFF.md` 2026-08-08. Open since 2026-08-06. Cosmetic, with a non-cosmetic failure mode.*

The project name is `meopenclaw-telegram-claimquery`, taken from a stale feature-branch directory name — so every container and volume is prefixed with a branch that no longer means anything.

**Renaming naively orphans three volumes**, and `gateway_state` (31 MB: pairing identity, agent sessions, plugin registry, cron state) is **not regenerable**. Losing it reads to the user as "the gateway forgot everything" — a re-pair, lost sessions, and cron declarations to re-seed.

Safe route, if it is done at all: `name:` in compose **plus** explicit `volumes: {<vol>: {name: <existing>}}` pinning each existing volume. The SQLite DB is unaffected either way — `/data` is a bind, not a volume.

Worth being honest that the benefit is tidiness only. The cost of getting it wrong is a visible outage.

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

**Resolved 2026-08-07 — there is no egress block today, and the probe was the problem.** Measured with the clients the code actually uses, not with `urllib`:

| client | container | result |
|---|---|---|
| `openai` SDK (httpx 0.28.1), default UA | app | **200**, real completion from `llama-3.3-70b-versatile` |
| Node `fetch`, default UA | gateway | **200**, real completion |
| `urllib`, default UA | app | **403 `error code: 1010`** |
| `urllib`, UA `curl/8.0` or `Mozilla/5.0` | app | **401 Invalid API Key** — i.e. it reached Groq |

`1010` is a Cloudflare User-Agent ban. Every "Groq is blocked" probe on record was `urllib`-shaped, so it measured a UA filter that neither runtime is subject to. The one-line check this entry flagged as "cheap to check first, ten minutes, unrelated to the provider choice" was the whole answer.

Two things this does **not** claim. It does not prove 2026-08-04's block was the same artefact — the body recorded then (`Access denied. Please check your network settings.`) is a different string from `1010`, so a real block that has since lifted is equally consistent. And it changes no config: `LLM_PROVIDER` is still `gemini` and the gateway agent is still `gemini/gemini-2.5-flash`, because moving a primary provider is a behaviour change and Justin's call.

**What it unblocks.** The cross-provider fallback this entry asks for is now buildable and no longer needs a new account: Gemini's free-tier daily exhaustion — the thing that spent the quota on 2026-08-06 and made typed chat answer "API rate limit reached" — has somewhere to fall through to. ADR-0017's chain being four links of one provider is still the defect; it now has a second provider to reach for. Still worth an ADR, since it changes what "fallback" means in 0017.

### Which date anchors a claim — the deadline says treatment, the excess math still says the charge
*Open since 2026-07-29. Capabilities: `settlement-validation`, `dashboard-visit-ledger`. Follows the ADR-0020 correction of the same date.*

`claim_status.treatment_date` now anchors the submission deadline on the date the pet was treated, because Petcover's letter says *"within one year of your pet receiving treatment"* and the charge is a different date by an unbounded amount (live: treated 19 Jun / 30 Jun 2026, both charged 06/07/2026 — 17 and 6 days of slack that anchoring on the charge silently granted).

`claim_status._policy_year_key` still derives the excess and annual-cap policy year from the **transaction** date. Same question, applied to money instead of a deadline. Whether that is right is **not recorded anywhere and is not inferable from the code** — the excess design (ADR-0011/0013) predates this distinction being noticed, so treating the charge date as the anchor there may have been deliberate or may simply never have been asked.

Left alone on purpose: changing it moves expected-reimbursement figures and settlement-mismatch flags, and it compounds the *already recorded, still undecided* disagreement about closed policy years in `openspec/specs/dashboard-visit-ledger/spec.md` ("Known inconsistency — closed policy years"). Two open questions about the same anchor should be decided together, by Justin, not resolved one at a time by whoever is editing.

Concrete case to reason about when it is decided: Aari's policy anniversary is 09-23, so a treatment on 19 Jun charged 06/07 sits in the same policy year either way. A treatment in mid-September charged in early October would not.

### A legal transition applied to the wrong claim has no demonstrated guard
*Open since 2026-07-30. Capability: `claim-state-machine`. Found by the `eval-change` run on `claim-state-from-event-log`; ADR-0020's Decision 1 stays open on it.*

The transition table refuses `settled`→`acknowledged`, which is what walked claims #6 and #7 backwards on 2026-07-27. It does **not** refuse the other two moves from that incident, and it should not: #22's `sent`→`below_excess` and #18's `below_excess`→`acknowledged` are ordinary forward moves, because `below_excess` is non-terminal by decision (the invoice is retained). What was wrong with those two was that the event reached the wrong claim.

The state machine cannot be their guard, and nothing else has been shown to be one. `_already_recorded` is the obvious candidate, but ADR-0020 records that event-level idempotency was tried against the real DB for this exact incident and **did not help** — the problem was never duplicate events. Reference/Sr routing precedence is the other candidate and has never been tested against a replay of misrouted mail.

So: a re-read whose event is a *legal* move applied to the *wrong* claim would still be applied today, silently and legitimately as far as the machine is concerned. `tasks.md` 1.3 briefly asserted idempotency covered this; that assertion was withdrawn on 2026-07-30 rather than left standing.

What would close it: a test that replays real misrouted mail against a claim whose state legally accepts the event, and demonstrates which mechanism rejects it — or, if none does, a decision that Phase 2's `revert_state` is the intended remedy and this is an undo problem rather than a prevention one.

### The 19→0 shadow measurement could not discriminate, so the machine is still unproven live
*Open since 2026-07-30. Capability: `claim-state-machine`. Gates task 6.4 and Phase 2.*

`state_projection_disagreements` read 19 before the backfill and 0 after, and both numbers are real — but neither tested the state machine. Strip the `state_backfilled` events from the live log and re-fold: **all 22 claims project `pending_match`**, because no claim's log holds the `matched`/`drafted`/`sent` events that would advance a fold. A projection that ignored every real event outright produces the identical 19. And the 0 is arithmetically forced: the backfill copies `vet_claims.status` into `detail["status"]`, and the fold reads it back exempt from `TRANSITIONS` — both sides of the comparison are the same column.

Nothing live has yet exercised the transition table, event ordering, or the illegal-event skip. Only the seed and the copy-back have. `apply_event`'s `BACKFILL_EVENT` branch has never run at all, because the script raw-`INSERT`s.

This does not mean the machine is wrong — the unit tests cover it, and claims #2 and #8 folded `backfill`→`approved`→`settled` correctly on 2026-07-30T08:45, which is the first live fold that could have disagreed. It means 6.4's week of zero is a much weaker signal than it reads as, and should be judged on **claims that transition during the week**, not on the count staying at zero.


### Should the 👍 acknowledgement survive the gateway's native typing indicator?
*Open since 2026-08-01. Capability: `telegram-bot`. Raised by Justin while spiking OpenClaw; deferred deliberately, not forgotten.*

The 👍 reaction exists for one reason: a slow handler (an LLM turn) made the bot feel dead, so every inbound message is reacted to before its handler runs. Seeing the gateway live, Justin's own words: *"I liked that I could see the bot change to show typing … that's what I was after by the thumbs up, but this is better because it shows its working in the background."*

The native indicator is strictly more informative — receipt **plus** work-in-progress, and it clears by itself when the reply lands. The 👍 only ever signalled receipt.

**Decided for now: keep the ack.** It is cheap, it is already specified (`telegram-bot`: "Every incoming message is acknowledged immediately"), and removing it during a transport swap would confuse a real regression with an intended change.

What would close it: after cutover, confirm the typing indicator fires on the paths that matter — an LLM chat turn, and a tap whose handler is slow — then decide whether the reaction is redundant. If it is, this removes a requirement and its code rather than adding any. Note the two are not equivalent for **taps**: a button press may show no typing indicator at all, which is exactly the case the ack was added for.

## Reminders should push, now that a push channel exists (opened 2026-08-01)

ADR-0003 chose dashboard-only reminder delivery because no push channel existed.
That reason expired — Telegram has been live for months, and the gateway swap
(ADR-0024) brings cron and multi-channel push with it.

Deliberately **not** folded into `openclaw-gateway-core` (Justin, 2026-08-01):
independent of the transport swap, and near-trivial once the gateway is in
place, so including it would add scope without reducing work.

What a change here would need to settle: whether a due reminder pushes
unconditionally or only when unacknowledged; whether it reuses the claims
notification path or gets its own; and what happens to reminders that came due
while the gateway was down (gateway cron runs missed jobs at startup, capped at
five per restart — see tasks 11.4).

## The baseline references a capability that does not exist (found 2026-08-01)

`openspec/specs/task-capture/spec.md` points at a capability named
`task-telegram-surface` — "Full detail in the `task-telegram-surface`
capability." There is no such capability among the 18 in `openspec/specs/`.
The only other reference is in `openspec/changes/telegram-agent-reach/`, an
unarchived change.

**Not fixed in `openclaw-gateway-core`** (Justin, 2026-08-01). It is pre-existing
baseline rot created by another change's incomplete sync, and pulling it into the
gateway diff would be tidying someone else's unfinished work into unrelated
scope.

Two possibilities, and whoever picks this up should check which before acting:
`telegram-agent-reach` may be about to create the capability, in which case this
is an ordering problem between two open changes and resolves itself on archive.
Or that change stalled and the reference is permanently dangling, in which case
the fix is to create the capability or drop the sentence.

Worth doing before the next archive either way — a baseline that references
missing capabilities gets less trustworthy each time it is read and believed.

## ~~Doctor reports two CRITICALs that do not block startup~~ CLOSED 2026-08-08 (found 2026-08-01)

**Re-run on 2026.7.1: zero CRITICALs.** The session-store one is gone — self-healing, as this entry suspected. The skills half persists but has *changed shape*: it is now a count (`Eligible: 13 / Missing requirements: 32`), not a CRITICAL.

**Keep the warning below — it is stronger evidence now than when written.** A condition that silently changed severity across one version bump is exactly why `doctor` must not be promoted from repair tool to health gate. `config validate` remains the authoritative pre-boot check.

Original entry follows.

Slice 1's task 13.6, moved here at archive on 2026-08-02 rather than into
`openclaw-telegram-cutover`, because it is true of the gateway as it runs today
and has nothing to do with holding the bot token.

`openclaw doctor` on a first run reports `CRITICAL: Session store dir missing
(~/.openclaw/agents/main/sessions)` and 32 skills with missing requirements.
Neither blocked startup, and the gateway has since served turns, so at least the
first is either self-healing or misclassified.

**Why it matters that this stays open.** `scripts/gateway_seed.sh` already calls
`oc doctor --fix` as a repair step for an invalid config volume. Nobody should
promote `doctor` from a repair tool to a health gate — the obvious next step —
until these two are understood, because a gate that fails on a condition the
system tolerates gets skipped, and a skipped gate is worse than no gate.

Slice 1 deliberately used `config validate` as the authoritative pre-boot check
instead, since it exits non-zero on warnings and is the same validation the
gateway runs at startup.

## Route the LLM backend by purpose, and get vision off the unpaid Gemini quota (2026-08-02)

Design settled in **ADR-0026** (status: *proposed*, not accepted). Survey and the
numbers behind it: `docs/research/2026-08-02-free-llm-providers.md`.

**Why it is open rather than done.** The routing shape is agreed; the vision
provider is not, and picking it needs two things nobody has tested:

1. Whether this account can obtain a **Cerebras** key. Its free tier was sold out
   for this account on 2026-07-23 and has not been retried. Cerebras is the only
   surveyed candidate that answers vision *and* the cross-provider gap in one
   move, so this gates the recommendation.
2. Whether any candidate's **OCR holds up against a real scanned invoice** from
   the corpus — not a benchmark. Wrong OCR means a wrong amount on a claim sent
   to an insurer. This is the one LLM purpose where a quality failure costs money
   rather than patience.

**Why it should not wait indefinitely.** Verified from Google's own terms on
2026-08-02: the unpaid Gemini quota is used to "provide, improve, and develop"
its models, "human reviewers may read, annotate, and process your API input and
output", and it says "Do not submit sensitive, confidential" data. Every scanned
vet invoice `extract_vision()` handles goes there, carrying name, address, pet
names and itemised amounts. That is a boundary problem and it is live now.

**The work, once the provider is chosen** (~40 lines, no call-site changes —
`purpose` is already an argument on all five call sites):

- `_PURPOSE_PROVIDER` map consulted by `_resolve()`, defaulting to
  `config.LLM_PROVIDER` for any unmapped purpose.
- `_client` module global becomes a dict keyed by base URL. **This is the
  load-bearing change** — it is what currently makes a cross-provider chain
  impossible, and it is why simply adding a second provider to config does not
  fix the eval's single-point finding (axis 5c).
- `_FALLBACK_MODELS` entries become `(provider, model)`.
- Per-provider 429 classification. `_is_daily_budget_exhausted` matches
  `"tokens per day"`/`"(tpd)"`, which is Groq-shaped; any other provider's body
  produces a chain that never walks and never says so.
- `_last_model_used` stops being a module global — with routing it goes from
  stale to wrong, and it is how Justin learns he got a fallback.

**Related, and cheap to check first:** one agent observed Groq returning 403 to
python-urllib's default User-Agent and 200 with any UA set. Not reproduced
independently. If it holds it may explain the unexplained `403 Access denied` at
`llm.py:139-142`. Ten minutes, and it is unrelated to the provider choice.

## Split `mcp_server.py` into transport / protocol / tool-registry layers (PR #4 review, 2026-08-02)

Justin's review comments on PR #4, `app/openclaw/mcp_server.py:126` and `:225` —
*"This class is doing too much work"* and *"The mcp layer should be in its own
file, following SOLID"*.

**The concern is right; the wording needs one correction before anyone acts on
it.** There is no class in the file, and the MCP layer *is* already its own file.
What is actually mixed inside those 244 lines is three layers:

1. **HTTP transport** — `mcp_endpoint`, the `POST`/`GET` routes, the auth guard,
   batch handling, status codes.
2. **JSON-RPC / MCP protocol** — `dispatch`, `_error`, `_result`, the method
   table, protocol-version negotiation, notification handling.
3. **Tool registry and invocation** — `TOOLS`, `TOOL_NAMES`, `_impls`,
   `_call_tool`, `turn_context`.

Splitting those three is the real request.

**Deferred deliberately, and the timing is the argument.** Slice 2's section 3
adds `propose_*` tools and the confirm gate to this exact file (tasks 3.1–3.7).
That is roughly a 50% growth in a 244-line module, and it lands squarely in
layer 3. Refactoring the layering now means doing it twice, or doing it against
a shape that is about to change. **Do the split as the first task of section 3**,
when the mutation surface arrives and the structure finally earns its keep.

**Two constraints that must survive the split**, both currently resting on the
file being one unit:

- `test_mcp_inventory_has_no_dangerous_tool` and the tool-count budget assertion
  read `TOOLS` / `TOOL_NAMES`. The inventory is the `gmail-isolation-boundary`
  enforcement surface, so whichever module ends up owning it must stay the single
  enumerated place — no dynamic registration, no second registry.
- `_impls()` selects from `agent._build_impls` **by name from `TOOL_NAMES`**,
  which is the only reason the `propose_*` functions in that same dict are
  unreachable today. Once section 3 makes proposals legitimate, that filter stops
  being the boundary and the split must not quietly drop it before its
  replacement exists.

**Already done from the same review** (commit on `feature/integration`): the raw
`SELECT name FROM pets ORDER BY name` inside `turn_context` — the "data queries"
half of the comment — collapsed into `db.list_pet_names()`. It had been written
out four times.

**Not in scope here, and much larger:** raw SQL is spread across the whole
codebase — `claim_status.py` 44 sites, `claim_forms.py` 31, `invoice_matching.py`
30. A general data-access layer is a different proposal and would need its own
change; do not let it ride in on this one.

### ADR-0026 parked, and the privacy exposure that stays open (2026-08-03)

Justin's call: leave ADR-0026 (LLM routes by purpose) at **proposed**. The
routing shape is settled; the vision provider is not, and it is gated on two
untested things in `docs/research/2026-08-02-free-llm-providers.md` — whether
this account can obtain a Cerebras key, and whether any candidate's OCR holds up
against a real scanned invoice from the corpus.

**What parking accepts, stated plainly so it is not rediscovered as a surprise:**
`extract_vision()` sends scanned vet invoices — name, address, pet names,
itemised amounts — to Google's unpaid Gemini quota. Its terms, retrieved
2026-08-02, say Google uses submitted content "to provide, improve, and develop"
its models, that "human reviewers may read, annotate, and process your API input
and output", and "Do not submit sensitive, confidential" data. Verified the same
day: **Groq serves no vision model at all** (15 models, no `scout`, no
`maverick`, no `-vl`), so there is no in-provider alternative to move to.

Nothing is broken today and nothing is being done wrong by accident. This is a
known exposure with a deliberate decision behind it, and the decision was to
carry it until slice 2 ships rather than take on an unproven OCR path in the
middle of a cutover — wrong OCR means a wrong claim amount sent to an insurer,
which is the one purpose where a quality failure costs money rather than time.

Revisit when: slice 2 is archived, or a Cerebras key becomes obtainable, or a
vision-capable free tier appears whose terms do not permit human review.

### ~~The app cannot reach the gateway to send anything~~ CLOSED 2026-08-08 (found live 2026-08-03)

**Both proposed options shipped, and the entry is kept for the reasoning.** Verified 2026-08-08:

- **Option 2 won as the live path.** `gateway_client.py:231` posts to `OPENCLAW_GATEWAY_HTTP_URL + /api/v1/claims/send`, the plugin's own in-process route inside the gateway — so the token stays in one place and no protocol is re-derived.
- **Option 1 shipped as the fallback.** `app/Dockerfile` multi-stage-copies the `openclaw` CLI out of the gateway image, with a comment naming this failure. Both containers' CLIs are 2026.7.1.
- The preflight gap is closed too — see the note at the end of this entry.

Original reasoning below, unchanged.

**The cutover was rolled back on this.** First real `/actions` after the cutover
returned, correctly and visibly:

```
{"status":"partial","route":"command/actions","correlation_id":"tg-actions-n1",
 "result":"⚠️ 6 card(s) did not send: gateway CLI not found at 'openclaw'"}
```

`gateway_client` shells out to the `openclaw` CLI. **The CLI lives in the
gateway container; the app container has no such binary and never did.** Slice 1
merged `gateway_client` explicitly "with no caller", and every flag in it was
verified against `openclaw message --help` *on the host*. Nothing ever ran it
from where it actually has to run. Task 4.4 gave it its first caller, and the
first real tap found this — which is the point of 4.11, but it means all
outbound is broken under the cutover, not just cards.

**What is already known, read from the gateway's shipped code:**

- The app *can* reach the gateway over HTTP: `http://gateway:18789/health` from
  inside the app container returns `{"ok":true,"status":"live"}`.
- The CLI does not use a plain REST endpoint. `openclaw message send` calls
  `callMessageGateway({gateway:{url,token,…}, method:"send", params:{to,
  message, mediaUrl,…}})`, which goes through `callGatewayLeastPrivilege` →
  `callGatewayWithScopes` — per-method operator scopes, idempotency keys, device
  auth. Reimplementing that wire format in Python is re-deriving a proprietary
  protocol, and this project's rules exist because of exactly that class of
  guess.
- The plugin api has **no** general send capability (`api-builder` exposes
  `registerChannel` and `sendSessionAttachment`, nothing else outbound), so the
  plugin cannot simply be asked to send on the app's behalf.
- `OPENCLAW_GATEWAY_TOKEN` is given to the gateway by compose and **not** to the
  app.

**Options, none of them free:**

1. **Put the CLI in the app image.** The product-supported client, so no
   protocol is re-derived. Costs a Node runtime in a Python image and hands the
   app a gateway token — widening a boundary ADR-0024 drew narrowly on purpose.
2. **A plugin HTTP route.** `registerPluginHttpRoute` exists in the SDK. The app
   POSTs to a route inside the gateway and the plugin sends. Keeps the token in
   one place, but the plugin needs a send capability its `api` does not expose —
   unverified how it would get one.
3. **Return cards from the command handler.** Avoids app-initiated sends for the
   command path only. Does nothing for the pipeline's unattended notifications,
   which are most of the outbound traffic.

**What the preflight missed, and should not have.** It asserts eleven things and
never once asserts *the app can actually send a message*. A check that pushed a
real message through `gateway_client` at deploy time would have caught this
before a human tapped anything. That is the gap worth closing regardless of
which option wins.

**Closed — verified 2026-08-07.** `gateway_preflight.check_app_can_send` exists
and is wired into `main()`. It is also a better check than this entry asked for:
it does **not** push a real message (that would train Justin to ignore the
preflight), and it does not use `message send --dry-run` (which answers
`handledBy: "core"` without contacting the gateway, so it passes against a wrong
URL, a wrong token or an unpaired device — all three of the failures this deploy
actually hit). It sends **write-scoped** to target `0`, so the connect, the
pairing and the write scope are all exercised and Telegram refuses the dummy
target. The function's own docstring names the three weaker probes that were
tried live and passed while a real send failed. Left here rather than deleted so
the reasoning stays reachable; the gap itself is gone.

## From petcover-settlement-reconciliation (2026-08-04)

- ~~Should the dashboard's expected-reimbursement estimate net the 35% age
  contribution?~~ **Decided 2026-08-04 by Justin: yes.** 65% is the policy's own
  benefit rate, not a percentage inferred from the letters, so the
  "no fabricated deduction" rule it seemed to contradict never applied. The
  ledger now nets `config.PETCOVER_BENEFIT_RATE` after the excess and before the
  cap. Echo (no Petcover figures on file) is untouched. See the change's
  `dashboard-visit-ledger` delta and design.md Decision 3's reversal note.
  Remaining open piece: whether the rate should ever be per-pet — that needs a
  column, which on the live DB means a hand-run `ALTER TABLE`, so it waits for a
  second insured pet.
- **The closed-policy-year disagreement, open since 2026-07-25**: the dashboard
  drains the $150 excess for closed years and settlement validation does not.
  Worth settling in the same conversation as the age contribution — two open
  disagreements about the same numbers is one too many.
- **Five `approved` events lack `age_contribution_stated`** (ids 18, 21, 22, 54,
  55) because the extraction pattern shipped after they were written. Today's
  code reads it from those same five emails, but `_already_recorded` blocks a
  re-read from backfilling and that is correct (ADR-0020): re-reading mail is not
  a repair tool. The gap is permanent in the log and Check A skips those events
  rather than checking them against a term they never captured.
- **Recover the five lost approval letters.** `poll_petcover_status(reread=True,
  since=2026-07-24)`. A live write, so Justin's call, and it must run *after* the
  settlement fix deploys or the recovered letters are flagged by the formula that
  fix replaces. `process_reply` skips already-logged (email, claim, event)
  triples, so it records only what is new and cannot resurrect claim #2's
  dismissal. Sequenced after the serial-map correction below — see that entry.
- **Confirm the estimate after deploy.** Claim #8's card read
  `Expected payment: $296.50` before the 65% rate landed and should read
  `$192.72` after. Read-only check; the whole point of the change is that this
  figure now matches what Petcover pays.
### One invoice, two Condition Threads — the model cannot represent it
*Found 2026-08-06 while closing out `serial-assignment-by-evidence`. Capability: `condition-thread-tracking`.*

Claim #2's $580.74 invoice was assessed by Petcover as **two** claims: $445.74 under `DC1-27-5628` Sr 8 (arthritis, paid $289.73, with $135.00 marked non-claimable) and the remaining **$135.00** under `DC1-26-5992` Sr 4 (the ALT thread, paid $87.75). The Blood Profile line moved threads.

The arithmetic closes exactly: **$289.73 + $87.75 = $377.48**, which is $580.74 × 0.65 and is precisely the figure their 29/07 status table projected for Sr 8 *before* the split. So nothing was refused — the whole invoice was allowed, across two threads.

A `vet_claims` row holds **one** `petcover_reference` and one `petcover_sr`, so this cannot be recorded. Consequences to expect, not to fix by accident:

- **Claim #2 under-reports what Petcover paid by $87.75.** Any reconciliation reading it sees $289.73 against a $580.74 invoice and infers a shortfall that does not exist.
- **Event 91 (the `DC1-26-5992` Sr 4 approval) stays unlinked.** Linking it to #2 was considered and rejected: `_latest_settlement_detail` takes the most recent event carrying figures, so #2 would then report its settlement as **$87.75**, which is worse than reporting $289.73.
- Deciding this properly means either a claim owning several (reference, sr) pairs, or a split-claim row per thread. Both are schema changes on a live DB.

**Correction, 2026-08-06 — unlinked no longer means invisible.** This entry
originally read "stays unlinked **forever**" and drew the conclusion that the
$87.75 is therefore lost from view. The first half stands; the conclusion was
wrong, and `723c516` ("a Petcover letter with no claim is now visible") fixed it
the same day. `claim_status.unlinked_letters()` reads every
`claim_status_events` row with `claim_id IS NULL` and `pending_actions` appends
them, so event 91 now appears in `/actions` and on the dashboard as an
`unlinked_letter` card carrying its own event id. Six such rows existed live —
10, 30, 31, 88, **91**, 92 — accumulated since 2026-07-21 and read by nothing.

**Why the original reasoning is still the right reasoning.** Surfacing a letter
and attributing its money to a claim are different acts, and only the second was
rejected here. Nothing above changes: **claim #2 still under-reports what
Petcover paid by $87.75**, and the schema decision is still the only real fix.

Caveat that survives the fix: event 91 predates `process_reply` storing
`reference`/`sr` on unmatched rows, so both are NULL on it (verified live
2026-08-06) and its card cannot name the letter — it reads as an `approved`
event with amounts and no thread. Only rows written after `723c516` carry them.

### Four claims have no recoverable claimable subtotal
*Found 2026-08-06. Capability: `claimable-subtotal-provenance`.*

Claims **#16, #18, #19, #21** have an invoice total and **zero line items** stored, so the subtotal cannot be recomputed from anything we hold — `invoice_matching.claimable_amount` needs items. Fixing them means re-extracting the source invoice, which spends LLM/vision budget and rewrites data on three settled claims and one below-excess claim for display purposes only.

Claim #2 *was* fixable and is done: it had its four line items, and the app's own rule returns $580.74 with nothing non-claimable.

### The serial→treatment-date map needs a fresh ask, not a feed
*Found 2026-08-06. Capability: `condition-thread-tracking`.*

Petcover's 2026-07-29 table is the only artefact that states a treatment date per serial, and it is a **one-off Justin requested** — nothing delivers it on a schedule. It covers serials up to that date only.

Consequence: where two claims share an amount, the letter alone cannot place the serial and routing correctly refuses (live: the two $35.00 claims and the two $45.00 claims). Resolving a future ambiguity means asking Petcover again. The historical ambiguity is already resolved — the map was corrected by hand on 2026-08-05 against that table.

Worth knowing: **the charge date equalled Petcover's stated treatment date on all nine claims they state one for.** So `treatment_date()`'s "assumed = charge date" fallback, which drives the 12-month submission deadline, is confirmed against real data rather than merely plausible.

- **Correct the live serial→claim map.** Petcover's status table of 2026-07-29
  (`19fab5f3b534416c`) states a treatment date per serial, and against it every
  serial we hold is on the wrong claim (0 for 10), while every letter's stated
  amount matches its true claim's invoice to the cent (7 for 7). The true map is
  written out in the change's post-mortem. Not applied: it rewrites
  money-affecting links on nine claims and is Justin's call. **Sequencing note:**
  the recovery re-read of the five lost approval letters routes by the same
  0-for-10 heuristic, so doing it first attaches real settlements to wrong claims
  — correct the map first, or accept that the new links need fixing too.
  Supersedes the earlier "not correctable from data we hold", which was written
  before that table was read.

## From csv-upload-via-telegram (archived 2026-08-10)

- **`templates/index.html:299` crashes on some dashboard ledger rows.** `d.claimable_subtotal`
  is missing on some claims in `claim_status.dashboard_lists()`'s output — a data-shape gap,
  not touched by csv-upload-via-telegram. Found live while verifying that change's dashboard
  parity (task 8.4), after fixing an unrelated `TemplateResponse` signature bug that was
  masking it. Reproduce: `GET /` against the live DB.
- **`claims-pending-flow`'s condition-entry flow is unverified since the gateway cutover.**
  ADR-0029 found `before_dispatch` hooks silently never invoke a plugin's handler in gateway
  2026.7.1; this flow uses the identical mechanism and has not been independently re-checked
  against a real typed condition since. If it's also broken, condition text is falling through
  to the chat agent — the exact outcome the hard rules forbid.
