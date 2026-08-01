> **Stage: planning, not implementing (Justin, 2026-08-01).** No further code until he says otherwise. Section 1 and its tests are already written and passing (190/190) and stay as they are; sections 2–3 are approved *in principle* — build both, with his involvement where something needs confirming — but not yet. Everything below is a plan to be refined, not a queue to work through.

Sections 9 (logging parity) and 10 (test coverage) are **cross-cutting**: they interleave with 1–7 rather than following them. A task in 1–7 is not done until its logging and test counterparts are.

## 0. Spikes — answer before writing production code

Any negative answer changes the plan. Record the actual answer next to each task, not just a tick.

**0.1–0.6 need Justin.** They require installing the gateway, holding the bot token, and tapping real buttons in the real chat. They cannot be completed by an agent working alone; 0.7 can.

- [x] 0.1 **DONE 2026-08-01 — gateway running in Docker, isolated, unconfigured.** Container `openclaw-gateway-spike`, image `ghcr.io/openclaw/openclaw:latest` (v2026.6.34; v2026.7.1-2 available). No bind mount to `app/data`, no Docker socket, three named volumes of its own. Live claims container untouched. Token in the session scratchpad, not the repo.
  - **Runtime:** 324 MB, uid 1000 (`node`), entrypoint `tini -s --`, default cmd `node openclaw.mjs gateway`. **No Chromium in the standard tag** (`command -v chromium` → 127), so the `-browser` variant is the only one that bundles it.
  - **Port 18789**, `/healthz` returns 200. The image declares **no** `EXPOSE`, so the port must be published explicitly.
  - **Refuses to start unconfigured** (exit 78): *"Missing config. Run `openclaw setup` or set gateway.mode=local (or pass --allow-unconfigured)."*
  - **Refuses to bind without auth**, and in a container it defaults to `bind=auto` (0.0.0.0): *"Refusing to bind gateway to auto without auth."* Needs `OPENCLAW_GATEWAY_TOKEN`. Good default — it fails closed. Publish to host loopback only regardless.
  - **Default agent model is `openai/gpt-5.5`**, not Groq. Relevant to 11.5 and D8.
  - **Gateway log file is `/tmp/openclaw/openclaw-<date>.log` — inside the container.** Lost on recreate and outside every backup. Feeds 9.5 and 7.0c.
  - Tool and hook names are **not** in plugin metadata (`toolNames`/`hookNames` are empty for all 66); everything registers at runtime. So the inventory can only be read from a running, configured agent — 0.9 and 0.10 still need a plugin written.
- [x] 0.2 **PASSED — buttons DO render on a photo message via the CLI, styles included.** Blocking spike cleared. The five earlier failures were a malformed payload: `buttons` must sit inside `presentation.blocks[{type:"buttons"}]`, never at the top level. Validate with `normalizeMessagePresentation` from `plugin-sdk/interactive-runtime` before sending — a rejected presentation is dropped silently with `ok:true`. **Attempt 1 (default config): image delivered, NO buttons rendered.** `--presentation '{"buttons":[[...]]}'` was accepted and the send reported `ok:true` — the buttons simply did not appear, with no error. Note that failure mode: a silent drop, not a rejection. Attempt 2 pending after setting `channels.telegram.capabilities.inlineButtons = "all"` (enum: off|dm|group|all|allowlist; unset by default). **Do not conclude buttons are unsupported on media until attempt 2 is read.**
- [x] 0.3 **PASSED, verified visually 2026-08-01.** `message edit --message-id <id> -m <text>` changed the caption of a **photo** message; Telegram showed "edited". So `_append_result`'s caption path is portable and the crash-on-PDF-alerts risk does not carry over. Still untested against a **document** (PDF) message — the review alerts are documents, not photos, so confirm that case before relying on it.
- [ ] 0.4 Spike: capture a real Groq per-day token-limit response body (or reproduce from `llm_calls` history) and determine whether the gateway's failover classifies it as retryable. Record the verdict; if it fails fast, note it as a known gap rather than fixing it here.
- [ ] 0.5 Spike: can the Telegram channel be pinned so only one username reaches the agent, and can DM pairing be disabled outright? Record the config keys used.
- [ ] 0.6 Confirm the callback-data size limit is unchanged through the gateway (64 bytes) by sending a token at the boundary.
- [x] 0.7 Read the gateway's plugin API contract for event forwarding; confirm which registration API delivers inbound message and callback events, and note the `api.on` vs `registerHook` vs `registerTool` distinction that applies.

  **Answer — partial, and the missing half blocks phase 4.** Confirmed from a real third-party OpenClaw plugin on this machine (`context-mode` 1.0.111) plus the docs:
  - `api.on(...)` — typed lifecycle hooks. Named ones seen in working code: `session_start`, `before_tool_call`, `after_tool_call`, `before_compaction`, `after_compaction`, `before_prompt_build`, `before_model_resolve`.
  - `api.registerHook(...)` — command hooks, colon-delimited: `command:new`, `command:reset`, `command:stop`. Using the wrong one of these two registers silently and the hook never fires.
  - `api.registerTool(...)` — agent-callable tools, and it may be absent on some builds (that plugin guards with `if (api.registerTool)`). Agent-callable tools were surfaced via an **MCP sidecar** declared in `mcp.servers.<name>`, not by the plugin itself.
  - `register()` must be **synchronous** — the gateway discards its return value, so an `async register()` loses every hook registered inside it. Use the initPromise pattern.
  - **Not found anywhere: an event for an inbound channel message or a callback/button press.** `docs.openclaw.ai/tools/plugin` does not enumerate hook names at all, and the one working plugin available locally is session/tool-scoped and never touches channel traffic. So the event-bridge mechanism D2 depends on is **unverified**.
  - Consequence: task 1.4 and all of phase 4 rest on an assumption. Candidate fallbacks if no such hook exists — the Telegram channel's own `webhookUrl`/`webhookSecret` mode pointed at this app, or reading inbound via `openclaw message read`/`search`. Neither is validated.

- [x] 0.8 **Added by 0.7.** Resolve how inbound channel messages and callback presses reach an extension.

  **Answered 2026-08-01 — largely resolved, and it shrinks the work.** The mechanism exists; it is called an *interactive handler*, not an event hook, which is why 0.7 missed it. Docs, verbatim: *"Callback clicks not claimed by a registered plugin interactive handler are passed to the agent as text: `callback_data: <value>`."*
  - Conversation needs **no bridge at all** — the agent is the message processor and reaches the domain through MCP tools. Forwarding raw messages was porting the old architecture; removed from D2.
  - Only deterministic button callbacks need claiming, via the interactive handler.
  - Inline buttons additionally require `channels.telegram.capabilities.inlineButtons` to permit the target surface (default `allowlist`) — relevant to 0.2 and 0.5.
  - Residual unknowns for 0.9: the exact interactive-handler API, and whether a claimed callback can be acknowledged so Telegram stops showing a spinner.

- [ ] 0.9 **Added by 0.8.** Confirm the interactive-handler registration API and callback acknowledgement. Small, but 4.2 cannot be written without it.

## 1. Internal transport surface

- [x] 1.1 Add FastAPI internal router bound to `127.0.0.1` only, requiring a shared secret from config; reject anything else with no body detail.
- [x] 1.2 Add `POST /internal/tick`, `/internal/ingest`, `/internal/nudge` wrapping the existing pipeline entrypoints. No new logic in the handlers.
- [x] 1.3 Add an advisory lock so a second concurrent `/internal/tick` returns immediately without calling `pipeline.run_once`. Assert two concurrent calls never both run.
- [ ] 1.4 Add `POST /internal/telegram/event` accepting a gateway-shaped event; call `message_log.record_inbound` **before** any handler runs.
- [x] 1.5 Add a `gateway_client` module wrapping `openclaw message send` / `edit` / `react` with a single failure path that writes a human-readable reason and never swallows.
- [x] 1.6 Smoke tests for 1.1–1.5 with no gateway present (subprocess stubbed), added to `tests/test_core.py`.

## 2. MCP server — read tools

- [ ] 2.1 Add the Python MCP server module exposing read tools only: claim list, claim detail, pending actions (via `claim_status.pending_actions()`), pet list, status events.
- [ ] 2.2 Enumerate the inventory in one place so it can be asserted; no dynamic or wildcard registration.
- [ ] 2.3 Assert in the smoke suite that the inventory contains no filesystem, shell, browser, mailbox-search or secret-returning tool. This test is the enforcement of `gmail-isolation-boundary`.
- [ ] 2.4 Supply the real pet list and today's date as turn context read from the DB at call time, not baked into agent config.
- [ ] 2.5 Register with `openclaw mcp set`, confirm the tools appear in the agent's inventory, and verify a read question answers correctly against the live DB (read-only copy per the project's host-access rule).
- [ ] 2.6 Verify that with the Python app stopped, the agent reports the claims service unavailable and asserts no claim facts.

## 3. MCP server — proposals and the confirm gate

- [ ] 3.1 Add `propose_*` tools for the existing mutations (mark sent, set condition, assign pet, mark resolved, split between pets). Each records a pending action and returns a confirmation; none commits.
- [ ] 3.2 Move the commit path so it is reachable only from the confirm callback, never as a tool return value.
- [ ] 3.3 Port the two-pets-named refusal into the MCP server and assert it with the live 2026-07-27 message text ("This is actually split between echo and Aari. Aari cost was $35 out of this").
- [ ] 3.4 Port the no-per-item-amounts split refusal; assert no $0 rows are produced.
- [ ] 3.5 Assert every mutating tool accepts an explicit claim id, and that current-claim-from-reply is supplied to the turn.
- [ ] 3.6 Set the agent's tool-iteration cap explicitly in config; verify reaching it yields a best answer with visible truncation rather than a silent stop.
- [ ] 3.7 Verify a proposal reported as done by the model has in fact changed nothing in the DB.

## 4. Telegram cutover

- [ ] 4.1 Put the Python updater behind a config flag, defaulting on, so it can be disabled without deleting code. **DECIDED: the flag stays for one week of real daily use after cutover** (Justin, 2026-08-01), then section 6 removes it. Rollback is one env var and a restart, ~30s. A week is what it takes for the failures only real use finds — a caption that will not edit, buttons that will not attach to a card, a tap that quietly reached the LLM.
- [ ] 4.2 Write the thin Node event-bridge plugin: forwards inbound messages, edits and callback queries to `/internal/telegram/event`. No claims logic in the plugin.
- [ ] 4.3 Route the pending-free-text-flow check before the agent turn, so condition entry still consumes its reply and the agent never sees it.
- [ ] 4.4 Port outbound notification sends to `gateway_client`, including batched-claim messages, lifecycle notifications and the daily nudge. Every message keeps its `#id`.
- [ ] 4.5 Port card delivery: history cards, actions summary, per-item tap-to-resolve cards, PDF review alerts — all with working buttons.
- [ ] 4.6 Port `_append_result` to the gateway edit action, keeping the caption-vs-text split for documents and photos. Fall back to a reply if caption editing proved unavailable in 0.3, and log the degradation.
- [ ] 4.7 Port the 👍 acknowledgement to the gateway reaction action; verify a reaction failure does not break the handler.
- [ ] 4.8 Keep the authorization check app-side and case-insensitive; verify an event from any other username is rejected even if the gateway delivered it.
- [ ] 4.9 Persist the chat ID app-side from the `/start` event; verify an unattended notification with no registered chat ID logs the gap visibly and sends nothing.
- [ ] 4.10 Cut over: disable the updater flag, bind the gateway channel, enable the bridge. Single deploy step.
- [ ] 4.11 Verify live against the real chat: one claim taken from notification through condition tap, pet assign, mark sent, and confirm resolved. Record which claim id was used.
- [ ] 4.12 Verify a mid-handler crash leaves the row unprocessed and the replay queue re-runs it at startup.
- [ ] 4.13 Verify a duplicated gateway delivery commits no duplicate mutation.

## 5. Scheduling

- [ ] 5.1 Register gateway cron entries for the 15-minute tick, Gmail ingest and the daily nudge, pointing at the internal endpoints.
- [ ] 5.2 Add the reminder catch-up sweep replacing `misfire_grace_time=None`; a due-but-unfired reminder fires on startup.
- [ ] 5.3 Verify exactly-once firing across three restart cases: Python only, gateway only, both.
- [ ] 5.4 Verify a duplicated cron trigger does not re-fire an already-fired reminder.
- [ ] 5.5 Verify cron entries survive a gateway restart without re-registration.
- [ ] 5.6 Make a missing or disabled cron entry visible rather than presenting as an absence of due work.

## 6. Deletion and dependency cleanup

Do not start until phase 4 has run one full claim lifecycle on real data.

- [ ] 6.1 Delete `agent.py`'s tool loop and `llm.chat()`; confirm `extract()` / `extract_vision()` callers are untouched.
- [ ] 6.2 Delete `scheduler.py` and the updater code path; remove the flag from 4.1.
- [ ] 6.3 Remove `python-telegram-bot` and `apscheduler` from `requirements.txt`; rebuild and confirm the app starts.
- [ ] 6.4 Re-verify structurally that `send()` appears nowhere in `app/openclaw/` and no tool exposes sending.
- [ ] 6.5 Confirm the daily-budget walk still works for extraction after `chat()` is gone.
- [ ] 6.6 Run the full smoke suite; all LLM keys still force-blanked, vision still stubbed.

## 7. Deploy and operations

**DECIDED (Justin, 2026-08-01): the gateway runs in its own Docker container** — chosen for *isolation and to build trust in it*, not for deployment tidiness. That reason is stronger than the one I offered and it changes what the container must look like: containment is the point, so the boundary is the feature. Remaining integration details deliberately left open to decide together.

- [ ] 7.0a The gateway container gets **no bind mount to `app/data`** and no `DATABASE_PATH`. This is `gmail-isolation-boundary` enforced by the container boundary rather than by configuration — the Gmail token, the SQLite file and the invoice PDFs are simply not on its filesystem. The strongest form of D4, and the isolation reason is why.
- [ ] 7.0b Set `INTERNAL_API_ALLOW_HOSTS` to the Docker bridge subnet the gateway actually arrives from. The loopback default works in neither deployment shape; the shared secret remains the real auth.
- [ ] 7.0c Give the gateway container its own named volume for gateway state, and decide whether it joins `db_backup`'s scope. Its state directory currently sits outside every backup.

- [ ] 7.1 Add the gateway as a second compose service on Node 24; keep the DB out of its config and out of any workspace it can see.
- [ ] 7.2 Extend `scripts/deploy.ps1` to bring up both runtimes, stamp and report both versions, and print each health check.
- [ ] 7.3 Make a partial start report failure naming which runtime is down.
- [ ] 7.4 Keep `app_version` in `telegram_messages` as the Python app's version; record the gateway version separately without displacing it.
- [ ] 7.5 Confirm the gateway holds no Gmail credential and no Gmail-scoped Google key; document where its state directory lives and whether it is backed up.
- [ ] 7.6 **Pin the plugin set — and the work is DISABLING, not avoiding.** Measured 2026-08-01 on a stock container: **47 of 66 plugins enabled by default**, and *every* boundary-relevant one is on — `browser`, `file-transfer`, `phone-control`, `canvas`, `device-pair`, `memory-core`, `talk-voice`. The design assumed the agent starts with no browser and no filesystem reach; the true default is the opposite. `gmail-isolation-boundary` therefore requires positive action to establish, not merely restraint in adding things later.
  - Verified the mitigation works: `plugins disable browser` / `plugins disable file-transfer` persist and report `DISABLED` (restart to apply).
  - Decide the disable list, apply it as config rather than by hand, and make 2.3 assert the boundary-relevant plugins are off — an upgrade that re-enables a plugin must fail the suite, not pass quietly.
- [ ] 7.7 Check whether the gateway announces a dead channel as loudly as ADR-0015's restart-on-dead-updater did; if not, rebuild that alerting on top.

## 8. Documentation and decision trail

- [ ] 8.1 New ADR: OpenClaw gateway as the shell, Python as the domain — recording D1, D2 and why the domain was not ported.
- [ ] 8.2 New ADR: the proposal gate and harness refusals live in the MCP server, superseding the `telegram_bot._execute_action` location in ADR-0016.
- [ ] 8.3 Amend ADR-0009/0017: what the gateway covers, what `llm.py` keeps, and the daily-budget classification gap found in 0.4.
- [ ] 8.4 Amend ADR-0014/0015: `telegram_messages` retained and why; where dead-channel supervision now lives.
- [ ] 8.5 Amend ADR-0002: the stack is now two runtimes. Supersede the reasoning, do not delete it.
- [ ] 8.6 **DECIDED (Justin, 2026-08-01): reminders-and-push is a separate change after this one, unless it turns out to make sense to do now.** My read is that it does not: it is independent of the transport swap and becomes trivial once the gateway exists, so folding it in buys nothing but scope. Record against ADR-0003 that its original reason (no push channel existed) has expired, so the next reader sees a live question rather than a settled decision — and open a BACKLOG entry so it is not lost.
- [ ] 8.7 Update root `CLAUDE.md`, `app/openclaw/CLAUDE.md` module map, `CONTEXT.md` and `README.md`: new module boundaries, the two-runtime deploy, the internal endpoint, and the tool-inventory boundary.
- [ ] 8.8 Record in this file what was verified **live** versus only coded, per the project's working-style rule.
- [ ] 8.9 Sync the four modified capabilities and three new ones into `openspec/specs/` before archiving this change.

## 16. Rework forced by D10 (the CLI cannot send interactive messages)

- [ ] 16.1 ~~Supersede `gateway_client`'s role~~ **— void, D10 retracted.** `gateway_client` keeps the full outbound role including cards with buttons. Fix its `_argv`/presentation construction to emit `presentation.blocks[{type:"buttons"}]`, and add a test asserting the payload passes the platform normalizer rather than asserting our own shape. Original text: It stays valid for plain text and media and for caption edits; it CANNOT carry buttons. Every notify path that renders a card with buttons moves to the plugin. Keep the module and its one-seam logging property; narrow its documented scope.
- [ ] 16.2 **Plugin spike, replaces further CLI round-trips.** Minimal OpenClaw plugin that (a) registers an HTTP route, (b) renders a message with two inline buttons via the interactive path, (c) registers an interactive handler and proves a tap is claimed. Confirms or refutes D10 in one go and simultaneously answers 0.9, 0.10 and 13.x.
- [x] 16.3 **ANSWERED live 2026-08-01. `command` works; `callback` is inert without a plugin.**
  - Tapping a `{"action":{"type":"command","command":"/status"}}` button **invoked the slash command** and the gateway replied. Deterministic, no model in the path, no plugin required.
  - Tapping `{"action":{"type":"callback","value":"reject:7"}}` with nothing registered did **nothing at all** — no reply, no error, no log.
  - **This corrects an earlier alarm of mine.** I warned, from the docs, that an unclaimed callback is handed to the agent as text `callback_data: <value>`, which would put a model in the path of a commit token. That did not happen — it was simply inert. The risk in 9.10 may be smaller than stated, or conditional on config not set here. Re-verify before treating 9.10 as urgent; do not treat my earlier framing as established.
  - **Design consequence:** build the card interface on `command` actions wherever a tap can be expressed as a slash command — `/mark 7 sent`, `/pet 7 Aari`, `/resolve 7`. That reuses the existing command surface, needs no plugin, and keeps the LLM out entirely. A plugin is needed **only** for taps that cannot be a command string.
- [ ] 16.7 **A plugin is required after all** — correcting 16.3. `command` actions invoke *native* slash commands; `/mark`, `/pet`, `/resolve` are this app's, not the gateway's. The plugin must register them (`api.registerCommand`) and claim callbacks. It does **not** need to own outbound rendering — that stays with `gateway_client`.
- [x] 16.9 **`callback` actions sent from the CLI go nowhere — evidenced, with a stated limit.** Tapped with a working model (`groq/llama-3.3-70b-versatile`), the sender allowlisted, and `dmPolicy=allowlist`: no reply, no log line, and `channel_ingress_events`, `command_log_entries` and `diagnostic_events` all hold **zero rows**. The `callback_query` string in `openclaw.sqlite` is schema text, not data.
  Best explanation consistent with everything seen: the Telegram plugin encodes and routes the callbacks **it** creates (approvals, native commands); an arbitrary opaque `value` injected from outside has no registered owner and is discarded. That matches the docs — callback actions "carry opaque plugin data through the channel's interaction path, meaning channel plugins handle the interaction".
  **Limit of this finding:** it is a negative result. It cannot distinguish "discarded by design" from "misconfigured in a way we did not find". Proving it needs the plugin (16.2), which would also make the case moot by registering a handler. Do not spend more time on the negative.
  **Practical consequence, which is unchanged either way:** use `command` actions where a tap can be a slash command; build the plugin for anything else.
- [ ] 16.8 **9.10 reinstated.** Evidence that an unclaimed callback *does* reach the agent and spend tokens: open issue #46841 asks for a `telegram.callbackRoutes` webhook bypass to skip `processMessage` for zero-token handling. Our observation of silence is best explained by this spike's agent having no working model key. Re-test with a working model before treating the earlier "inert" result as the truth.
- [x] 16.6 Native rich messages enabled (`channels.telegram.richMessages=true`) so the 11.3 cards-vs-tables comparison is fair.
- [x] 16.5 **Answered by 18.5: a `command` action has the same 64-byte ceiling, minus a 6-byte prefix.** The risk named here was real. Condition buttons must keep carrying an index, not the text — but the conclusion drawn from that ("condition selection still needs `callback` plus a plugin handler") does **not** follow: an index-carrying command such as `/setcond 7 3` is 13 bytes and fits easily. No tap identified so far needs a `callback` action. Revisit only if a tap appears that cannot be named in 58 bytes.
- [ ] 16.6 Native rich messages are **off by default**: `channels.telegram.richMessages=true` enables "tables/details/rich media" (seen in `/status`). This is the feature 11.3 compares against Pillow cards — enable it before that comparison, or the test is unfair. A `command` button invokes a slash command, which maps onto the existing `/mark`, `/pet` surface with no token routing. If it holds, most of the bespoke callback bridge disappears.
- [x] 16.4 **ANSWERED live 2026-08-01: a document caption edits fine, but the default path logs a failure on every success.**
  - Sent a real PDF as a document, then edited it. The edit applied. Container log, on the successful edit: `[telegram] editMessage failed: Call to 'editMessageText' failed! (400: Bad Request: there is no text in the message to edit)`.
  - Reading `extensions/telegram/src/send.ts` and `action-runtime.ts` explains it. The `editMessage` action accepts both `content` and `caption`, and sets `editMode: caption != null ? "caption" : "auto"`. `"caption"` calls `editMessageCaption` directly. `"auto"` calls `editMessageText` first and only falls back to `editMessageCaption` after Telegram returns *there is no text in the message to edit* (`MESSAGE_HAS_NO_TEXT_RE`, `network-errors.ts:275`).
  - The CLI's `message edit` has **no `--caption` flag**, so every CLI edit of a document or photo takes the failing path: one wasted Telegram round trip and one error-shaped log line per successful card update.
  - **Consequence for `gateway_client.edit_message`:** send `caption` explicitly whenever the target message carries media, rather than relying on the `auto` fallback. Not for correctness — `auto` works — but for the failure-visibility rule. A log that says `editMessage failed` on every successful tap is how a real edit failure becomes invisible.
  - Second-order: the fallback is keyed to an **English Telegram error string**. It is a regex over `there is no text in the message to edit`. Passing `caption` explicitly does not depend on it; `auto` does.

## 15. Unauthenticated disclosure to unknown senders (found 2026-08-01, live — Justin flagged it)

- [ ] 15.1 **The gateway auto-replies to any unrecognised sender with a pairing kit.** Verbatim, to an unauthorised user: `"OpenClaw: access not configured. Your Telegram user id: <id>. Pairing code: UV7NHR3N. Ask the bot owner to approve with: openclaw pairing approve telegram UV7NHR3N"`. Telegram bots are discoverable by username, so any stranger who finds the bot learns it runs OpenClaw, receives a live pairing code, and is handed the exact phrasing to socially-engineer the owner into approving them. Justin raised this unprompted on first sight.
- [ ] 15.2 Find the setting that suppresses or blanks that reply and make silence the default before the gateway touches the real bot. If no such setting exists, that is a blocking finding, not a preference — the app's current behaviour is to ignore an unauthorised sender entirely and log the rejection, disclosing nothing.

## 14. Media can only be sent from an allowlisted directory (found 2026-08-01, live)

- [ ] 14.1 **The gateway refuses to send media from an arbitrary path**: `OutboundDeliveryError: Local media path is not under an allowed directory: /tmp/spike-card.png`. Sending the same file from `/home/node/.openclaw/workspace/` succeeded. Good security control, and it **breaks an assumption in D2**: `gateway_client.send_file(path)` was designed to hand the gateway a path on the *app's* filesystem, which will never be allowlisted — and cannot be, because 7.0a deliberately denies the gateway any sight of `app/data`.
- [ ] 14.2 Decide how rendered artifacts reach the gateway, given the isolation decision. Options: a narrow shared "outbox" volume carrying only rendered cards and claim PDFs (not the data dir); an HTTP fetch by URL from the app; or base64/stdin if the CLI supports it. **The isolation choice and the media allowlist together mean there must be exactly one narrow path between the two containers — design it deliberately rather than discovering it at cutover.** Whatever wins, it must not become a general mount of `app/data`.
- [ ] 14.3 Note for whoever runs these commands from Git Bash on Windows: `/tmp/x` in a `docker exec` argument is rewritten to `C:/Users/.../Temp/x` by MSYS path translation, producing a misleading "not under an allowed directory" error naming a Windows path. Prefix with `MSYS_NO_PATHCONV=1`.
- [x] 14.4 **The allowlist is a default, and here is exactly what it contains** (`dist/local-roots-*.js`, `buildMediaLocalRoots`): the OpenClaw-preferred tmp dir, `<configDir>/media`, and `<stateDir>/{media,canvas,workspace,sandboxes}`. Nothing else, and `/tmp` is **not** among them — the 14.1 failure was the allowlist working, not a misconfiguration. Verified live: a PDF in `/tmp` was refused, the identical file in `<stateDir>/media` sent.
  Two consequences for 14.2, which narrow the option set rather than settling it:
  - The shared-outbox option must mount **into one of those roots** (`<stateDir>/media` is the natural one). Mounting the app's render directory anywhere else on the gateway filesystem does nothing — the allowlist ignores mounts it does not know about.
  - `localRoots` can be set to the string `"any"`, which disables the check outright (`assertLocalMediaAllowed` returns immediately). Do not. Record it here so nobody later "fixes" a path error by reaching for it: it is the whole control, and it is one word.
  - 19b should assert the resolved roots do not include `"any"` and do not include the data dir, for the same reason the other deploy-time assertions exist — it is configuration, so no app-side test can see it.

## 13. The gateway's own Telegram surface (found 2026-08-01 running it live)

- [ ] 13.1 **The gateway registers 61 slash commands on the bot**, and logged *"menu text exceeded the conservative 5700-character payload budget; shortening descriptions to keep 61 commands visible."* Justin's bot today offers a handful — `/mark`, `/pet`, `/history`, `/actions`, `/start`. After cutover his command menu is largely OpenClaw's. This is a direct cost to "don't lose the Telegram UI I built" and was on nobody's list. Decide: prune the gateway's command set, or accept the menu changing shape. Check first whether the app's own commands can coexist or are displaced.
- [ ] 13.2 Confirmed working: disabling plugins removes them from the runtime. After `plugins disable browser file-transfer` and a restart the loaded set was `canvas, device-pair, memory-core, phone-control, talk-voice, telegram` — `browser` and `file-transfer` absent. Telegram **auto-enables** on detecting a token ("auto-enabled plugins for this runtime without writing config"), so the enabled set is partly implicit; 7.6's disable list has to be asserted, not assumed.
- [ ] 13.4 **Authorization is TWO concepts here, not one — and the app has only one.** Measured live: DM pairing controls *who may talk to the bot*; `commands.ownerAllowFrom` controls *who may run privileged commands and approve dangerous actions*. Doctor is explicit: "DM pairing only lets someone talk to the bot; it does not make that sender the owner for privileged commands." The app's model is a single `TELEGRAM_USERNAME` check covering both. Map deliberately, by numeric id (`telegram:<id>`) rather than username. Getting it wrong fails in one of two directions: Justin cannot run his own commands, or someone who paired can. Feeds 0.5 and 12.5.
- [ ] 13.5 An access denial reached the user as "OpenClaw: access not configured" and produced **no log line at default level** — verified against 10 minutes of container logs. A rejection that leaves no trace is the silent failure the project's rules forbid; the app logs every rejected command today. Find the log level that records it, or add it. Feeds 9.2.
- [ ] 13.6 Doctor also reports `CRITICAL: Session store dir missing (~/.openclaw/agents/main/sessions)` on a first run, and 32 skills with missing requirements. Neither blocked startup. Understand before treating a clean `doctor` as a health gate.
- [ ] 13.3 Telegram polling runs through an "isolated polling ingress" with a spool at `/home/node/.openclaw/telegram/ingress-spool-default`. Worth understanding before trusting delivery guarantees — it may already provide some of what ADR-0014's replay queue does.

## 12. Consequences of conversation bypassing the app (design D9)

Found 2026-08-01 while reconciling the specs with D2's correction. Four of these five are the gateway having to do something the app used to; none may be assumed to work.

- [ ] 12.1 **Resolves the D7/D2 collision.** Plugin forwards a *copy* of every inbound message to `/internal` for logging only — a tee, not a bridge. Without it the training dataset Justin kept the table for narrows to callbacks and outbound, which is the half he did not ask for. Assert an agent-handled message still produces a `telegram_messages` row with its raw payload.
- [x] 12.2 **DECIDED (Justin, 2026-08-01): the plugin claims text while a flow is pending.** `_pending_condition` and `_pending_split` are preserved; what he types is stored verbatim with no model between his words and `condition_text` — the field the hard rules forbid inferring. **This makes a currently-unverified capability load-bearing:** the plugin must be able to *conditionally intercept a text message*, not merely claim callbacks. The docs only evidence callback claiming. Note this is a different capability from 12.1's logging tee — a tee copies, this intercepts. See 0.10.
- [ ] 0.10 **Gates 12.2.** Confirm a plugin can conditionally claim an inbound *text* message (not just a callback) and prevent it reaching the agent. If it cannot, 12.2's decision is unavailable and Justin must re-choose between the agent-tool and fully-conversational options — both of which put the model between his typing and a hard-rule field. **Raise this before building anything in section 12.**
- [ ] ~~12.2-old~~ Superseded: decide the fate of `_pending_condition` and `_pending_split` (the "Other (type it)" condition entry and the per-item split walk). They need the next typed message routed to the app, which the correction removes. Three options: claim text while a flow is pending (reopens part of 0.8), re-express both as agent tools, or let the agent handle them conversationally and delete the dicts. `_pending_actions` is unaffected — it is a callback. **Justin's call: the third is most native, and least like the interface he has today.**
- [x] 12.3 **DECIDED (Justin, 2026-08-01): keep the 👍 ack for now.** Removing it mid-swap would blur a real regression with an intended change. The native typing indicator is better — it shows work in progress, not just receipt — and replacing the ack with it is logged in `openspec/BACKLOG.md` to revisit after cutover. Caveat recorded there: a **tap** may produce no typing indicator, which is the case the ack was added for.
- [ ] 12.3a Superseded — original: Confirm who sends the 👍 acknowledgement once the app no longer sees inbound messages. If the gateway does not ack automatically, decide between the agent doing it and dropping it. It exists so a slow handler does not feel dead.
- [ ] 12.4 Confirm the gateway delivers **edited** messages to the agent. If it does not, a typed correction vanishes — the exact 2026-07-27 failure, whose fix now sits outside the path.
- [ ] 12.5 Re-scope the app-side authorization requirement to callbacks only, and record that for conversation the gateway's access control *is* the authorization. This promotes 0.5 from a nice-to-have to load-bearing.
- [ ] 12.6 Rewrite the `telegram-bot` and `openclaw-gateway-runtime` spec deltas to match: they currently describe the app receiving all inbound events, which D2 no longer does.

## 18. RESOLVED: buttons are `command` actions; the plugin registers the commands (2026-08-01)

- [x] 18.1 **`api.registerCommand` works end to end.** `/claimtest` returned `PLUGIN COMMAND OK. args=(none)` from a plugin loaded via `plugins.load.paths`. This is the mechanism `/mark`, `/pet`, `/resolve` need.
- [x] 18.2 **`command` buttons work from the CLI.** A tap invokes the slash command through core's native command path — verified twice (`/status`, then plugin-registered commands). Deterministic, no model involved.
- [x] 18.3 **`callback` buttons cannot be driven from the CLI, by design.** `toTelegramCallbackData` wraps a callback action's value: `sanitizeTelegramCallbackData(buildTelegramOpaqueCallbackData(button.action.value))`. The raw value never reaches Telegram, so the namespace resolver — which splits raw `callback_data` on the first `:` — cannot match. The opaque encoding exists for a plugin's own send-and-decode round trip. Registration itself succeeded (no diagnostic pushed); the value simply never arrives in the form the resolver expects.
- [x] 18.4 **Architecture decision that follows: every button is a `command` action.** Plugin registers the commands; the command handler calls the app's `/internal` endpoints. No callbacks, no interactive handlers, no opaque tokens, no LLM in the tap path. This is simpler than every design considered so far.
- [x] 18.5 **ANSWERED 2026-08-01: the command budget is 58 UTF-8 bytes, and overflow deletes the button silently.**
  - `buildTelegramNativeCommandCallbackData` (`native-command-callback-data.ts:5`) is `"tgcmd:" + commandText`. `sanitizeTelegramCallbackData` (`approval-callback-data.ts:21`) returns `undefined` when the result exceeds `TELEGRAM_CALLBACK_DATA_MAX_BYTES = 64`. 64 − 6 = **58 bytes for the command string**, counted in UTF-8, not characters.
  - Overflow is not an error and not a dead button. `toTelegramCallbackData` returns undefined → `toTelegramInlineButton` returns undefined → `.filter(Boolean)` drops the button from its row → an empty row is dropped → with no rows left `buildInlineKeyboard` returns `undefined` and the message arrives with **no keyboard at all**. Sent live at 58 and 59 bytes: both returned `ok: true` with a real message id. **Silent-failure mode #7.**
  - Checked against the shipped constants: 58b renders, 59b is dropped. A realistic worst case, `/mark 7 Dental disease and extraction under GA`, is 46 bytes and fits.
  - **This closes 16.5.** The existing index-not-text discipline survives the transport change, and now has a second reason: free text in a command button overflows 58 bytes on the first long condition, and does so invisibly. Buttons carry ids and indices.
  - Regression test (19a) must assert the byte budget on the longest generated command, and must assert on `Buffer`-equivalent byte length rather than `len()` — a non-ASCII pet or condition name costs more than one byte.
- [ ] 18.6 **Gotcha to record in the module map:** `plugins list` / `plugins inspect` report `commands: []`, `hookCount: 0` and `Shape: non-capability` for a plugin whose commands demonstrably work. Those fields come from the **persisted registry**, not live runtime, and go stale silently (`persisted-registry-stale-policy`). Never diagnose a plugin from them; test the behaviour.
- [ ] 18.7 **Two silent enablement gates** for a `plugins.load.paths` plugin: the entry must ALSO be enabled via `plugins.entries.<id>.enabled = true`, and the default export must be wrapped in `definePluginEntry` from `plugin-sdk/plugin-entry`. A plain object export loads without error and never runs. Neither failure produces a diagnostic.

## 17. The default agent request is 23.5k tokens — Groq's free tier cannot run it (measured 2026-08-01)

- [ ] 17.1 **Hard blocker, not a tuning issue.** A single agent turn on a stock gateway measured **23,438–23,513 tokens** against Groq free tier's **12,000 TPM**. Verbatim: `413 Request too large ... on tokens per minute (TPM): Limit 12000, Requested 23438`. This is not exhaustion after heavy use — one turn is ~2x the per-minute ceiling, so it can **never** succeed no matter how long you wait. For comparison this repo's own chat request is ~2.6k tokens (`config.py`): **the gateway agent's turn is ~9x larger**.
- [ ] 17.2 **Justin's D8 instinct was righter than my answer.** He asked whether MCP would burn tokens; I said the tool inventory is a per-turn tax but framed it as manageable. Measured, the default surface alone breaks the provider this project standardises on. The cause is everything shipped on every turn: 45 loaded plugins' tool schemas plus **61 registered slash commands**. Adding claims tools makes it worse, not better.
- [x] 17.7 **MEASURED: plugins are NOT the lever. The floor is ~29k tokens.** Using Groq's `413 ... Requested N` as a measuring instrument via `openclaw agent --agent main --session-key <fresh>`:
  | Surface | Fresh session | Requested tokens |
  |---|---|---|
  | 45 plugins, 61 commands | no (accumulated) | 22,560 |
  | 1 plugin (`plugins.allow`) | no (accumulated) | 30,159 |
  | 1 plugin (`plugins.allow`) | **yes** | **28,991** |
  Cutting 44 plugins did **not** reduce the turn — a minimal surface measured *higher* than the full one. The bulk is the core agent prompt, core tools and skills (doctor reports 14 eligible), none of which `plugins.allow` touches. **Conclusion: Groq free tier's 12k TPM cannot run the OpenClaw agent, and no amount of plugin pruning changes that.** Gemini handles it comfortably (1M context) and is what the spike now uses.
  **Method note, twice-learned:** the first two rows are contaminated by session history — `agent:main:main` accumulates turns, so the numbers measure conversation, not surface. Always pass a fresh `--session-key`. Reporting "disabling plugins made it worse" from row 2 would have been a false finding.
- [ ] 17.8 If Groq is still wanted for the agent, the untested lever is `agents.defaults.contextPruning.tools.allow` (a tool allowlist) plus reducing skills — not plugins. Only worth pursuing if Gemini is rejected.
- [ ] 17.3 **7.6 gains a second, harder justification.** Pinning the plugin set was a security requirement (`gmail-isolation-boundary`); it is now also a **feasibility** requirement. Measure tokens per turn after disabling unused plugins and pruning the command surface, and treat "turn size under the provider's TPM" as an acceptance criterion with a number, not an aspiration.
- [ ] 17.4 Decide the provider for the agent against that number, not against preference. Options: cut the surface until Groq's 12k TPM fits; use a provider with a higher TPM; or accept a paid tier. Note this is **per-minute**, a different constraint from ADR-0017's per-day budget — both now apply.
- [ ] 17.5 **Observed failover behaviour, relevant to ADR-0017 and D8.** OpenClaw retried the *same* model 3 times (10s/20s/30s backoff) then surfaced `decision=candidate_failed reason=rate_limit next=none`. Correct shape for a per-minute limit, and it confirms the gateway classifies Groq 429/413s as `rate_limit`. Two notes: with one model configured there is nothing to fall through to, and retrying a `413 request too large` is futile by construction — the request size never changes, so all 4 attempts were unsatisfiable.
- [ ] 17.6 **Operational: the spike shares the production Groq key.** The gateway agent competes for the same free-tier quota as the live claims service. Give the gateway its own key, or accept that agent traffic can starve `invoice_matching`'s extraction calls.

## 11. Fit the OpenClaw architecture — audit before keeping anything bespoke

Justin's principle (2026-08-01): the solution fits OpenClaw, not the reverse. Keep what this repo built **only** where the trade-off of losing it is real; the burden of proof is on keeping the bespoke thing. Each item below is currently an assumption in the design, not a measured decision. Answer with what the gateway actually does, then decide.

- [x] 11.1 `telegram_messages` vs the gateway's own message records.

  **Decided by Justin, 2026-08-01: HARD KEEP.** The training-dataset job is real and intended, and the gateway is unlikely to store raw payloads tagged with *this app's* version. The audit-trail and replay jobs may well be duplicated by the gateway; that duplication is accepted rather than investigated, because the dataset job alone justifies the table. Consequence for the build: `message_log` stays wired into the new transport, `record_inbound` keeps its write-before-handler ordering, and every send path through `gateway_client` must keep writing. Not to be reopened on fit-the-architecture grounds.
- [ ] 11.2 Native approval buttons vs the MCP-side confirm gate (D3). **Justin, 2026-08-01: decide after seeing it — do not choose on my reasoning.** So this is now a capture task, not an analysis task: install the gateway, drive a real mutation on a real claim, and bring back what its approval prompt *actually says on the phone*. The question it must answer: can the prompt show a full rendered outcome ("assign Aari to claim #7, Echo gets $28"), or only a tool name? If only a tool name, a wrong claim chosen by the model would be invisible at the moment of approval. Present the capture, then he decides. **Blocks treating D3 as decided.**
- [ ] 11.3 Pillow claim cards vs native rich tables. **Justin, 2026-08-01: build one of each and let him compare on his own phone.** Not a mockup comparison — send a real rendered `claim_card` PNG and the same data as a native table to the live chat, same session, and let him pick.
- [ ] 11.3a **Constraint that may decide 11.3 before taste does:** the actions view needs a tap-to-resolve button *per row*. Images carry that today. Whether a native table can attach per-row buttons is unverified — fold this into 0.2. If it cannot, the actions view stays an image regardless of preference, and 11.3 narrows to the history view alone.
- [ ] 11.4 `tasks.py` / `reminders.py` vs the gateway's own `tasks` and `cron`. 97 lines plus a scheduler against a native facility. Watch for the name collision — a gateway "task" is not an OpenClaw-Claims Task (`CONTEXT.md`).
- [x] 11.5b **RESOLVED — Groq works as a custom OpenAI-compatible provider.** No third-party plugin, no new account, and it reuses the existing free-tier key. Config that worked:
  ```
  models.providers.groq = {
    "baseUrl": "https://api.groq.com/openai/v1",
    "api": "openai-completions",
    "apiKey": "<GROQ_API_KEY>",
    "models": [{"id":"llama-3.3-70b-versatile","name":"...","input":["text"],"contextWindow":131072}]
  }
  ```
  `models` **must be an array of objects with `id`** — an object keyed by model id is rejected (`expected array, received object`). Afterwards `models list --provider groq` reports `Auth: yes` and the agent starts on it. Keeps D8's token maths valid, since Groq's 100k/day/model ceiling still applies.
- [ ] 11.5a **Superseded by 11.5b, kept for the reasoning: OpenClaw ships no Groq provider.** Measured 2026-08-01 — 38 bundled providers (`anthropic, google, openai, mistral, together, openrouter, ollama, xai, …`), **none is Groq**. Setting `agents.defaults.model.primary = groq/llama-3.3-70b-versatile` is accepted by config and then fails at runtime with `FailoverError: Unknown model ... reason=model_not_found next=none`; the model appears in `models list` only because it was configured, showing `Auth: no`. The whole LLM stack here is Groq (ADR-0009 default, ADR-0017's four-model daily-budget chain), so **the gateway agent cannot use the provider this project standardised on**. Options: a ClawHub/third-party Groq plugin, configuring Groq as a custom OpenAI-compatible provider under `models.providers[]`, or running the agent on Gemini (bundled as `google`, key already present, `google/gemini-2.5-flash` authenticates fine — used for the spike). Note the consequence for D8's token maths: Groq's 100k/day/model ceiling is what that analysis assumed.
- [ ] 11.5 `llm.py`'s fallback chain vs gateway model failover, once 0.4 says whether daily-budget exhaustion is classified. Extraction may keep its own walk while chat delegates; say so explicitly either way.
- [x] 11.0 **Fenced off from this audit (Justin, 2026-08-01).** Two behaviours are not open to native replacement; design around them.
  - **The two harness refusals** — two-pets-named never becomes a one-pet assignment; a split with no per-item amounts is refused rather than filling $0 rows. Both lost once as prompt rules. A native validation layer cannot know they exist, so they stay in code we control.
  - **`#id` in every message** — survives whatever rendering wins in 11.3 and whatever approval UX wins in 11.2. A native table or approval prompt that renders "Aari, The Shire Vet, $284.50" and drops the id produces exactly the un-actionable output that was reversed on 2026-07-24.

  He also selected "audit everything", which conflicts with naming those two. Reading applied: fence those two absolutely, audit everything else. The item this leaves open is the named-sweeps / no-mailbox-browsing boundary (ADR-0016), and the conflict largely dissolves on inspection — that boundary is a **hard rule, not a design choice**, so auditing it can only mean "is there a *stronger* native enforcement?", never "can it go?". His own choice to run the gateway in an isolated container is precisely such a stronger enforcement: a filesystem the token is not on beats a tool allowlist. Flagged to him; correct if the reading is wrong.

- [ ] 11.6 Record each verdict with its reason in `design.md`, including the ones where the bespoke version wins. A kept-because-measured decision and a kept-because-nobody-checked decision must not look the same later.

## 9. Logging and observability — parity or better

Baseline to hold: `telegram_messages` (raw payload + `app_version` + `processed_at`), `llm_calls` (per attempt), `ops_alerts` with ADR-0015 levels, `vet_claims.flag` human-readable reasons, `claim_status_events`, `vision_ocr_attempts`, `email_extractions`. Nothing here may get quieter.

- [ ] 9.10 **Added by 0.8.** Prove the interactive handler actually registered: assert at startup that a known callback token is claimed, and treat any `callback_data:` string reaching the agent as an error, never as input. Without this, a plugin that registered nothing looks healthy and silently routes every tap — including `sent:7` — through the LLM. Registering with the wrong API fails silently (confirmed in a working plugin's source), so "it loaded" is not evidence.
- [ ] 9.11 **Added by D8.** Record the measured token cost of one chat turn with the final tool inventory, against the 100k/day/model ceiling. The schema ships on every request, so tool count is a per-turn tax — treat it as a budget with a number, not a matter of taste.
- [ ] 9.1 Add a correlation id minted at the gateway edge and carried through plugin → internal endpoint → handler → any resulting send. Persist it on the `telegram_messages` row. This is the "better": today an event crossing two runtimes cannot be traced end to end.
- [x] 9.2 Log every `/internal/*` request: route, outcome, correlation id. Log rejections (bad/missing secret, non-loopback origin) explicitly — a rejected event must not look like an event that never arrived.
- [x] 9.3 Make `gateway_client` failures loud: capture the CLI's exit code and stderr into the logged reason. A failed send writes a human-readable reason and never becomes a silent no-op.
- [ ] 9.4 Keep `telegram_messages` writing on the gateway path with no field lost — raw payload, truthful kind, summary, `app_version`, `processed_at` ordering. Assert an edit event still logs as an edit with its text (the 2026-07-27 empty-`other` regression).
- [ ] 9.5 Locate and document where the gateway records its own LLM calls and chat turns; write down the two-place accounting (`llm_calls` + gateway records) so a token-spend question is answerable. Record retention and whether that store is backed up.
- [ ] 9.6 Log each tick's outcome app-side (claims advanced, flags written, duration) rather than relying on `cron runs` alone; `cron runs` says it fired, not what it did.
- [ ] 9.7 Preserve ADR-0015 alerting levels across the swap, and add a dead-channel alert if 7.7 finds the gateway's supervision quieter than the old restart-on-dead-updater.
- [ ] 9.8 Verify no log line, alert, or error message carries a secret, a bank detail, or `.env` content — the new internal endpoint and the CLI stderr capture are both new places one could leak.
- [ ] 9.9 Write down the one thing that does get quieter: chat-side LLM calls leaving `llm_calls`. Named in the llm-backend spec; it must also be in the docs, not just the spec.

## 19. Non-functional regression tests — the learnings must not rot

Everything in this section exists because it was discovered the hard way on 2026-08-01. They split by what can be checked hermetically and what needs a live gateway; **both halves are required**, because the interesting failures here are configuration and budget, which no unit test can see.

### 19a. Hermetic — `app/tests/test_core.py`, no gateway present

- [ ] 19a.1 **Presentation payload shape.** Assert `gateway_client` emits buttons at `presentation.blocks[{type:"buttons"}]` and never at the top level. Five sends were silently discarded for this, each returning `ok:true` with a real message id. Assert the exact nesting, not just "buttons present".
- [ ] 19a.2 **Never trust `ok`.** Assert `gateway_client` treats a success response with a dropped presentation as a failure where detectable, and that no code path infers "rendered" from "sent". This is the guard against the platform's defining hazard.
- [ ] 19a.3 **Tool-inventory budget.** Assert the MCP inventory count stays at or under a declared maximum. Every tool schema ships on every chat turn; an unbounded inventory is a silent per-turn cost increase. Failing loudly on tool #N+1 is the point.
- [ ] 19a.4 **Command payload length.** Assert every generated button command string stays under the transport limit, using the longest real case (a condition selection). Fail with the offending string, since Justin has accepted trimming to fit.
- [ ] 19a.5 **One outbound seam.** Already implemented — keep `test_nothing_outside_gateway_client_shells_out_to_the_gateway` and extend it if a second transport appears.
- [ ] 19a.6 **`#id` on every outbound claim message** across the gateway path, including button labels and command strings.
- [ ] 19a.7 **Tool inventory contains no filesystem, shell, browser, mailbox-search or secret-returning tool** (the `gmail-isolation-boundary` enforcement, currently task 2.3 — cross-referenced here so the NFR set is complete in one place).

### 19b. Live preflight — `scripts/gateway_preflight.py`, run by `deploy.ps1`, fails the deploy

A config check, not a test suite. Each assertion below corresponds to something that was found silently wrong.

- [ ] 19b.1 **Agent turn size under a declared ceiling.** Measure with `openclaw agent --agent <id> --session-key <fresh> --message hi` and assert the result is under the configured model's per-minute limit. **Must use a fresh session key** — an accumulated session measures conversation history, not surface, and produced two false readings before this was caught.
- [ ] 19b.2 **The model can actually serve a turn.** A model id that config accepts can still fail at runtime with `model_not_found` (Groq did, before a custom provider entry existed). Assert one real turn completes.
- [ ] 19b.3 **Boundary plugins are disabled**: `browser`, `file-transfer`, and anything else granting filesystem, shell or browser reach. An upgrade re-enabling one must fail the deploy, not pass quietly.
- [ ] 19b.4 **Access configuration**: `channels.telegram.dmPolicy == "allowlist"`, `allowFrom` non-empty, `commands.ownerAllowFrom` non-empty. Default is `pairing`, which hands unknown senders a live pairing code and the command to socially-engineer approval.
- [ ] 19b.5 **The gateway holds no Gmail credential** and no Google key with a Gmail scope, and has no mount that can reach `app/data`.
- [ ] 19b.6 **The plugin actually registered.** Assert its commands respond, not that `plugins list` reports them — those fields come from a persisted registry that goes stale silently and reported `commands: []` for a working command.
- [ ] 19b.7 **Media outbox path is allowlisted** and is a narrow directory, not `app/data`.

### 19c. Documented, not tested — record where a test is impossible

- [ ] 19c.1 State plainly in the deploy docs which of the above cannot be asserted and why, so the gap is visible rather than assumed covered. A checklist that silently omits an unverifiable item reads as full coverage.

## 10. Test coverage

All additions go in `app/tests/test_core.py` (assert-based, no pytest) and must stay hermetic: LLM keys force-blanked, vision stubbed, **and runnable with no gateway installed**.

- [x] 10.1 Stub the gateway CLI at a single seam so every send/edit/react path is testable without a daemon; assert the suite passes with the gateway absent.
- [ ] 10.2 Regression: agent tool inventory contains no filesystem, shell, browser, mailbox-search or secret-returning tool. This is the test that catches a future plugin install breaching `gmail-isolation-boundary`.
- [ ] 10.3 Regression: no send path — `send()` absent from `app/openclaw/`, no send tool in the inventory.
- [ ] 10.4 Regression: every outbound claim message carries its `#id` (existing test, re-pointed at the gateway path).
- [ ] 10.5 Regression: a `propose_*` tool commits nothing; only the confirm callback commits. Include the case where the model's text asserts it is already done.
- [ ] 10.6 Regression: the two-pets-named refusal, asserted with the live 2026-07-27 message text.
- [ ] 10.7 Regression: split with no per-item amounts is refused, no $0 rows.
- [ ] 10.8 Regression: pending free-text flow consumes its reply and the agent never receives it as a turn.
- [ ] 10.9 Regression: caption-vs-text append on document and photo messages.
- [ ] 10.10 Regression: authorization rejects any other username even when the gateway delivered the event; case-insensitive compare still passes.
- [x] 10.11 Regression: two concurrent `/internal/tick` calls never both enter `pipeline.run_once`.
- [ ] 10.12 Regression: duplicate gateway delivery commits no duplicate mutation; unprocessed row replays at startup.
- [ ] 10.13 Regression: daily-budget fallback still walks models for `extract()` after `chat()` is deleted.
- [ ] 10.14 Regression: correlation id present on the `telegram_messages` row for a gateway-delivered event.
- [x] 10.16 **Added.** Guard test: nothing outside `gateway_client` imports `subprocess` or reads `config.OPENCLAW_CLI`. Converts the one-seam rule from convention into enforcement — the gap the module map rates as only *partial* for `LoggedBot`. Match the USAGE form (`config.OPENCLAW_CLI`), not the bare name: `config.py` defines the setting and a guard that fires on its own definition site gets trained away.
- [ ] 10.15 Run the full suite at the end of every phase, not only at phase 6. Record pass/fail in this file with the actual output on failure.

  **Section 1 run, 2026-08-01: PASS.** 190/190 tests, exit 0, `ALL TESTS PASSED`. 11 new tests, gateway CLI stubbed via an injected `runner` — suite still passes with no gateway installed and no new dependency.
  Two harness facts learned the hard way, both worth knowing before adding tests here:
  - The runner iterates `globals()` inside `if __name__ == "__main__":`, so anything appended **below** that block is never defined when it runs. The suite still prints `ALL TESTS PASSED`. A silent no-op.
  - `_fresh_db()` deliberately does NOT clear `telegram_messages` (it is the RL dataset), so assert message-log counts **relatively**, never against an absolute total.
  - Piping the run through `tail` reports `tail`'s exit code, not Python's. Redirect to a file if you need the real one.
