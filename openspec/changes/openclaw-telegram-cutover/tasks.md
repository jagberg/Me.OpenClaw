# Tasks — the Telegram cutover

Carved out of `openclaw-gateway-core` on 2026-08-02 by that change's task 8.14,
after slice 1 deployed. **Every task below moved verbatim.** Nothing was
rewritten, re-numbered or re-worded in the move, so a reader who follows a
cross-reference from slice 1 lands on the same text it pointed at. That is also
why the numbering is not contiguous: sections 3–6, 9, 10 and 12 keep their
original numbers, and the stragglers keep theirs.

**Read slice 1 first.** The decision trail — D1–D12, ADR-0023/0024/0025, the
eight silent-failure modes, the token measurements, the 58-byte command budget —
lives in `openspec/changes/archive/*-openclaw-gateway-core/`. This change
references it and does not restate it, because a copied decision trail diverges.

**The boundary rule that put these tasks here** (slice 1, task 8.11): a task is
slice 2 if it is only true once the gateway holds the bot token. Not "is it
hard" or "is it related". Slice 1 archived by syncing its requirements into
`openspec/specs/`, so the test was whether each requirement is *true* after that
slice ships.

**What is already built and waiting.** `gateway_client.py` is merged with no
caller. `app/gateway-plugin/index.js` registers the five commands and its
`claimCommandMenu` no-ops for want of a token. `docker-compose.yml` carries
`TELEGRAM_BOT_TOKEN: "${TELEGRAM_BOT_TOKEN:-}"`, empty. Supplying that token
**is** the cutover, and `scripts/gateway_preflight.py` fails the deploy if both
runtimes poll or neither does.

**Two tokens need revoking before any of this runs** — both were exposed in
plaintext during slice 1's build (once by running `docker compose config`). The
live bot's replacement goes in the deploy worktree's `app/.env`.

## 3. MCP server — proposals and the confirm gate

- [x] 3.0 **ANSWERED — Justin chose one commit path, 2026-08-02, recorded as ADR-0027.** The finding that forced the question, kept as written: ADR-0025's decision was that a chat-initiated confirm commits *in the MCP surface*. Verified 2026-08-02 against the gateway's shipped code: **there is no mechanism for it.** An MCP server gates its own commit by asking the client mid-call — MCP calls that *elicitation* — and the gateway's MCP client is constructed with no `capabilities`, so it declares `{}` on `initialize`; the bundled SDK then throws `Client does not support elicitation capability` for any such handler, and the string `elicit` appears nowhere in the gateway's agent MCP runtime. Approvals in that product are a **plugin** capability (`plugin-sdk/approval-runtime.js`), and this MCP server is not a plugin — that is precisely the step ADR-0025's Context got right and its Decision carried one surface too far. Full evidence and the forced consequence are in ADR-0025's 2026-08-02 amendment. **The option Justin chose does not exist; the option he declined (one Python commit path) is the only feasible one, so the outcome is forced — but the reason he chose as he did is what gets lost, and accepting that is his call.** Not confirmed by a live refused elicitation: that needs an agent turn and Groq's daily budget was exhausted the same day. Worth doing before the cutover; it would only confirm.
- [x] 3.1 **DONE 2026-08-02.** Add `propose_*` tools for the existing mutations (mark sent, set condition, assign pet, mark resolved, split between pets). Each records a pending action and returns a confirmation; none commits. **Needs a durable proposal store either way** — the MCP call and the tap are separate requests in separate runtimes, so the per-turn `proposals` list `telegram_bot` passes around today does not survive the gap. A new table is fine (`CREATE TABLE IF NOT EXISTS` only fails to help on *existing* tables).
- [x] 3.2 **DONE 2026-08-02.** Move the commit path so it is reachable only from the confirm callback, never as a tool return value. On the feasible shape: a chat proposal returns text plus a confirm button, the tap goes `command` button → plugin → `/internal` → the same commit function a card tap reaches. Two knock-ons: the proposal's identity must fit the **58 UTF-8 byte** command-button budget, and `confirm` becomes a sixth entry in both `BUTTON_COMMANDS` and the plugin's `COMMANDS` — now asserted equal by `test_the_plugin_registers_exactly_the_commands_a_button_may_emit`.
- [x] 3.3 **DONE 2026-08-02** — `test_the_mcp_surface_refuses_a_single_pet_assignment_when_the_message_names_two`, asserted with the verbatim live message. The text comes from `db.latest_inbound_text()`, not a tool argument: a model that paraphrased it would paraphrase the second pet name away and take the refusal with it. Port the two-pets-named refusal into the MCP server and assert it with the live 2026-07-27 message text ("This is actually split between echo and Aari. Aari cost was $35 out of this").
- [x] 3.4 **DONE 2026-08-02** — `test_the_mcp_surface_refuses_a_split_with_no_amounts_rather_than_writing_zero_rows`; the refusal queues nothing and sends no button, so it is a true no-op. Port the no-per-item-amounts split refusal; assert no $0 rows are produced.
- [x] 3.5 **DONE 2026-08-02** — `test_every_mutating_tool_takes_an_explicit_claim_id`. Assert every mutating tool accepts an explicit claim id, and that current-claim-from-reply is supplied to the turn.
- [ ] 3.6 Set the agent's tool-iteration cap explicitly in config; verify reaching it yields a best answer with visible truncation rather than a silent stop.

  **Not possible as written — there is no such config key (checked 2026-08-04).** The task was written while `llm.chat`'s own `max_iterations=4` was the bound; the gateway runs the loop now, and its config schema has no equivalent. Every candidate in the shipped bundle is something else:

  | Symbol | What it actually bounds |
  |---|---|
  | `MAX_RUN_LOOP_ITERATIONS` | model **retries/failover** attempts (`resolveMaxRunRetryIterations`), not tool rounds |
  | `maxToolCalls` | code-mode headless scripts and cron trigger scripts only |
  | `maxTurns` | agent-to-agent ping-pong, xAI search, Claude Code ACP passthrough |
  | `MAX_TOOL_FAILURES = 8` | how many failures a compaction summary lists |
  | `tools.codeMode.maxPendingToolCalls` | code mode again |

  Enumerated from `agents.defaults.*` (≈175 keys) and `tools.*`; nothing caps the chat loop's tool rounds. What does bound a long turn is the context budget plus compaction, and those degrade rather than stop.

  So the task needs re-scoping rather than doing: either accept the product's bound (and record that "visible truncation" is not available), or ask upstream. **Left open deliberately, not silently** — the original wording would otherwise read as an unfinished chore rather than a wrong premise. Practical exposure today is low: twelve read tools over a single-user claim history, and no observed long-loop turn.
- [x] 3.7 **DONE 2026-08-02** — `test_a_proposal_writes_a_pending_row_and_changes_no_claim_data` snapshots `vet_claims` around the call and compares, rather than reading the model's sentence. Verify a proposal reported as done by the model has in fact changed nothing in the DB.

## 4. Telegram cutover

**Deployed and verified live 2026-08-02 (`e06cf94+deploy`), pre-cutover.** The
non-cutover half — 4.1, 4.4, 4.5, 4.8, and 4.9's failure-visibility half — is
built, deployed and running. What the deploy actually proved, as distinct from
what the suite proved:

- Both containers up; `/health` returns `polling_alive: true` and
  `state_projection_disagreements: 0`.
- **All eleven button commands registered inside the gateway**, reported by the
  plugin and confirmed by the preflight's own log read: `PASS button commands
  registered — 11 reported, no collisions`. Five became eleven because every
  action-card tap needs a registered verb.
- `pending_proposals` exists in the live DB with its ten columns, created by
  `CREATE TABLE IF NOT EXISTS` at startup — no manual DDL, because that
  constraint only binds *existing* tables.
- Preflight: 8 PASS, 1 FAIL, 1 SKIP. The FAIL is `model serves a turn`, a Groq
  daily-budget exhaustion (`Limit 100000, Used 96708` earlier the same day), and
  the SKIP depends on it. **A quota result, not a deploy failure** — do not read
  it as one.

**Second deploy, 2026-08-03 (`d91ef44+deploy`): the preflight passes in full — 10 of 10, nothing skipped.** Groq's budget had rolled over, so the turn check ran: `llama-3.3-70b-versatile answered`, and `turn size under ceiling — 5,395 tokens; toolSchemaChars=2359, workspaceFileChars=6665, skillChars=0`. Twelve button commands registered, no collisions. `pending_flows` created at startup alongside `pending_proposals`.

Still needing the token: 4.6, 4.7 and 4.10–4.13. 4.2 and 4.3 also need
0.10a — how a plugin reaches `before_dispatch`, which exists but whose plugin-side
API is unestablished.


- [x] 4.1 **DONE 2026-08-02** — `config.TELEGRAM_UPDATER_ENABLED`, default on; `notify.using_gateway()` is its inverse and the one place the transports diverge. Put the Python updater behind a config flag, defaulting on, so it can be disabled without deleting code. **DECIDED: the flag stays for one week of real daily use after cutover** (Justin, 2026-08-01), then section 6 removes it. Rollback is one env var and a restart, ~30s. A week is what it takes for the failures only real use finds — a caption that will not edit, buttons that will not attach to a card, a tap that quietly reached the LLM.
- [~] 4.2 **PARTLY DONE, and the tick was wrong until 2026-08-04.** What exists: the plugin registers `before_dispatch` via `api.registerHook` and forwards to `/internal/telegram/claim`; it holds no claims logic and fails open, so a failed check sends the message to the agent rather than losing it. Observed claiming a live message — Justin's typed sentence on 2026-08-04 reached the agent and was acked.

  **What does NOT exist, though this task's own sentence names it: nothing forwards inbound messages, edits or callback queries to `/internal/telegram/event`.** The endpoint is built and unit-tested; the plugin never calls it. Found by reading the log after Justin's live run (2026-08-04 08:43–09:12): six commands and taps, and **zero inbound rows** in `telegram_messages` — the last sixteen rows are all `direction=out`.

  Consequences, none of them cosmetic:
  - ADR-0014's audit trail has no inbound half since the cutover. "Did my tap register?" is answerable only from the app log, which is what ADR-0014 exists to stop relying on.
  - The replay queue is permanently empty, so **4.12 cannot be verified at all** — nothing is ever queued to replay.
  - The RL dataset lost its input side; outbound rows have no matching inbound.

  **Why it is not a one-line fix.** A tap and a slash command never enter `dispatch-from-config`, which is what emits `message_received` (established the same session while chasing the ack), so a hook-based tee would miss exactly the events that matter. The plugin's command handler *can* post the tee itself — but a plugin command ctx carries **no message id and no update id** (enumerated from `commands-CDhgE9eG.js`), and `record_inbound_raw` dedupes on `update_id UNIQUE`. So a faithful row is not available from that path; the choice is between a row with a NULL/synthetic id (audit trail yes, dedupe and replay no) and leaving the gap.

  Original task text, kept: Write the thin Node event-bridge plugin: forwards inbound messages, edits and callback queries to `/internal/telegram/event`. No claims logic in the plugin.
- [x] 4.3 **DONE 2026-08-03** — `pending_flows.claim_text` is the one decision, called by both transports; the state moved from two module dicts into the `pending_flows` table for the same reason proposals did. Route the pending-free-text-flow check before the agent turn, so condition entry still consumes its reply and the agent never sees it.
- [x] 4.4 **DONE 2026-08-02** — every outbound now goes through `notify.py`, which routes by the flag. `pipeline` no longer calls `telegram_bot.send_*_sync` at all. Port outbound notification sends to `gateway_client`, including batched-claim messages, lifecycle notifications and the daily nudge. Every message keeps its `#id`.
- [x] 4.5 **DONE 2026-08-02** — cards are built once in `commands` (gateway button shape) and rendered by both transports; `telegram_bot._action_keyboard` converts rather than duplicating. Five commands became eleven, because every card button's verb must be registered or its tap reaches the model. Port card delivery: history cards, actions summary, per-item tap-to-resolve cards, PDF review alerts — all with working buttons.
- [x] 4.6 **DONE 2026-08-03** — `notify.append_result`, with the plain-reply fallback and the degradation logged. The caption gap is a CLI limit, not an oversight: no `--caption` flag exists, so an edit runs `editMode: "auto"` and a *successful* caption edit still writes `there is no text in the message to edit` into the gateway's log. Port `_append_result` to the gateway edit action, keeping the caption-vs-text split for documents and photos. Fall back to a reply if caption editing proved unavailable in 0.3, and log the degradation.
- [x] 4.7 **DONE 2026-08-03** — `notify.ack`, returning a bool and never raising, asserted both ways. **Narrower than the PTB version, deliberately:** only the claim path acks, because a `command` handler's context carries no usable message id (16.2 — every correlation id came through with the `x` fallback). A tapped button's feedback is its reply text, which is what it always was. Port the 👍 acknowledgement to the gateway reaction action; verify a reaction failure does not break the handler.
- [x] 4.8 **DONE 2026-08-02** — `commands.is_authorized`, one implementation for both transports, refusing out loud. Keep the authorization check app-side and case-insensitive; verify an event from any other username is rejected even if the gateway delivered it.
- [ ] 4.9 Persist the chat ID app-side from the `/start` event; verify an unattended notification with no registered chat ID logs the gap visibly and sends nothing.
- [x] 4.10 **DONE 2026-08-03, `76001bd+deploy`.** Cut over: disable the updater flag, bind the gateway channel, enable the bridge. Single deploy step.

  **It took two attempts, and the first one is the more useful record.** `TELEGRAM_UPDATER_ENABLED` had been added in 4.1 and wired only into `notify`, so it switched where *outbound* went and did nothing about the poller. The first cutover therefore handed the token to the gateway while the app kept calling `getUpdates`, and Telegram answered `Conflict: terminated by other getUpdates request`. `gateway_preflight` failed the deploy — *"one runtime is down while the other is up"* — which is the guard doing exactly what it exists for. Rolled back in about a minute (root `.env` token emptied, gateway restarted, app polling again), fixed in `76001bd`, re-cut.

  The lesson worth keeping: **a flag that names a behaviour is not the same as a flag that gates it**, and the test that existed only checked the outbound seam. There is now one asserting the poller is never built with the flag off, and that a disabled updater reads as *disabled* (`polling_alive() is None`) rather than *dead*, which is what keeps the watchdog from SIGTERMing the process on a loop.

  **Attempt two also failed, on something bigger, and attempt three is the one that stuck.** The first `/actions` after cutting over answered `⚠️ 6 card(s) did not send: gateway CLI not found at 'openclaw'`. `gateway_client` shells out to `openclaw`; every flag in it had been verified against `--help` **on the host**; nobody had run it from inside the app container, which had no such binary. Rolled back again.

  Fixed by Justin's call (2026-08-03): *"I prefer standard uses of frameworks and tools unless it really makes sense to deviate"*, and size is not a constraint. So the CLI is baked into the app image by multi-stage copy from the gateway's own tag — the client the product documents for this, at a version that cannot drift from the server. Three further things had to be true, none of them guessable from the docs alone:

  - **`ws://` to a DNS name is refused.** The CLI cannot tell that `gateway` resolves to a private address and fails closed: *"SECURITY ERROR: … uses plaintext ws:// to a non-loopback address"*. The literal `172.28.0.20` passes, which is safe because the subnet was already pinned.
  - **Shared-secret auth alone is enough only for a loopback client.** This one connects from the compose subnet, so the gateway required a paired device. One-time `openclaw devices approve`, and the token lives in a volume — without it every rebuild would silently need re-pairing.
  - **Pairing is scoped, and `read` is not `write`.** `openclaw health` connected on `operator.read` while `notify.send_text` still returned False with *"device is asking for more scopes than currently approved"*. A second approval granted the write scope.

  **The preflight check took three attempts too, and that is the lesson worth keeping.** `message send --dry-run` never contacts the gateway (`handledBy: "core"`). `openclaw health` connects but needs only read scope, so it passed against the exact broken state above. Only a **write-scoped** send probe — to target `0`, refused by Telegram, delivered to nobody — exercises transport, pairing and scope together. Two of the three would have shipped a green check over a broken deploy.

  Proven end to end afterwards: `notify.send_text` from inside the app container delivered a real message to Justin's chat.

  Live afterwards: `PASS exactly one Telegram poller — the gateway`, app log `Telegram updater disabled — the gateway owns the channel`, `/health` `polling_alive: null`, gateway `enabled, configured, running, connected, mode:polling, token:env`, twelve commands registered with no collisions. The preflight's only remaining FAIL is Groq's daily budget again — a quota result, not a cutover one.
- [ ] 4.11 Verify live against the real chat: one claim taken from notification through condition tap, pet assign, mark sent, and confirm resolved. Record which claim id was used.

  **The sequence to run** (written 2026-08-04 at Justin's request; nothing here can be faked from this side, because a tap is a Telegram callback and there is no way to originate one). Run it in one sitting so the log window is contiguous, and note the claim id used.

  | # | Do this in Telegram | Expected | What it proves |
  |---|---|---|---|
  | 1 | `/actions` | Summary card first, then one card per outstanding action, all within ~6s | The burst path and the in-gateway send route (not the CLI) |
  | 2 | Tap **Set condition** on a claim that needs one | A force-reply prompt naming the claim | `pending_flows` starts, durable across the three requests |
  | 3 | Type the condition in plain words (e.g. `kennel cough`) | Card comes back with the condition on it | The typed words reach `condition_text` with **no model in between** — the hard rule |
  | 4 | Tap **Assign pet** where the pet is ambiguous, pick one | Card updates with the pet | Pet assignment from a tap, not inference |
  | 5 | Tap **Mark sent** on a submission | One confirmation; claims sharing the `draft_id` all move | Batch semantics survived the cutover |
  | 6 | Tap the same **Mark sent** again | Refused as already confirmed, no second mutation | Single-use taps — Telegram redelivers, and a double mark-sent is a second Petcover submission |
  | 7 | Tap **Confirm resolved** on a needs-action item | Item leaves the action list | Needs-action persists until an explicit confirm (ADR-0008) |
  | 8 | Send a plain sentence (not a command), e.g. `what's outstanding?` | An answer naming claim `#id`s, and a 👍 on your message | The agent runs on Gemini, and the native ack works for typed messages |

  Two things NOT to expect, both established: a 👍 on `/actions` or on any tap (a slash command never enters the ingress path that creates the ack, and a tap has no message to react to — typing is the feedback there), and instant delivery of a multi-card burst (Telegram's own ~1 message/second/chat floor).

  Afterwards, say the claim id and roughly when. I read `telegram_messages`, the app log and the gateway log for that window and record what actually happened — including 4.12 (a mid-handler crash leaves the row unprocessed and startup replays it) and 4.13 (a duplicated delivery commits no duplicate mutation), which are observations on this same run rather than separate exercises.

  **RUN 1 — 2026-08-04 08:43–09:12 (`e92a79d+deploy`), by Justin. Four of six steps; two had nothing to act on.**

  | Step | Result |
  |---|---|
  | `/actions` | 5 cards, one burst, `http.send_cards ms=5686` — all five in the same second (08:43:46), summary card included. The ~1s/message Telegram floor is the whole cost |
  | `/history` | 1 card, `ms=3084`, page 1/2 rendered |
  | Assign pet, claim #4 | `pet` command dispatched in 65ms; `vet_claims.pet_id = 2` written at 09:07:46. Real mutation, correct |
  | Dismiss #2, twice | **This is 4.13, live.** Both taps reached the app (`tg-dismiss-n4` 09:09:15, `tg-dismiss-n5` 09:09:22, both HTTP 200) and the log holds **exactly one** `mismatch_dismissed` event. The second returned `"Claim #2 has no settlement mismatch to review."` — a visible refusal, not a silent no-op |
  | Plain sentence to the agent | Answered, and the native 👍 arrived — the ack works for typed messages, as established |
  | Mark sent / typed condition | **Not exercised: nothing to act on.** No claim is in `drafted` (so no submission to mark), and all seven condition-less claims are Echo's Bow Wow ones, which surface as `blocked_insurer` rather than `set_condition`. The money case (double mark-sent = second Petcover submission) therefore remains unit-tested only — 10.12 |

  Correlation ids appear on every outbound row of the run (`tg-actions-n1`, `tg-history-n2`), which is 10.14 working live one day after being added. What the run also found is that **no inbound row was written at all** — see 4.2.
- [ ] 4.12 Verify a mid-handler crash leaves the row unprocessed and the replay queue re-runs it at startup.

  **Blocked by 4.2, not merely unverified.** Nothing writes an inbound row on the gateway path, so the replay queue is empty by construction and there is no row for a crash to leave unprocessed. The mechanism itself is unit-tested (`test_replay_pending_reruns_only_unprocessed_and_settles_them`); what cannot be shown is that it has anything to act on in production. Do not tick this until 4.2's tee exists.
- [x] 4.13 **DONE 2026-08-04, live.** Justin tapped Dismiss on claim #2 twice. Both taps reached `/internal/command/dismiss` (correlations `tg-dismiss-n4`, `tg-dismiss-n5`, six seconds apart, both HTTP 200) and `claim_status_events` holds exactly one `mismatch_dismissed` row. The second tap answered `"Claim #2 has no settlement mismatch to review."` — visible, and no second mutation. Note the guard that held here is `dismiss_mismatch`'s own precondition check, NOT the log-row dedupe (which never ran, since no inbound row is written — 4.2).

## 5. Scheduling

**Cut over 2026-08-04, `2490ab9`/`35db686+deploy`. Preflight 12 of 12, `owner: gateway cron`, no APScheduler lines in the app log.** Justin's two calls: reminders piggyback the 15-minute tick rather than getting a minute-resolution cron entry, and a late reminder always fires and says how late.

- [x] 5.1 **DONE.** Five entries, not three: `claims.tick` (every 15m), `claims.ingest` (5m), `claims.nudge` (`0 9 * * *`), `claims.vet-nudge` (`0 9 * * 1`), `claims.expire` (`0 9 * * *`) — the three calendar jobs in **Australia/Sydney**, an IANA zone so the gateway handles DST, declared by `scripts/gateway_cron.sh` and asserted by the new `cron entries declared` preflight check.

  **Two of the five had no endpoint at all** — the weekly vet chase and the queue expiry were in-process only, so the cutover as originally written would have stopped both silently. `/internal/vet-nudge` and `/internal/expire-queue` added.

  **A behaviour change, asked for and taken (Justin, 2026-08-04):** APScheduler fired these in the app container's local time, which is UTC, so a "9am" nudge arrived at 7-8pm Sydney. Cron takes an IANA zone, so this is now genuinely 09:00 local. Asserted in `test_core.py` because a revert would be invisible — the expression still reads `0 9 * * *` and still fires daily, just at the wrong end of the day.

  Facts checked against the product rather than assumed: `cron.add` is a gateway RPC, so this cannot live in the pre-boot seed (`config get cron` returns scheduler settings — `enabled`, `retry`, `runLog`, `maxConcurrentRuns` — and no job definitions; the plugin SDK exposes no cron surface). Payload kinds are agent turn / shell command / system event, so a deterministic call is `--command` + curl. `--declaration-key` is the product's own idempotency handle, which is why a redeploy re-asserts the same five rather than adding five more.

  The secret is left as `$CLAIMS_INTERNAL_SECRET` in the payload, not interpolated: argv is persisted in the cron store and echoed by `cron get`, `cron list` and the run log. Verified it still resolves — the app logged `internal ingest starting correlation=int-53e1d403c38c` and answered **200**, which is the guard passing. (An earlier probe hit 404 on a not-yet-deployed route, which proves nothing about the secret — FastAPI routes before it guards.)

- [x] 5.2 **DONE — and it recovered two real reminders that had been silently lost.** `reminders.sweep_due()` runs inside `pipeline.run_once`. On the first live tick it logged `reminder 6 due (overdue by 2d)` and `reminder 12 due (overdue by 2d)`; the DB confirms reminder 12 was due `2026-08-01T12:02` and still `scheduled` on 08-03.

  **The bug it exposes is older than this change.** APScheduler's jobstore is in-memory (deliberately, 2026-07-31), and only the interval/cron jobs are re-registered at startup — `schedule_reminder` runs at *capture* time, so a one-shot `date` job for a pending reminder simply vanished on the next restart. `misfire_grace_time=None` promised restart-safety and could not deliver it, because after a restart there was no job left to misfire. A sweep gets it right by construction: it asks the DB what is due rather than remembering what it meant to do.

- [x] 5.3 **DONE for the gateway and both cases; Python-only is not applicable.** Over 25 minutes after the deploy (which recreated *both* containers) and a `docker compose restart gateway`: exactly **1** `internal tick starting` and **3** `internal ingest starting` at a 5-minute cadence, and zero `apscheduler` lines. An app-only restart cannot double-fire anything now — the app holds no schedule.
- [x] 5.4 **DONE, unit-tested, not yet seen live.** `sweep_due` is idempotent by its `WHERE status = 'scheduled'`, asserted in `test_a_reminder_due_while_the_app_was_down_still_fires_and_says_how_late` (second call marks 0). Deliberately no `fired_at` column — that needs hand-run `ALTER TABLE` on the live DB, and `status` already carries the fact. Not exercised by a genuinely duplicated cron delivery, because none has occurred.
- [x] 5.5 **DONE, verified live.** After `docker compose restart gateway`: 5 jobs, all enabled, next-run times preserved, and the tick then fired on schedule at 23:34. The store is sqlite (`/home/node/.openclaw/state/openclaw.sqlite`), so nothing needs re-registering — `gateway_cron.sh` asserts the declaration rather than creating it.
- [x] 5.6 **DONE, both at deploy time and at runtime.** The `cron entries declared` preflight check fails the deploy if any of the five is missing, disabled, or carries an `agentTurn` payload (which would work, and burn model tokens every tick forever). At runtime `/health.scheduler` reports `owner`, per-job `last_ok_at`/`minutes_since_ok`, and an `overdue` list — seen going from five `never` entries to none as each job first fired. `owner` is read from `SCHEDULER_ENABLED` rather than inferred, because "nothing ran" means a broken app when it is on and a missing cron entry when it is off.

  Still true, and worth naming: `job_runs` records that a route *ran*, not that it did the right thing. A tick that runs and matches nothing looks identical to a healthy one.

## 6. Deletion and dependency cleanup

Do not start until phase 4 has run one full claim lifecycle on real data.

- [ ] 6.1 Delete `agent.py`'s tool loop and `llm.chat()`; confirm `extract()` / `extract_vision()` callers are untouched.
- [ ] 6.2 Delete `scheduler.py` and the updater code path; remove the flag from 4.1.
- [ ] 6.3 Remove `python-telegram-bot` and `apscheduler` from `requirements.txt`; rebuild and confirm the app starts.
- [ ] 6.4 Re-verify structurally that `send()` appears nowhere in `app/openclaw/` and no tool exposes sending.
- [ ] 6.5 Confirm the daily-budget walk still works for extraction after `chat()` is gone.
- [ ] 6.6 Run the full smoke suite; all LLM keys still force-blanked, vision still stubbed.

## 12. Consequences of conversation bypassing the app (design D9)

Found 2026-08-01 while reconciling the specs with D2's correction. Four of these five are the gateway having to do something the app used to; none may be assumed to work.

- [ ] 12.1 **Resolves the D7/D2 collision.** Plugin forwards a *copy* of every inbound message to `/internal` for logging only — a tee, not a bridge. Without it the training dataset Justin kept the table for narrows to callbacks and outbound, which is the half he did not ask for. Assert an agent-handled message still produces a `telegram_messages` row with its raw payload.
- [x] 12.2 **DECIDED (Justin, 2026-08-01): the plugin claims text while a flow is pending.** `_pending_condition` and `_pending_split` are preserved; what he types is stored verbatim with no model between his words and `condition_text` — the field the hard rules forbid inferring. **This makes a currently-unverified capability load-bearing:** the plugin must be able to *conditionally intercept a text message*, not merely claim callbacks. The docs only evidence callback claiming. Note this is a different capability from 12.1's logging tee — a tee copies, this intercepts. See 0.10.
- [x] 0.10 **ANSWERED 2026-08-02 — yes, and 12.2's decision stands.** Read from the gateway's shipped hook runner rather than its prose. Two claiming hooks exist and both run before the model:

  - `inbound_claim` — *"Allows plugins to claim an inbound event before commands/agent dispatch."*
  - `before_dispatch` — *"Allows plugins to inspect or handle a message before model dispatch. First handler returning `{ handled: true }` wins."*

  Both go through `runClaimingHook`, so a handler that claims stops onward dispatch — which is the capability 12.2 needs and the docs only evidenced for callbacks. **`before_dispatch` is the right one of the two**: it runs *after* command routing, so a slash command still works while a condition-entry flow is pending, where `inbound_claim` would swallow it.

  **Two caveats, both real.** This is read off `hook-runner-global-*.js` and `plugin-sdk/hook-runtime.js`, not observed claiming a live message — the same standard of evidence as the elicitation finding in ADR-0025's amendment, and it deserves the same live confirmation before 12.2 is built on it. And `registerInternalHook` was not found exported on the object a plugin's `register(api)` receives; the hook machinery is plainly there, but **how this plugin reaches it is still unknown** and is the next thing to establish. Original text below.

- [x] 0.10a **ANSWERED 2026-08-03 — the path is `api.registerHook`.** The plugin api object carries it (`api-builder-*.js` builds `registerHook: handlers.registerHook ?? noopRegisterHook`), and the registry implements it as `registerHook(events, handler, opts)` where `opts.name` is required and globally unique. Three properties read from `registry-D1_pYg_a.js`:

  - **No allowlist of event names.** Each entry goes straight to `registerInternalHook(event, wrappedHandler)`, so `"before_dispatch"` is accepted as written.
  - **The handler's return value propagates** — the wrapper is `return await handler({...evt, context})` — which is what `runClaimingHook` reads to decide whether the message was claimed. So `{ handled: true }` actually claims.
  - **Two conditions.** `config.hooks.internal.enabled !== false` (this gateway has no `hooks` key at all, so it is enabled by default — checked live), and `registrationCapabilities.capabilityHandlers`, which is true for registration modes `full` / `discovery` / `tool-discovery`. **The plugin's `registerCommand` comes from the same gated block and demonstrably works** — eleven commands registered live on 2026-08-02 — so `registerHook` is available to it by the same token. That is an inference from observed behaviour rather than a separate observation, and it is the one step here still worth confirming with a real hook.

  **Confirmed live 2026-08-03, `d91ef44+deploy`.** `openclaw hooks list` shows `claims-pending-flow — ready — plugin:claims` among 6/6, and the plugin logs an ERROR if `api.registerHook` is missing, which it did not. So the inference above is now an observation: `registerHook` is available to this plugin and the hook is registered. Still unproven is a real message being *claimed* — that needs the token, and `hooks list` reads a registry, which is the same class of evidence the project's rule warns about for `plugins list`.

  Hook names are global: a collision pushes an `error` diagnostic (`hook already registered: <name> (<other plugin>)`) and **returns without registering**. Same silent-ish class as `registerCommand`'s collision, so the name wants a prefix and the registration wants asserting.

- [ ] 0.10b **Gates 12.2.** Confirm a plugin can conditionally claim an inbound *text* message (not just a callback) and prevent it reaching the agent. If it cannot, 12.2's decision is unavailable and Justin must re-choose between the agent-tool and fully-conversational options — both of which put the model between his typing and a hard-rule field. **Raise this before building anything in section 12.**
- [ ] ~~12.2-old~~ Superseded: decide the fate of `_pending_condition` and `_pending_split` (the "Other (type it)" condition entry and the per-item split walk). They need the next typed message routed to the app, which the correction removes. Three options: claim text while a flow is pending (reopens part of 0.8), re-express both as agent tools, or let the agent handle them conversationally and delete the dicts. `_pending_actions` is unaffected — it is a callback. **Justin's call: the third is most native, and least like the interface he has today.**
- [x] 12.3 **DECIDED (Justin, 2026-08-01): keep the 👍 ack for now.** Removing it mid-swap would blur a real regression with an intended change. The native typing indicator is better — it shows work in progress, not just receipt — and replacing the ack with it is logged in `openspec/BACKLOG.md` to revisit after cutover. Caveat recorded there: a **tap** may produce no typing indicator, which is the case the ack was added for.
- [ ] 12.3a Superseded — original: Confirm who sends the 👍 acknowledgement once the app no longer sees inbound messages. If the gateway does not ack automatically, decide between the agent doing it and dropping it. It exists so a slow handler does not feel dead.
- [ ] 12.4 Confirm the gateway delivers **edited** messages to the agent. If it does not, a typed correction vanishes — the exact 2026-07-27 failure, whose fix now sits outside the path.
- [ ] 12.5 Re-scope the app-side authorization requirement to callbacks only, and record that for conversation the gateway's access control *is* the authorization. This promotes 0.5 from a nice-to-have to load-bearing.
- [ ] 12.6 Rewrite the `telegram-bot` and `openclaw-gateway-runtime` spec deltas to match: they currently describe the app receiving all inbound events, which D2 no longer does.

## 9. Logging and observability — parity or better

Baseline to hold: `telegram_messages` (raw payload + `app_version` + `processed_at`), `llm_calls` (per attempt), `ops_alerts` with ADR-0015 levels, `vet_claims.flag` human-readable reasons, `claim_status_events`, `vision_ocr_attempts`, `email_extractions`. Nothing here may get quieter.

- [ ] 9.10 **Added by 0.8.** Prove the interactive handler actually registered: assert at startup that a known callback token is claimed, and treat any `callback_data:` string reaching the agent as an error, never as input. Without this, a plugin that registered nothing looks healthy and silently routes every tap — including `sent:7` — through the LLM. Registering with the wrong API fails silently (confirmed in a working plugin's source), so "it loaded" is not evidence.
- [x] 9.13 **Added and done 2026-08-04, by a failing deploy.** The gateway agent had ONE model and therefore no answer to a spent daily quota. `GenerateRequestsPerDayPerProjectPerModel-FreeTier` for `gemini-2.5-flash` ran out mid-session and the preflight failed `model serves a turn` with the gateway reporting only `FailoverError: API rate limit reached` — no mention of which limit, which is its single `rate_limit` bucket talking.

  Fixed by declaring all four probed models on the provider entry **and** setting `agents.defaults.model.fallbacks`; the first is load-bearing, because the gateway cannot fail over to a model its provider never declares. Verified live on `e92a79d+deploy`: `model serves a turn — gemini-3.6-flash answered`, 12 of 12 PASS. ADR-0017's 2026-08-04 amendment, which also records what stays broken (the gateway still burns transient retries on the exhausted model, because only the provider's `quotaId` distinguishes per-minute from per-day and the gateway never reads it).

  Also fixed the preflight's own message: it named Groq's daily wording and said nothing for Gemini, so the first failure of this kind was misread as per-minute and "wait a minute" was tried twice before the real quota was checked.

- [ ] 9.11 **Added by D8.** Record the measured token cost of one chat turn with the final tool inventory, against the 100k/day/model ceiling. The schema ships on every request, so tool count is a per-turn tax — treat it as a budget with a number, not a matter of taste.
- [x] 9.1 **DONE 2026-08-04.** The id was already minted at the edge and logged; what was missing was the persistence half, and it is now a `telegram_messages.correlation_id` column written by both the inbound and outbound writers. See 10.14 for what the test pins, including the migration onto the live table.
- [x] 9.2 Log every `/internal/*` request: route, outcome, correlation id. Log rejections (bad/missing secret, non-loopback origin) explicitly — a rejected event must not look like an event that never arrived.
- [x] 9.3 Make `gateway_client` failures loud: capture the CLI's exit code and stderr into the logged reason. A failed send writes a human-readable reason and never becomes a silent no-op.
- [ ] 9.4 Keep `telegram_messages` writing on the gateway path with no field lost — raw payload, truthful kind, summary, `app_version`, `processed_at` ordering. Assert an edit event still logs as an edit with its text (the 2026-07-27 empty-`other` regression).
- [ ] 9.5 Locate and document where the gateway records its own LLM calls and chat turns; write down the two-place accounting (`llm_calls` + gateway records) so a token-spend question is answerable. Record retention and whether that store is backed up.
- [ ] 9.6 Log each tick's outcome app-side (claims advanced, flags written, duration) rather than relying on `cron runs` alone; `cron runs` says it fired, not what it did.
- [ ] 9.7 Preserve ADR-0015 alerting levels across the swap, and add a dead-channel alert if 7.7 finds the gateway's supervision quieter than the old restart-on-dead-updater.
- [ ] 9.8 Verify no log line, alert, or error message carries a secret, a bank detail, or `.env` content — the new internal endpoint and the CLI stderr capture are both new places one could leak.
- [ ] 9.12 **Added by 7.7 (slice 2).** Poll the gateway's health from the app and raise an `ops_alert` at ADR-0015's levels when the Telegram channel is not running, or when the health monitor is restarting it repeatedly. The gateway restarts a dead channel by itself — what it does not do is tell anybody outside its own container log, which is destroyed on recreate. Assert the alert fires, not merely that the poll runs.
- [ ] 9.9 Write down the one thing that does get quieter: chat-side LLM calls leaving `llm_calls`. Named in the llm-backend spec; it must also be in the docs, not just the spec.

## 10. Test coverage

All additions go in `app/tests/test_core.py` (assert-based, no pytest) and must stay hermetic: LLM keys force-blanked, vision stubbed, **and runnable with no gateway installed**.

- [x] 10.1 Stub the gateway CLI at a single seam so every send/edit/react path is testable without a daemon; assert the suite passes with the gateway absent.
- [x] 10.2 **DONE — the same assertion as 19a.7, which was ticked while this stayed open** (eval, 2026-08-02). One test, `test_mcp_inventory_has_no_dangerous_tool`, satisfies both; two checkboxes for one assertion is how a reader concludes there are two guards. Kept as a cross-reference rather than deleted, since both section 10 and section 19a are meant to be readable alone. Original: Regression: agent tool inventory contains no filesystem, shell, browser, mailbox-search or secret-returning tool.
- [x] 10.3 **DONE 2026-08-04** — `test_nothing_in_the_app_can_send_mail_and_no_tool_offers_to`. Greps for Gmail's *send call* rather than the word "send": `bot.send_message` is a legitimate Telegram send, and a guard that trips on it gets deleted within a week. Also asserts no tool on the agent's surface is named send/email/mail — a capability cannot be prompted away — and that `drafts().create` still exists, so the guard cannot pass by the Gmail integration having been removed. (Written wrong first: the vacuity check looked in `gmail_client.py`, where drafting does not live.)
- [ ] 10.4 Regression: every outbound claim message carries its `#id` (existing test, re-pointed at the gateway path).
- [ ] 10.5 Regression: a `propose_*` tool commits nothing; only the confirm callback commits. Include the case where the model's text asserts it is already done.
- [ ] 10.6 Regression: the two-pets-named refusal, asserted with the live 2026-07-27 message text.
- [ ] 10.7 Regression: split with no per-item amounts is refused, no $0 rows.
- [x] 10.8 **Already covered** by `test_a_pending_condition_flow_claims_the_next_typed_message_and_then_releases` (written for 4.3/12.2): `claim_text` returns a card while the flow is pending and `None` before and after, which is exactly "consumed, and the agent never sees it". Cross-referenced rather than duplicated.
- [ ] 10.9 Regression: caption-vs-text append on document and photo messages.
- [ ] 10.10 Regression: authorization rejects any other username even when the gateway delivered the event; case-insensitive compare still passes.
- [x] 10.11 Regression: two concurrent `/internal/tick` calls never both enter `pipeline.run_once`.
- [x] 10.12 **DONE 2026-08-04** — `test_a_duplicated_gateway_delivery_commits_exactly_one_mutation`. Picked out of the remaining §10 items ahead of the others (Justin, 2026-08-04) because it is the one with a money consequence: a second mark-sent is a second Petcover submission for one set of invoices.

  Three layers, each asserted through the real function rather than by reading a flag, because the first alone is not enough — the log dedupe covers a *redelivery of the same update*, while two taps on two cards of one batch are two different updates:
  1. `record_inbound_raw` returns None the second time and leaves one row, so the replay queue cannot hold an event twice.
  2. `claim_status.mark_sent` on a sibling of the same `draft_id` refuses (`ok: False`, wording that says "sent" rather than reading as a failure), and the log ends with exactly one `sent` event per claim.
  3. `proposals.commit` on the same proposal twice: the second is refused as already confirmed and writes no second event.

  The replay half of this task was already covered by `test_replay_pending_reruns_only_unprocessed_and_settles_them`.
- [x] 10.13 **DONE 2026-08-04, and it found a live regression rather than confirming one** — `test_extraction_walks_the_model_chain_under_every_provider_including_gemini`. `llm.extract` delegated to `gemini.extract` whenever `LLM_PROVIDER=gemini`; harmless while Gemini was a rollback option, but when Groq's network block made Gemini the default, invoice extraction started pinning ONE model and retrying it three times with backoff — the right answer to a per-minute cap and a useless one to a per-day cap, i.e. the exact failure ADR-0017 exists to prevent, back through the side door. Fixed in `1f58b45`: `extract()` goes through `chat()` for every provider, `gemini.extract` is deleted, and `_is_daily_budget_exhausted` now recognises Gemini's spelling — read off a real 429, whose message says only "you exceeded your current quota" while the useful part sits in `details[].violations[].quotaId` (`GenerateRequestsPerDayPerProjectPerModel-FreeTier`). The test asserts both directions: a per-day violation walks, a per-minute-only one does not.
- [x] 10.14 **DONE 2026-08-04** — `test_a_gateway_delivered_event_and_its_replies_carry_the_same_correlation_id`. Justin asked for the column once it was clear the change was small (`db._migrate_added_columns` already existed for exactly this), so `telegram_messages.correlation_id` is added at startup rather than by hand-run DDL. Threaded through `record_inbound_raw` (from `/internal/telegram/event`) and `record_outbound` (both the CLI and fast-route senders), so a tap and the cards it produced share one id.

  Two properties the test pins that are easy to lose: the migration is asserted against a table created WITHOUT the column and holding a row — the live case, where `CREATE TABLE IF NOT EXISTS` does nothing and a fresh-DB test would pass regardless — and that pre-existing row must read NULL rather than a fabricated id. An unprompted send (the nudge, a claim notification) also carries no id, because there is no inbound event to correlate with and a made-up link is worse than an absent one.
- [x] 10.16 **Added.** Guard test: nothing outside `gateway_client` imports `subprocess` or reads `config.OPENCLAW_CLI`. Converts the one-seam rule from convention into enforcement — the gap the module map rates as only *partial* for `LoggedBot`. Match the USAGE form (`config.OPENCLAW_CLI`), not the bare name: `config.py` defines the setting and a guard that fires on its own definition site gets trained away.
- [ ] 10.15 Run the full suite at the end of every phase, not only at phase 6. Record pass/fail in this file with the actual output on failure.

  **Section 1 run, 2026-08-01: PASS.** 190/190 tests, exit 0, `ALL TESTS PASSED`. 11 new tests, gateway CLI stubbed via an injected `runner` — suite still passes with no gateway installed and no new dependency.
  Two harness facts learned the hard way, both worth knowing before adding tests here:
  - The runner iterates `globals()` inside `if __name__ == "__main__":`, so anything appended **below** that block is never defined when it runs. The suite still prints `ALL TESTS PASSED`. A silent no-op.
  - `_fresh_db()` deliberately does NOT clear `telegram_messages` (it is the RL dataset), so assert message-log counts **relatively**, never against an absolute total.
  - Piping the run through `tail` reports `tail`'s exit code, not Python's. Redirect to a file if you need the real one.

## Carried over from slice 1

Six items that were open when slice 1 archived. Each is here rather than in
`openspec/BACKLOG.md` because each depends on the gateway holding the token —
they are not stragglers, they are slice-2 work that was written early.

Two of them (13.1c, 17.6) carry a `[x]`-worth of verification already; the tick
state is preserved exactly as slice 1 left it rather than reset, so nobody
re-runs a live check that already produced a number.

One slice-1 item did **not** come here: 13.6 (doctor's `CRITICAL: Session store
dir missing` and 32 skills with missing requirements) is operational and true
today, so it went to `openspec/BACKLOG.md` instead.

- [ ] 2.7 **Added by 2.6.** Map the gateway's empty-allowlist error to a human sentence before it reaches the chat. Must not become a fallback that answers the question anyway — the whole value of the current behaviour is that no claim fact can be produced when the source of truth is unreachable.

- [x] 13.1c **RESOLVED AND VERIFIED LIVE — see the body below; the per-chat scope is the answer.** **CONTRADICTION FLAGGED — the option Justin chose does not exist as configuration.** He asked for the app's five commands plus `/status` and `/models`. There is no per-command menu allowlist. `commands.native` is a single boolean for the entire native command surface, and disabling it excludes plugin commands from the catalog too (`bot-native-commands.ts:1056`, `...(nativeEnabled ? pluginCatalog.commands : [])`) — which would break every `command` button, the whole basis of D12. So the achievable choices are **all ~60 commands, or none plus a broken button path.**

  Attempted and measured 2026-08-01: removing all 13 skills dropped the menu from 61 to **60**. Skills were not the source. The 30 enabled plugins are almost all model providers contributing no commands; the bulk is core (`/status`, `/models`, `/cron`, `/agents`, `/memory`, …) and core is exactly what `commands.native` gates as one unit.

  One structural detail that limits the damage, worth not forgetting: menu visibility and callability are **decoupled**. `bot-native-commands.ts:1125` — *"Telegram only limits the setMyCommands payload (menu entries). Keep hidden commands callable by registering handlers for the full catalog."* So commands beyond Telegram's cap still work; they are merely invisible. That is the mechanism a per-command menu allowlist would use if one existed.

  **RESOLVED 2026-08-01 — there is a clean way, and it is Telegram's own, not a workaround.** Justin asked whether the menu could be limited or the app's commands grouped at the top; looking properly answered the first.

  The gateway registers its menu into exactly **two** scopes — `default` and `all_group_chats` (`TELEGRAM_COMMAND_MENU_SCOPES`, `bot-native-command-menu.ts:38`). Its delete loop iterates the same two. Telegram resolves a private chat's menu most-specific-first: **`BotCommandScopeChat` → `BotCommandScopeAllPrivateChats` → `BotCommandScopeDefault`.** The per-chat scope is therefore **unclaimed**, and writing it for Justin's chat id replaces his menu entirely with the app's five commands — without overwriting, racing, or deleting anything the gateway owns. Every one of the other ~60 stays callable, because visibility and callability are decoupled (line 1125).

  This satisfies the guiding principle rather than straining it: it is the layering Telegram documents, used as designed, in a slot the gateway deliberately left free.

  **Where the call belongs:** the in-gateway plugin, which already holds the bot instance. Not `gateway_client` — that would need the Bot API and a second token holder, which D12 forbids. Re-apply on plugin start, since the gateway rewrites its own scopes on restart and a future version could widen `TELEGRAM_COMMAND_MENU_SCOPES` to include ours.

  **VERIFIED LIVE 2026-08-01.** `setMyCommands` with `scope: {type:"chat", chat_id:…}` returned `ok:true`; reading that scope back returned exactly the five; the default scope was untouched at 47 entries; and Justin confirmed his `/` menu now shows five actions. The mechanism works as the Bot API documents it.

  Incidental, and consistent with 18.6's lesson: the gateway logged *"shortening descriptions to keep 60 commands visible"* while the default scope actually holds **47**. The log's count is taken before the text-budget drop. `getMyCommands` is authoritative; the log is not.

  Residual risk to assert in preflight: if a future gateway version adds the chat scope to `TELEGRAM_COMMAND_MENU_SCOPES`, it would overwrite ours on every restart and the menu would silently revert. Assert the scope list is still the two it writes today.

  Ordering, for completeness: the registered list preserves construction order and is never alphabetised (the sort at line 337 only feeds a cache hash). Plugin commands are concatenated **after** core (`bot-native-commands.ts:1056`), so within the default menu the app's commands sit at the bottom *and* are first to be dropped on overflow. Another reason to own the chat scope rather than live in the default one.

- [ ] 13.5 **Half fixed 2026-08-04, and the half that was fixed is not the half the task names.** The premise was a log LEVEL problem. Investigated against the shipped bundle: the drop lines exist and are **info** — `log.info("dropping dm (not allowlisted)")` and `logger.info({chatId, reason: "not-allowed"}, "skipping group message")` in the Telegram ingress spool — so level was never the blocker for the allowlist path. (The observation that started this was almost certainly the *pairing* path, which replies to the sender and logs nothing; `dmPolicy` is `allowlist` here, so that path is not reachable.)

  The real defect was **retention**, and it bit this session: the gateway had two sinks, stdout (`docker compose logs`, destroyed on recreate) and `/tmp/openclaw/openclaw-<date>.log` (inside the container, destroyed with it). Every deploy erased the previous deploy's evidence — which is how the text of a real `job_runs` failure was lost hours after it happened.

  Fixed in the seed: `logging.file` → `/home/node/.openclaw/logs/gateway.log`, inside the state **volume**, and `logging.level` pinned to `info` so a shipped default moving to `warn` cannot silently take the drop lines with it. Verified live — the file exists in the volume, carries `channels/telegram` and `plugins` lines, and survived `docker compose restart gateway`. Asserted by `test_the_gateways_log_is_configured_to_outlive_its_container`.

  **Still open, and it needs a second person:** that a denial by *this* config actually writes a line. Proving it means an un-allowlisted Telegram account messaging the bot, which cannot be originated from here. `logging.redactSensitive` is also deliberately unset — it takes `"off"` or `"tools"`, not the boolean it reads like (the validator rejected `true`), and picking between them without knowing the default would be guessing at a control over whether tool payloads reach the log (9.8).

- [ ] 13.3 Telegram polling runs through an "isolated polling ingress" with a spool at `/home/node/.openclaw/telegram/ingress-spool-default`. Worth understanding before trusting delivery guarantees — it may already provide some of what ADR-0014's replay queue does.

- [ ] 17.6 **MEASURED, with a number, on the first real deploy (2026-08-02): `Limit 100000, Used 96708`.** The gateway's log says `TPD` and `tokens per day` in as many words. My agent measurement turns consumed **97% of the shared Groq daily budget for `llama-3.3-70b-versatile`**, and the deploy's own preflight then failed on it.

  This is the starvation this task predicted, arriving faster than expected and from the direction it named. Consequence for Justin today, stated precisely rather than alarmingly: `llm.py` walks **four** Groq models each with its own 100k/day ceiling (ADR-0017), so `extract()` falls through to model 2 rather than failing — degraded, not broken. The agent is pinned to one model and is simply out until midnight UTC.

  Two things follow. **The gateway needs its own key**, or the agent must be given the same multi-model chain the app has — 11.5 recorded that the gateway has a single `rate_limit` bucket and treats a daily exhaustion as transient, so it will spend three futile retries discovering this every turn. And the **preflight now names it**: "no turn completed" reads as a broken deploy, when the operator's actual response is to wait rather than to fix anything.

  **Overtaken 2026-08-04 by a different failure, and the contention this task tracks is moot for now.** Groq stopped serving this network entirely: `api.groq.com` returns `403 {"message":"Access denied. Please check your network settings."}` to a request carrying **no `Authorization` header at all**, identically from the Windows host, the app container and the gateway container, with a valid key and with a garbage one. Not a budget, not the key, not the account. Every model in ADR-0017's chain is a Groq model, so the primary and all three fallbacks went together — the first live instance of the single-provider gap in `docs/failure-modes.md`.

  Verified live on `82f2d8f+deploy`:
  - **Preflight 11 of 11 PASS, nothing skipped** — the first fully green preflight since `6035250`, four deploys back. `model serves a turn — gemini-2.5-flash answered`; `turn size under ceiling — 4545 tokens (gateway estimate; the provider reported none); toolSchemaChars=2359, workspaceFileChars=6665, skillChars=0`.
  - **The app's own two LLM paths, inside the running container:** `llm.extract(...)` → `'OK'` via the Gemini SDK, and `llm.chat(...)` → `{'text': 'OK', 'model': 'gemini-2.5-flash'}` via the OpenAI-compatible surface. The second is new behaviour, not a re-point: `chat()` used to refuse `LLM_PROVIDER=gemini` outright.
  - **Gemini's fallback chain probed model by model** against a `claims__*`-shaped tool before being written down, exclusions and their reasons recorded in `llm._FALLBACK_MODELS` (one retired with a 404, two out of quota with a 429).
  - **`gmail-isolation-boundary` still PASSES**, with the gateway now holding `GEMINI_API_KEY` — exempted by exact name after checking that the key returns **401** on `gmail.googleapis.com/users/me/profile`, `.../users/me/messages` and `drive/v3/files`.

  What is NOT fixed: nothing fails over *between* providers, so the same class of outage on Gemini has the same manual cure. And the preflight cannot notice a provider becoming unreachable — it asserts the current choice serves a turn, which is a different question.

- [ ] 19a.6 **`#id` on every outbound claim message** across the gateway path, including button labels and command strings. **MOVED TO SLICE 2 (2026-08-02), because the thing it asserts does not exist yet.** The gateway-path outbound composition is section 4 (`notify_claim_states`, card delivery, `_append_result`); until those are ported there is no message for this to inspect, and a test written now would either assert nothing or assert a stub. `test_notify_messages_carry_claim_ids` continues to cover the current transport and must be re-pointed, not replaced, when 4.4/4.5 land.

  Sharper than when it was written, from 2.5: the model **dropped the ids** on its first live turn despite the MCP instructions demanding them. So this splits in two. The tool output and the composed message are code, and are assertable. What the *agent* repeats is not — that is the same lesson as 13.4, and it belongs in the "cannot be asserted" list in `docs/gateway-deploy.md` rather than in a test that will pass by luck.

## 20. Documentation and archive

- [ ] 20.1 Sync this change's deltas into `openspec/specs/` before archiving — the second of 8.9's two runs. Four requirements of `openclaw-gateway-runtime`, three of `claims-mcp-surface`, four of `gmail-isolation-boundary` (all ADDED to capabilities the baseline already holds from slice 1), plus the five whole capabilities `telegram-bot`, `conversational-agent`, `llm-backend`, `reminder-scheduling` and `task-capture` (MODIFIED/REMOVED against the existing baseline). **Check the three ADDED files do not restate a requirement slice 1 already put in the baseline** — that is the failure mode `openspec validate --specs --strict` will not catch, because two differently-titled requirements about the same subject both validate.
- [ ] 20.2 Update `README.md` and root `CLAUDE.md` at cutover, not before. Slice 1's eval found both asserting the swap was unbuilt while it ran; the mirror-image error here is asserting the cutover happened while the token is still empty.
- [ ] 20.3 Append the cutover outcome to ADR-0015 (dead-channel supervision moved to the container boundary) and ADR-0014 (replay under gateway delivery) as `## Amendment (YYYY-MM-DD)` blocks. Append, never edit above the first amendment line.
- [ ] 20.4 Amend ADR-0025 if the confirm gate's location moves again during 3.2. It already records one correction ("in the MCP server" → split by origin); a second would make three states, and only the ADR can hold that history.
- [x] 20.5 **DONE 2026-08-02.** Fix the four non-discriminating assertions slice 1's eval found on axis 2, and add the missing self-checks. Justin's call (2026-08-02): after the merge, before slice 2's build starts.

  The eval report itself was never written to the repo and survived only in the session transcript; it was recovered from there, so the four are restated here rather than referenced. Each fix names what could not have failed before:

  1. `tools/list` was compared against `mcp_server.TOOL_NAMES`, which is derived from `TOOLS`, which is what `dispatch` returns — the assertion could not fail. Now a frozen literal of the seven names, plus a check that each served entry actually carries a description and an object schema (a client reads those off the response, never off `TOOLS`). `ping` was the one `dispatch` method with no assertion; asserted.
  2. The dangerous-tool scan iterates two production lists with no negative control, so a tripwire that stopped firing would read as a clean inventory forever. Self-checked now against five dangerous names and two safe ones, matching the precedent in `test_no_module_outside_claim_status_writes_the_status_column`. Same omission fixed in the `subprocess`/`config.OPENCLAW_CLI` marker scan. `_impls()`'s set-equality and no-`propose_*` asserts hold by construction and were left, but are now preceded by an assert that `_build_impls` really does carry `propose_*` — without it both would pass against an empty dict.
  3. Budget ceilings were 12 tools / 8,000 chars against a measured 7 / 2,405: five tools and 3.3x the schema could arrive green. Tightened to 8 / 3,000. **Section 3's proposal tools will turn this red on purpose** — re-measure a real turn and raise both numbers deliberately.
  4. `BUTTON_COMMANDS` had only "non-empty" and "slash-free" on it, neither breakable by a rename, so the tuple was decorative. New `test_the_plugin_registers_exactly_the_commands_a_button_may_emit` parses `const COMMANDS` out of `gateway-plugin/index.js` and asserts it equals the tuple — the preflight's deploy-time comparison, without the container.

  **One behaviour change, declared rather than slipped in:** `gateway_client.build_buttons` now refuses a verb outside `BUTTON_COMMANDS`. `button_commands.py` already stated that card-building code must draw from the tuple and nothing enforced it; an undeclared verb is not an error at the gateway, it reaches the agent as a chat turn and spends tokens. No card builds buttons yet, so nothing live changes — the guard is in place before section 4 writes the first caller. `test_a_button_command_over_the_byte_budget_is_refused_not_sent` had its padding fixture re-based on a real verb, since it was tripping the new guard before reaching the byte check it exists to measure.

## 21. Deferred out of slice 1's eval

- [ ] 21.1 The `1,172` vs `1,422` MCP schema-character discrepancy is recorded as unreconciled in slice 1's trail. Reconcile it here or state which number the budget assertion uses and why the other exists.
- [ ] 21.2 `openspec/specs/task-capture/spec.md` references a capability that does not exist, `task-telegram-surface` — pre-existing, from another change's incomplete sync, logged in `openspec/BACKLOG.md`. This change modifies `task-capture`, so it is the natural place to fix it.

- [ ] 21.3 **The rest of slice 1's axis-2 findings, transcribed 2026-08-02 because the eval report was never written to the repo and exists only in a session transcript.** 20.5 fixed the four assertions that could not fail; these are declared domains with unasserted members, which is a different defect and was not in 20.5's scope. Each names a failure path that is unexercised, and every one of them is a path this change either rebuilds or deletes — so assert them as the sections land, not in a batch:

  - `gateway_client._run`: 4 failure modes, 3 asserted. `TimeoutExpired` (`gateway_client.py:115-116`) unasserted.
  - `internal_api.record_event`: 4 outcomes, 3 asserted. The 500-on-write-failure (`internal_api.py:253-259`) is unasserted, and it is exactly the failure posture its own docstring is built on.
  - `internal_api._run`: 3 responses, 0 asserted. The `skipped` body (`internal_api.py:114-115`) is never checked — that is the body a caller sees when `run_exclusive` refuses an overlapping tick. **CLOSED 2026-08-04** — `test_an_overlapping_internal_call_says_skipped_and_never_reads_as_a_run` asserts the body, that the job function did not run, and that `job_runs.last_ok_at` did NOT advance while `last_skipped_at` did (a route that only ever skips is a stuck lock and must not read as healthy).
  - `message_log._describe`: the media branch has 3 outcomes and 2 asserted; the `<other>` fallback (voice, sticker, video) is unasserted, and the `edit:` prefix is asserted on `text` but not on `command`.
  - `internal_api.plugin_hello`'s `lstrip("/")` normalisation (`internal_api.py:177`) is never exercised — the test pokes `_plugin_report` directly and skips the route.
  - **(f)** Task 19a.4 claims the byte budget was asserted "using the longest real case (a condition selection)". It was not: the fixture is synthetic padding, and no production-built command string is measured anywhere. Section 4 builds the first real one — measure that, and correct 19a.4's claim rather than leaving it.
  - **(a)** `row["app_version"] == config.APP_VERSION` reads the constant production wrote, so it discriminates NULL and not correctness. Pre-existing pattern elsewhere in the suite; noted, not worth churning on its own.
