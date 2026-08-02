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

## Doctor reports two CRITICALs that do not block startup (found 2026-08-01)

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
