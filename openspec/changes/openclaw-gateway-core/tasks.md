> **Stage: planning, not implementing (Justin, 2026-08-01).** No further code until he says otherwise. Section 1 and its tests are already written and passing (190/190) and stay as they are; sections 2–3 are approved *in principle* — build both, with his involvement where something needs confirming — but not yet. Everything below is a plan to be refined, not a queue to work through.

Sections 9 (logging parity) and 10 (test coverage) are **cross-cutting**: they interleave with 1–7 rather than following them. A task in 1–7 is not done until its logging and test counterparts are.

**This change ships in two slices (8.11, Justin, 2026-08-01).** Slice 1 is everything that can be true while the Python app still owns Telegram: sections 0, 1, 2, 7, 8, 11, 13 (except 13.1c), 14–19. Slice 2 is the cutover and what depends on it: sections 3, 4, 5, 6, 12, 13.1c. Sections 9 and 10 split by the same rule. The directory split happens at slice 1's archive (8.14), not now — until then this file holds both.

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
- [x] 0.4 **ANSWERED by 11.5, from the shipped classifier rather than a captured response body.** The gateway has a single `rate_limit` bucket and treats it as **transient** — `shouldAllowCooldownProbeForReason` and `shouldUseTransientCooldownProbeSlot` both include it, so it re-probes the exhausted model during cooldown, and `resolveSessionSuspensionReason` maps it to `quota_exhausted` regardless of window. It does not distinguish per-day from per-minute. Recorded as a known gap, not fixed here: extraction and vision keep `llm.py`'s per-model daily walk; chat delegates and spends three futile retries per exhausted-day turn. ADR-0009's amendment carries it.
- [x] 0.5 **ANSWERED live.** Yes to both. Keys in use: `channels.telegram.dmPolicy: "allowlist"`, `channels.telegram.allowFrom: ["<id>"]`, `channels.telegram.groupPolicy: "allowlist"`, and `commands.ownerAllowFrom: ["telegram:<id>"]`. The default is `pairing`, which is what produced 15.1 — an unrecognised sender receives a live pairing code and the command to request approval. 19b.4 asserts the allowlist setting at deploy because the default is the dangerous one and configuration regresses silently.
- [x] 0.6 **ANSWERED — and it is two different numbers, which 18.5 only recorded one of.** Telegram's 64-byte `callback_data` ceiling is unchanged, but the gateway prefixes it, so the usable payload depends on the action type:
  - **`command`** → `tgcmd:` + text = 6 bytes overhead → **58 bytes** usable. Verified live at the boundary (58 renders, 59 silently vanishes).
  - **`callback`** → `buildTelegramOpaqueCallbackData` writes `tgcb1:` + a 5-character checksum + `:` = 12 bytes overhead → **52 bytes** usable. Derived from source, not sent, because the card interface uses no callback actions.
  Recorded so the 52 is on file if a callback is ever needed; today only 58 is operative.
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

- [x] 0.9 **Largely void — its premise was callbacks, and the card interface has none.** Buttons are `command` actions (D12/18), so no interactive handler is registered and no callback acknowledgement is needed. 16.9 separately showed an externally-injected callback is discarded.
  The residual worth keeping is not about callbacks at all: **does `api.registerCommand` work end to end, from a button tap through to `/internal`?** Folded into 16.2, where it belongs. Original text: Confirm the interactive-handler registration API and callback acknowledgement. Small, but 4.2 cannot be written without it.

## 1. Internal transport surface

- [x] 1.1 Add FastAPI internal router bound to `127.0.0.1` only, requiring a shared secret from config; reject anything else with no body detail.
- [x] 1.2 Add `POST /internal/tick`, `/internal/ingest`, `/internal/nudge` wrapping the existing pipeline entrypoints. No new logic in the handlers.
- [x] 1.3 Add an advisory lock so a second concurrent `/internal/tick` returns immediately without calling `pipeline.run_once`. Assert two concurrent calls never both run.
- [ ] 1.4 Add `POST /internal/telegram/event` accepting a gateway-shaped event; call `message_log.record_inbound` **before** any handler runs.
- [x] 1.5 Add a `gateway_client` module wrapping `openclaw message send` / `edit` / `react` with a single failure path that writes a human-readable reason and never swallows.
- [x] 1.6 Smoke tests for 1.1–1.5 with no gateway present (subprocess stubbed), added to `tests/test_core.py`.

## 2. MCP server — read tools

- [x] 2.1 **BUILT 2026-08-01 — `app/openclaw/mcp_server.py`, seven read tools, mounted at `/mcp`.** `turn_context`, `query_claims`, `pending_actions`, `claim_detail`, `claim_history`, `submissions_awaiting_reply`, `list_tasks`. Every implementation is reused from `agent._build_impls` rather than restated — `pending_actions` in particular shares its derivation with the `/actions` cards specifically so chat and cards cannot disagree, and a second copy here would be the fifth instance of the bug this codebase keeps making.

  **Hand-rolled JSON-RPC, not the official SDK, and the reason is a hard conflict rather than a preference.** `pip install mcp` pulls `mcp` 2.0.0 → starlette 1.3.1; this app pins `fastapi==0.115.6`, which requires `starlette<0.42`. `pip check` went red on the spot. It also drags in opentelemetry, jsonschema, httpx2, pyjwt and pywin32 — into the one container whose small surface is part of the security argument. Reverted, venv restored, `pip check` clean. Streamable HTTP minus the stream is a JSON-RPC POST endpoint, which is what this is: `initialize`, `ping`, `tools/list`, `tools/call`, notifications answered with 202, and `GET /mcp` answered 405 because nothing here pushes.
- [x] 2.2 **Enumerated in `TOOLS`, no dynamic or wildcard registration.** `_impls()` selects out of the chat agent's implementations *by name from `TOOLS`*, so a `propose_*` function existing in the same dict cannot be reached — asserted, not argued.
- [x] 2.3 **`test_mcp_inventory_has_no_dangerous_tool`.** Substring tripwire over the inventory (`file`, `shell`, `exec`, `browser`, `mail`, `secret`, `send`, `sql`, …) plus an assertion that the reachable implementation set equals `TOOL_NAMES` exactly and contains no `propose_*`. This test is the `gmail-isolation-boundary` enforcement; 13.4 is why it cannot be relaxed to prompt discipline.
- [x] 2.4 **`turn_context` reads pets and today's date from the DB per call.** Asserted by inserting a pet mid-test and requiring the next call to see it — a cached list would pass a naive test and be wrong the day a pet is added. This is why the shipped `USER.md` deliberately carries no pet list.
- [x] 2.5 **REGISTERED AND PROBED LIVE 2026-08-01, then driven end to end on Groq.**

  ```
  openclaw mcp add claims --url http://host.docker.internal:8978/mcp \
    --transport streamable-http --header X-OpenClaw-Secret=...
  openclaw mcp probe claims   ->  "- claims: 7 tools"
  ```

  `mcp probe` opens a real MCP connection, so this is the product's own validator agreeing with the hand-rolled server rather than our own test agreeing with itself. The server log shows the exact handshake: `POST 200` (initialize) → `POST 202` (`notifications/initialized`) → `GET 405` (the client tries to open a stream, is refused, carries on) → `POST 200` (`tools/list`).

  **Then a real turn.** `--message "What is waiting on me right now?"` on `groq/llama-3.3-70b-versatile`, against a scratchpad copy of the live DB: the agent called the tools and answered with real pets, real vets and the BLOCKED marker that `pending_actions` emits. **First end-to-end agent turn on the provider this project standardises on.**

  Four things fall out of it:
  - **MCP tools are named `<server>__<tool>`** — `claims__query_claims`, and so on. `tools.allow` matches those names and accepts `claims__*`. Two allowlists written from the bare names (`query_claims`) failed with *"No callable tools remain after resolving explicit tool allowlist"*. Not guessable; read off `systemPromptReport.tools.entries`.
  - **Turn size with the real inventory: 4,934 prompt tokens**, tools contributing 1,172 schema chars across 7 entries. Against Groq's 12,000 TPM that is ~7,000 tokens of headroom, and it *confirms* rather than merely predicts ADR-0023's budget — the earlier ~8,100 figure was measured with a stand-in tool. The inventory is cheap; the floor is the core prompt.
  - **The reply carried no claim `#id`s**, though `INSTRUCTIONS` demands them in as many words. A hard-won UI rule stated as prompt text did not hold on its first live turn. Consequence for 19a.6: it can assert the *tool output* carries ids (it does), but it cannot assert the model repeats them — that needs `AGENTS.md` reinforcement and a live check, not a hermetic test. Same lesson as 13.4, on a rule that is ours rather than the platform's.
  - Operational: **Gemini's free tier was exhausted** by the measurement turns mid-session (`429 ... exceeded your current quota`). 17.6 recorded this hazard for the shared Groq key; it applies to Gemini too, and Gemini is the only vision-capable backend (ADR-0010).
- [x] 2.6 **VERIFIED 2026-08-01 — it fails closed, and harder than the spec describes.** With the Python app stopped, no `claims__*` tool registers, so `tools.allow: ["claims__*"]` resolves to an empty set and the gateway refuses the turn outright: *"No callable tools remain after resolving explicit tool allowlist … Fix the allowlist or enable the plugin that registers the requested tool."* No claim facts are asserted, because no answer is produced at all.

  A second probe with one unrelated tool left allowed (`memory_search`) got a turn that reported a tool unavailable and still asserted no claim facts. One turn is weak evidence for a model's behaviour, but the *structural* result is strong: the tools genuinely disappear when the app is down, so there is nothing for the model to answer from.

  **Gap, and it belongs in slice 1:** the user sees a raw `GatewayClientRequestError: … No callable tools remain …` in Telegram. The requirement says the reply should *state the claims service is unavailable*. Fail-closed is the right posture and must not be softened into a fallback that answers anyway — what is missing is the sentence. Fix belongs in the plugin, which can map that error to a human one. Added as 2.7.
- [ ] 2.7 **Added by 2.6.** Map the gateway's empty-allowlist error to a human sentence before it reaches the chat. Must not become a fallback that answers the question anyway — the whole value of the current behaviour is that no claim fact can be produced when the source of truth is unreachable.

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

- [x] **7.0a / 7.0c / 7.1 / 7.2 / 7.3 / 7.4 BUILT 2026-08-02.** `docker-compose.yml` gains the gateway service; `scripts/deploy.ps1` brings up both runtimes, stamps and reports both versions, and treats a partial start as a failure; `scripts/gateway_preflight.py` runs the config assertions and fails the deploy. Compose validates.

  **The isolation decisions are enforced by the file, not by discipline.** No `env_file` on the gateway, no bind mount to `app/data`, no Docker socket, its own named volume for state. The only path between the containers is a `media_outbox` volume, mounted read-only into `<stateDir>/media` because the media allowlist is a fixed set of roots and ignores mounts it does not know about (14.4).

  **A fixed subnet (`172.28.0.0/24`) with static addresses**, because `INTERNAL_API_ALLOW_HOSTS` matches exactly rather than by CIDR and a bridge address is otherwise dynamic. Recorded with how little it buys: the shared secret is the boundary, and 16.2 showed the allowlist cannot distinguish the gateway from any other host process when traffic arrives via `host.docker.internal`. Over a compose network it can, which is why the plugin uses the service name.

  **Found while wiring it: the gateway's secrets cannot come from `app/.env`.** Compose variable interpolation reads a root `.env` or the shell; `env_file:` only populates a container's environment and does not feed `${...}`. Giving the gateway `env_file: ./app/.env` would hand it the Gmail credential and `DATABASE_PATH` — the exact thing 7.0a forbids. So the gateway's three values live in a root `.env` (`.env.example` committed, `.env` gitignored at any level). The separation is the boundary rather than a workaround: nobody can point the gateway at the app's secrets without noticing they are duplicating them.

  Consequence worth naming: `INTERNAL_API_SECRET` now exists in **two** files that must agree, which is the divergence hazard this repo already has once. It fails loudly — a mismatch means the plugin's boot report is rejected, `/health.gateway_plugin` stays empty, and the preflight fails the deploy naming it. Nothing half-working reaches Justin's phone.

  **7.4:** `GATEWAY_VERSION` is read from the running image by `deploy.ps1` and surfaced on `/health` beside `app_version`, never into it. `telegram_messages.app_version` keeps meaning "the Python code that handled this row".
- [ ] 7.0a-orig The gateway container gets **no bind mount to `app/data`** and no `DATABASE_PATH`. This is `gmail-isolation-boundary` enforced by the container boundary rather than by configuration — the Gmail token, the SQLite file and the invoice PDFs are simply not on its filesystem. The strongest form of D4, and the isolation reason is why.
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

- [x] 8.13 **Trail audit 2026-08-01 — five defects in my own work, found by running the checklist rather than by re-reading what I meant to write.**
  1. **Supersession was one-directional.** ADR-0025 said it supersedes the gate's location in ADR-0016; ADR-0016 said nothing. A reader arriving at 0016 first — which CLAUDE.md tells them to — would have followed a location that no longer exists. Amendment added there, and its **Status** line now records the partial supersession, which the repo's own convention requires and I had skipped.
  2. **The index row for 0016 still summarised it as "why not MCP".** The design adopts MCP. Row corrected to say the "not MCP" half is reversed while the sweeps and no-repo-access rule stand — an index that misdescribes an ADR is worse than no index, because it is read instead of the ADR.
  3. **ADR-0006 looked contradicted and was not.** It rejected a separate deployable; the swap adds a second container. Different things — the claims service is still one logical boundary in one app, and the gateway is transport. Worth an amendment because the *reasoning* is now load-bearing: 0006 rejected a second process partly over SQLite contention, which is exactly why ADR-0024 forbids the gateway touching the database.
  4. **I used the wrong heading convention, eight times.** `docs/adr/README.md` documents `## Amendment (YYYY-MM-DD) — <what>`; I wrote `## Addendum — <date>: <what>`. The convention was written down 2026-07-25 precisely because it had been practised for weeks and recorded nowhere. Conformed. Recording the slip rather than just fixing it, since silently diverging from a documented convention is how it stops being one.
  5. **An answered open question still read as open.** "Which model for the agent?" was answered by ADR-0023's cuts. Struck, with the half that *is* still open sharpened: ADR-0017 requires end-to-end verification of every model in a chain, the gateway's chain lives outside this repo, and that requirement currently has no owner — sharpened further by the live finding that a config-valid model id can still fail with `model_not_found`.

  Nothing here changed a decision. All five were the trail describing itself inaccurately, which is the failure mode the checklist exists to catch and the one that is invisible from inside the work.
- [x] 8.1 **ADR-0024 written.** OpenClaw gateway as the shell, Python as the domain. **Records D12, not D2** — D2 was superseded the same day and an ADR citing it would freeze a retracted architecture.
- [x] 8.2 **ADR-0025 written.** Where the proposal gate lives. **Stale as written** — it said "in the MCP server", which D3's split-by-origin decision (Justin, 2026-08-01) has since changed. The ADR records the split: card taps commit behind `/internal`, chat-initiated proposals commit in the MCP surface, both inside Python. Supersedes the `telegram_bot._execute_action` location in ADR-0016.
- [x] 8.10 **ADR-0023 written** — the agent tool allowlist serving security and feasibility at once, with the measurements, the config key two guesses got wrong, and the method note about reading totals off provider errors. Linked from `docs/adr/README.md`.
- [x] 8.3 **Addenda appended to ADR-0009 and ADR-0017** — appended, not edited, since both are accepted. ADR-0017's says plainly that the per-day budget is untouched by the per-minute work, with a two-row table separating the limits. Amend ADR-0009/0017: what the gateway covers, what `llm.py` keeps, and the daily-budget classification gap found in 0.4. **ADR-0009's Groq default survives** — 17.8 cleared the ceiling that threatened it, so this amendment is now narrower than expected. ADR-0017's per-day budget is untouched by any of it; say so explicitly, because "the token problem is solved" will otherwise be read as covering both.
- [x] 8.4 **Addenda appended to ADR-0014 and ADR-0015.** 0014: kept deliberately, dataset job is the deciding reason, two runtimes mean two versions. 0015: supervision moves to the container boundary but the dead-channel *guarantee* must not go with it, and the alerting levels are unchanged — already threatened once by the platform logging a failure on every successful caption edit. Amend ADR-0014/0015: `telegram_messages` retained and why; where dead-channel supervision now lives.
- [x] 8.5 **Addendum appended to ADR-0002.** Only the "single Docker Compose service" half is superseded; Python/FastAPI/SQLite stand. Names the three costs — two configs, two versions, a deploy that can half-succeed — and records APScheduler's displacement. Amend ADR-0002: the stack is now two runtimes. Supersede the reasoning, do not delete it.
- [x] 8.6 **Done: ADR-0003 addendum + BACKLOG entry.** The addendum says the original reason has expired and that nobody has retaken the decision, so it is flagged rather than superseded — nothing has replaced it. **DECIDED (Justin, 2026-08-01): reminders-and-push is a separate change after this one, unless it turns out to make sense to do now.** My read is that it does not: it is independent of the transport swap and becomes trivial once the gateway exists, so folding it in buys nothing but scope. Record against ADR-0003 that its original reason (no push channel existed) has expired, so the next reader sees a live question rather than a settled decision — and open a BACKLOG entry so it is not lost.
- [x] 8.7 **Done 2026-08-01, and written as current-state plus in-flight rather than as though the swap had happened** — the same trap as 8.9. `README.md` gains an "In flight" section saying plainly that nothing below it has changed, a gateway row in the third-party table marked *not yet*, and the new ADRs in Docs. `CONTEXT.md` gains three vocabulary entries, the first of which is the **name collision** — after the swap "OpenClaw is down" means two different outages, so the doc that governs language is where that belongs. The module map gains `gateway-workspace/*.md` (not code, injected every turn, nothing enforceable in them) and marks `telegram_bot.py` as slated for deletion but live and unchanged until the atomic cutover. Root `CLAUDE.md` gains the six things that cost a day each and cannot be recovered from the product's prose. Original: Update root `CLAUDE.md`, `app/openclaw/CLAUDE.md` module map, `CONTEXT.md` and `README.md`: new module boundaries, the two-runtime deploy, the internal endpoint, and the tool-inventory boundary.
- [x] 8.8 **Live versus coded, as of 2026-08-01.** The project's rule is that a plausible assumption is worth nothing until real data breaks it or fails to; this section says which side of that line each claim sits on, so a reader does not inherit confidence nobody earned.

  **Verified live against the running gateway and Justin's real Telegram** — these were done, observed, and in several cases photographed:
  - A `command` button dispatches through core's command path and a plugin-registered command runs. A `callback` action injected from outside is discarded.
  - An **unregistered** command in a button reaches the agent as a chat turn and spends tokens — three of them, in Justin's chat, from the `/ping` probes.
  - The 58-byte command budget: 58 renders both buttons, 59 renders one, no error either way. Confirmed on his screen.
  - A document (PDF) caption edits correctly, and the default path logs `editMessage failed` on the successful edit.
  - A presentation's `text`/`context` blocks never render through the CLI — Justin reported the missing table before I noticed it.
  - Media sends are refused from `/tmp` and accepted from `<stateDir>/media`; the allowlist is a default, not something configured here.
  - Turn size: 22,810 stock → 20,616 with authored workspace files → 5,355 with a tool allowlist → 3,865 with skills removed. Every reading with a fresh session key.
  - Groq works as a custom OpenAI-compatible provider; a config-valid model id can still fail at runtime with `model_not_found`.
  - `dmPolicy` default hands an unrecognised sender a live pairing code (Justin saw it first).
  - `BotCommandScopeChat` overrides the gateway's default-scope menu — set, read back, and confirmed on his phone as five entries.
  - The stock agent interviews the user about its own name before working, and claimed to have checked mail in a runtime holding no mail credential.

  **Read from the product's shipped source, not executed** — high confidence, but a different kind:
  - `editMode: "auto"` tries `editMessageText` first and falls back on an English error string (`network-errors.ts:275`).
  - `sanitizeTelegramCallbackData` returns `undefined` over 64 bytes and the button is filtered out (`approval-callback-data.ts:21`).
  - The presentation contract has five block types and no table (`normalizePresentationBlock`).
  - Plugin approvals take free-text `title`/`description`; exec approvals are a fixed template (`approval-view-model`).
  - `commands.native` gates plugin commands too (`bot-native-commands.ts:1056`) — which is why the menu is all-or-nothing.
  - Gateway cron runs missed jobs at startup, capped at 5 per restart, agent jobs deferred two minutes (`planStartupCatchup`).
  - The failover classifier has one `rate_limit` bucket and treats it as transient (`model-fallback-*.js:46`).
  - The media allowlist roots (`buildMediaLocalRoots`), and that `localRoots: "any"` disables the check entirely.

  **Coded and hermetically tested, but never run against the gateway:** `internal_api.py` (secret guard, per-job lock, correlation ids), `gateway_client.py` (argv construction, `record_outbound`, exit-code capture). The CLI flag names inside `_argv` are the specific risk — they are one function precisely because they were unverified when written, and several sibling assumptions in this change have since been wrong.

  **Asserted from documentation only, and still unproven:** that the in-gateway plugin can register the app's commands via `api.registerCommand` in a way a `command` button reaches. The claims-spike plugin proved a plugin *can* register a working command; it has not proved the full `/mark 7 sent` → `/internal` → claim-logic path. **This is the single largest unverified assumption in the design**, and 16.2 is where it gets settled.

  **Not verified and not verifiable here:** anything about behaviour under Justin's real message volume, concurrent taps, or a gateway restart mid-handler. The replay guarantees (ADR-0014) are carried forward on the strength of the existing implementation, not re-proved against the new transport.
- [ ] 8.9 Sync the delta specs into `openspec/specs/` before archiving. **Now five modified and three new**, not four and three — `task-capture` was added 2026-08-01, see below. **Runs twice, once per slice** — 8.11 assigns every capability and, where one straddles, every requirement.

  **Deliberately not done yet, and that is correct.** `openspec/specs/` is the *current-state* baseline and this change is 56 of 181 tasks. Syncing now would assert the system already does things nobody has built. The CLAUDE.md warning is about not forgetting this at archive, not doing it early.

  Reviewing the sync surface 2026-08-01 turned up three things:
  - **`task-capture` was affected and had no delta.** Its confirm-before-commit requirement *is* the D3 gate, which split by origin, and it referenced the old `telegram_bot._execute_action` location. Delta written now, on Justin's call, while the reasoning is fresh — the alternative was rediscovering it months later during the sync itself.
  - **The baseline already references a capability that does not exist** — `task-telegram-surface`, from `task-capture`. Pre-existing, created by another change's incomplete sync. Justin's call: note it, fix elsewhere. Logged in `openspec/BACKLOG.md` with the two possibilities and which to check first.
  - **A delta stated a wrong reason**, corrected in place: `reminder-scheduling` claimed cron has no `misfire_grace_time` equivalent. It has `planStartupCatchup`. The app-side sweep is still required, but because cron guarantees the *invocation* fires and not that the *app processed it* — and someone reading the old reason would reasonably have deleted the sweep on discovering catch-up.
- [x] 8.11 **DECIDED (Justin, 2026-08-01): two slices.** Taken after 16.2 closed the last spike, which was his stated condition — slicing sensibly needed nothing left unknown. Original text: *Decide whether this change archives whole or in slices — after the section-0 spikes close. 181 tasks is a lot to hold un-synced. The cutover itself is atomic (one bot token, one poller), but the work before it is not: the internal API, the MCP surface and the preflight could ship and archive ahead of the transport swap, shrinking what has to be right on the day.*

  **The boundary rule, and it is the only one that matters.** A task is **slice 1** if it can ship while the Python app still owns Telegram exactly as it does today. A task is **slice 2** if it is only true once the gateway holds the bot token. Not "is it hard" or "is it related" — a slice archives by syncing its deltas into the current-state baseline, so the test is whether the requirement is *true* after that slice ships. A requirement that would describe a system nobody has built yet cannot be in slice 1, however finished its code is.

  **Slice 1 — "both runtimes up, the app still owns Telegram."**
  Sections **0** (spikes, closed), **1** (internal transport), **2** (MCP read tools), **7** (deploy, isolation, plugin pinning), **8** (docs and trail), **11** (fit audit, closed), **13** except 13.1c, **14** (media outbox), **15**, **16**, **17**, **18**, **19a**, **19b**, **19c**, plus the items in 9 and 10 that do not depend on the gateway carrying traffic.
  It ends with: two containers deployed, the gateway holding no bot token and no channel bound, the plugin loaded with its commands registered, the MCP read tools answering, the preflight failing a bad deploy, and the hermetic suite green. Telegram behaviour is bit-for-bit what it is today.

  **Slice 2 — "the cutover and everything downstream."**
  Sections **3** (proposals and the confirm gate), **4** (cutover), **5** (scheduling), **6** (deletion), **12** (consequences of conversation bypassing the app), **13.1c** (the per-chat command menu), and the remainder of 9 and 10.

  **Two placements that are not obvious and are deliberate:**
  - **MCP read tools are slice 1; the confirm gate is slice 2.** Read tools are exercisable through `openclaw agent` with no channel bound, so the requirement is true the moment they are registered. The gate needs a real tap on a real button, which needs the gateway polling. This splits `claims-mcp-surface` across both slices rather than holding it whole — worth the awkwardness, because holding it would drag the read tools into the risky day for no reason.
  - **13.1c is slice 2 even though the mechanism is proven.** Writing `BotCommandScopeChat` only means anything for a bot the gateway is driving. Verified live (see 13.1c), but its *requirement* is false until cutover.

  **How `openclaw-gateway-runtime`'s ten requirements split** — six become true at slice 1, four at slice 2. Recorded now because this is the reasoning that will be expensive to reconstruct at archive time:
  | Slice 1 | Slice 2 |
  |---|---|
  | In-gateway plugin owns the command surface | Gateway owns channel transport (sole poller) |
  | Agent turn size within the model's limits | The app reaches Telegram through gateway actions |
  | Workspace files shipped, versioned, unenforcing | Each runtime degrades visibly and independently |
  | Config that can silently regress asserted at deploy | Scheduled work runs from gateway cron |
  | Version stamping survives the second runtime | |
  | Deploy remains a single documented command | |

  `gmail-isolation-boundary` goes whole into slice 1 — the container boundary and the tool-inventory assertion both hold there. `telegram-bot`, `conversational-agent`, `llm-backend`, `reminder-scheduling` and `task-capture` go whole into slice 2; every one of them describes behaviour that only exists after the swap.

  **What this costs, stated plainly.** Two changes to keep coherent instead of one, and a decision trail split across two directories at the point where it is densest. The mitigation is that slice 1 carries the trail — the spikes, the eight silent-failure modes, the token measurements, the ADRs — and slice 2 references it rather than restating it.

  **The directory split happens at slice 1's archive, not now** (see 8.14). Doing it early would mean maintaining two `tasks.md` files through the remaining build for no benefit; the assignment above is the part that needed deciding while the reasoning was fresh.
- [ ] 8.14 **Added by 8.11 — the mechanics of the split, to be done when slice 1's tasks are complete and not before.**
  - This change (`openclaw-gateway-core`) becomes **slice 1**. Narrow its `proposal.md` scope statement to match, keeping the full reasoning; do not rewrite history to pretend it was always scoped this way.
  - Carve slice 2 into a new change — working name `openclaw-telegram-cutover` — moving sections 3, 4, 5, 6, 12, 13.1c and the deferred 9/10 items across, along with the five whole capability deltas and the four `openclaw-gateway-runtime` requirements named above.
  - Slice 2's `design.md` references this one's decisions (D1–D12, ADR-0023/0024/0025) rather than copying them. A copied decision trail diverges.
  - 8.9's sync then runs **twice**, once per archive, each syncing only that slice's requirements.
- [x] 8.12 **The agent workspace files need no capability of their own** (Justin, 2026-08-01). `openclaw-gateway-runtime` already requires them shipped from the repo, versioned with the app, and carrying no enforceable rule; a separate capability would restate that. Recorded so the question is not reopened as an oversight.

## 16. Rework forced by D10 (the CLI cannot send interactive messages)

- [ ] 16.1 ~~Supersede `gateway_client`'s role~~ **— void, D10 retracted.** `gateway_client` keeps the full outbound role including cards with buttons. Fix its `_argv`/presentation construction to emit `presentation.blocks[{type:"buttons"}]`, and add a test asserting the payload passes the platform normalizer rather than asserting our own shape. Original text: It stays valid for plain text and media and for caption edits; it CANNOT carry buttons. Every notify path that renders a card with buttons moves to the plugin. Keep the module and its one-seam logging property; narrow its documented scope.
- [x] 16.2 **CONFIRMED END TO END, 2026-08-01. D12 holds. This was the largest unverified assumption in the design and it is now verified.**

  Proven chain, with a real tap by Justin on a real phone:

  ```
  button {action.type:"command", command:"/mark 7 sent"}
    -> core native command path
    -> plugin handler registered via api.registerCommand("mark")
    -> HTTP POST to the Python app, X-OpenClaw-Secret + X-Correlation-Id
    -> 200, and the reply lands back in the chat
  ```

  **Corroborated from both ends.** The reply Justin received reads `CHAIN OK / command: /mark 7 sent / app status: 200 / app said: {"ok":true,"route":"echo","correlation_id":"tg-mark-x","client_host":"127.0.0.1"}`, and the Python process independently logged `ECHO HIT correlation=tg-mark-x`. The same id, minted once inside the plugin, observed on both sides of the hop — two independent observation points rather than one, which is what distinguishes this from every earlier result in this change that turned out to be a misread.

  **The evidence is a correlation id, not a screenshot.** The Python side logged `ECHO HIT correlation=tg-mark-x`. That id is minted inside the plugin's own handler (`tg-${name}-${messageId}`) and exists nowhere else, so the request cannot have come from anything but a plugin-dispatched command. A tap arrives at the gateway as `Inbound message … (direct, 12 chars)` — `/mark 7 sent` is exactly 12 characters — and is routed onward to the plugin's command.

  The Python half used the **real** `internal_api` guard rather than a stub: correct secret → 200, wrong secret and missing secret → `{"error":"rejected"}`. Reproduce by standing up `internal_api.router` plus one echo route on `:8977` against a scratchpad DB copy, pointing the plugin at `host.docker.internal:8977`.

  **Three findings that fall out of it:**
  - **`INTERNAL_API_ALLOW_HOSTS` is weaker than the code's comment claims.** `client_host` reads `127.0.0.1` even for calls originating in the container — Docker Desktop NATs them to loopback — so the allowlist cannot distinguish the gateway from any other process on the host. It blocks off-host callers and nothing finer. **The shared secret carries essentially the whole boundary**, which makes 19b's assertion that the secret is set and non-blank more load-bearing than it looked. Amend the comment in `internal_api.py` when 1.x is built.
  - **`ctx.messageId` is not populated** in a command handler — the correlation id came through as `tg-mark-x`, the fallback. Correlation across the gateway→app hop (9.1) needs its id from somewhere else; find the field that survives before designing on it.
  - **`registerInteractiveHandler` returns `undefined`**, logged on every earlier boot. Consistent with 16.9 and now moot, since the card interface uses no callbacks.

  **Method note, and it cost a round trip.** My first instrumentation logged only *rejections* — the echo route skipped `_run()`, which is what logs successes, and uvicorn was at `log_level="warning"` besides. A working tap and no tap looked identical, and Justin's first tap **had** in fact worked; I simply could not see it. Exactly the failure my own rule names. Instrument the success path before concluding anything from a quiet log.
- [x] 16.3 **ANSWERED live 2026-08-01. `command` works; `callback` is inert without a plugin.**
  - Tapping a `{"action":{"type":"command","command":"/status"}}` button **invoked the slash command** and the gateway replied. Deterministic, no model in the path, no plugin required.
  - Tapping `{"action":{"type":"callback","value":"reject:7"}}` with nothing registered did **nothing at all** — no reply, no error, no log.
  - **This corrects an earlier alarm of mine.** I warned, from the docs, that an unclaimed callback is handed to the agent as text `callback_data: <value>`, which would put a model in the path of a commit token. That did not happen — it was simply inert. The risk in 9.10 may be smaller than stated, or conditional on config not set here. Re-verify before treating 9.10 as urgent; do not treat my earlier framing as established.
  - **Design consequence:** build the card interface on `command` actions wherever a tap can be expressed as a slash command — `/mark 7 sent`, `/pet 7 Aari`, `/resolve 7`. That reuses the existing command surface, needs no plugin, and keeps the LLM out entirely. A plugin is needed **only** for taps that cannot be a command string.
- [x] 16.7 **BUILT AND RUNNING 2026-08-02 — `app/gateway-plugin/`, loaded live, all five commands registered, the chat menu claimed, the boot report landing in the app.** Gateway log: `[plugins] claims registered: mark, pet, resolve, history, actions` / `[plugins] claims: chat command menu set to 5 entries`, and `/health.gateway_plugin` carries the five names. Full preflight is nine PASS.

  Handlers forward to `/internal/command/<name>` and render what comes back; no claims logic in the plugin, per the spec's own scenario. That endpoint is slice 2's — until then a handler surfaces the app's real 404 rather than pretending, which is the honest failure and needs no code change when the endpoint arrives.

  **A fourth silent-ish gate, and this one at least shouts.** Beyond `definePluginEntry` and `plugins.entries.<id>.enabled`, a plugin directory needs **`openclaw.plugin.json`** *and* a `package.json` carrying `"openclaw": {"extensions": ["./index.js"]}`. Missing manifest fails config validation loudly — the one gate in this area that does.

  **The docs' import line does not work from a `plugins.load.paths` directory.** `docs/plugins/message-presentation.md` and friends say to import from `openclaw/plugin-sdk/...`; tried live, that is `ERR_MODULE_NOT_FOUND`, because the plugin sits outside the app's module resolution. The absolute container path `/app/dist/plugin-sdk/plugin-entry.js` is what works. Product docs losing to product behaviour again, exactly as the project rule predicts.

  **TWO OF MY OWN GUARDS WERE FALSE PASSES, and both were caught by going and looking rather than by reading a green line.**

  1. **`registerCommand` does not report a collision to the caller.** With the old spike plugin still loaded, the gateway logged `command registration failed: Command "mark" already registered by plugin "claims-spike"` — **1.2 seconds after `register()` returned**, asynchronously. The plugin had already reported all five to the app. So the boot report proves the plugin **ran**; it cannot prove the plugin **owns** the names. Fixed by having the preflight read the gateway's own log for those failures, and by saying so in the plugin's docstring so nobody upgrades the claim later.
  2. **That new log check then passed over three real collisions.** The gateway's log *file* is JSON per line, so the name arrives as `\"mark\"`, and a pattern matching `"mark"` found nothing. The check went green while the failures sat in the file. Caught only by disbelieving the PASS and reading the log directly — the project's own "a silent result is not a finding" rule, applied to my own output.

  **Also found: the isolation check was passing on a container holding `GOOGLE_API_KEY` and `GEMINI_API_KEY`.** 19b.5 says no Google key with a Gmail scope, and from outside you cannot show a bare `GOOGLE_API_KEY` lacks one. The agent runs on Groq, so any Google credential there is unnecessary *and* the same credential family as the Gmail token the boundary exists to exclude. `FORBIDDEN_GATEWAY_KEYS` widened to `GMAIL`, `GOOGLE`, `GEMINI`, `DATABASE_PATH`; the spike container was recreated without them and the agent still serves turns.

  Original: **A plugin is required after all** — correcting 16.3. `command` actions invoke *native* slash commands; `/mark`, `/pet`, `/resolve` are this app's, not the gateway's. The plugin must register them (`api.registerCommand`) and claim callbacks. It does **not** need to own outbound rendering — that stays with `gateway_client`.
- [x] 16.9 **`callback` actions sent from the CLI go nowhere — evidenced, with a stated limit.** Tapped with a working model (`groq/llama-3.3-70b-versatile`), the sender allowlisted, and `dmPolicy=allowlist`: no reply, no log line, and `channel_ingress_events`, `command_log_entries` and `diagnostic_events` all hold **zero rows**. The `callback_query` string in `openclaw.sqlite` is schema text, not data.
  Best explanation consistent with everything seen: the Telegram plugin encodes and routes the callbacks **it** creates (approvals, native commands); an arbitrary opaque `value` injected from outside has no registered owner and is discarded. That matches the docs — callback actions "carry opaque plugin data through the channel's interaction path, meaning channel plugins handle the interaction".
  **Limit of this finding:** it is a negative result. It cannot distinguish "discarded by design" from "misconfigured in a way we did not find". Proving it needs the plugin (16.2), which would also make the case moot by registering a handler. Do not spend more time on the negative.
  **Practical consequence, which is unchanged either way:** use `command` actions where a tap can be a slash command; build the plugin for anything else.
- [x] 16.8 **9.10 CONFIRMED live 2026-08-01, and by a route nobody predicted.** The mechanism is not the unclaimed *callback* — it is an unregistered ***command***.
  Both 18.5 probe messages carried a second button whose action was the command `/ping`, chosen only because it was short. `/ping` is not a native command and no plugin has registered it. Tapping it did not error and did not no-op: it **reached the agent as a chat turn**, which replied *"You've sent a `/ping`, and I see your previous messages…"* and spent tokens. Justin's screenshot shows three such turns.
  So the hazard 9.10 guards against is real and its trigger is broader than assumed. A `command` button is only deterministic **while its command is registered**. A typo, a plugin that failed to load (18.7's two silent gates), or a command renamed on one side turns every tap on that button into a model turn — with the tap's own token in the prompt. `/mark 7 sent` reaching an LLM as free text is precisely the commit-token-through-a-model path D12 exists to prevent.
  **Consequences, all of which are now requirements rather than precautions:**
  - 9.10's startup assertion must cover **commands**, not just callbacks: assert every command a button can emit is registered, and fail the deploy otherwise. Add to 19b.6, which currently only proves *a* command responds.
  - Anything arriving at the agent that parses as one of the app's command strings must be treated as an error and refused, never answered. It means the deterministic path broke.
  - The earlier reading in 16.3/16.9 — that an unowned interaction is silently inert — is now known to be **false for commands and unverified for callbacks**. Do not generalise from the callback result.
- [x] 16.6 Native rich messages enabled (`channels.telegram.richMessages=true`) so the 11.3 cards-vs-tables comparison is fair.
- [x] 16.5 **Answered by 18.5: a `command` action has the same 64-byte ceiling, minus a 6-byte prefix.** The risk named here was real. Condition buttons must keep carrying an index, not the text — but the conclusion drawn from that ("condition selection still needs `callback` plus a plugin handler") does **not** follow: an index-carrying command such as `/setcond 7 3` is 13 bytes and fits easily. No tap identified so far needs a `callback` action. Revisit only if a tap appears that cannot be named in 58 bytes.
- [ ] 16.6 Native rich messages are **off by default**: `channels.telegram.richMessages=true` enables "tables/details/rich media" (seen in `/status`). This is the feature 11.3 compares against Pillow cards — enable it before that comparison, or the test is unfair. A `command` button invokes a slash command, which maps onto the existing `/mark`, `/pet` surface with no token routing. If it holds, most of the bespoke callback bridge disappears.
- [x] 16.4 **ANSWERED live 2026-08-01: a document caption edits fine, but the default path logs a failure on every success.**
  - Sent a real PDF as a document, then edited it. The edit applied. Container log, on the successful edit: `[telegram] editMessage failed: Call to 'editMessageText' failed! (400: Bad Request: there is no text in the message to edit)`.
  - Reading `extensions/telegram/src/send.ts` and `action-runtime.ts` explains it. The `editMessage` action accepts both `content` and `caption`, and sets `editMode: caption != null ? "caption" : "auto"`. `"caption"` calls `editMessageCaption` directly. `"auto"` calls `editMessageText` first and only falls back to `editMessageCaption` after Telegram returns *there is no text in the message to edit* (`MESSAGE_HAS_NO_TEXT_RE`, `network-errors.ts:275`).
  - The CLI's `message edit` has **no `--caption` flag**, so every CLI edit of a document or photo takes the failing path: one wasted Telegram round trip and one error-shaped log line per successful card update.
  - **Consequence for `gateway_client.edit_message`:** send `caption` explicitly whenever the target message carries media, rather than relying on the `auto` fallback. Not for correctness — `auto` works — but for the failure-visibility rule. A log that says `editMessage failed` on every successful tap is how a real edit failure becomes invisible.
  - Second-order: the fallback is keyed to an **English Telegram error string**. It is a regex over `there is no text in the message to edit`. Passing `caption` explicitly does not depend on it; `auto` does.
  - Confirmed on the phone: the PDF renders as a document card with the edited caption beneath it and Telegram's own `edited` marker. This is the shape `_append_result` needs for review alerts, and it survives the transport change.

## 15. Unauthenticated disclosure to unknown senders (found 2026-08-01, live — Justin flagged it)

- [ ] 15.1 **The gateway auto-replies to any unrecognised sender with a pairing kit.** Verbatim, to an unauthorised user: `"OpenClaw: access not configured. Your Telegram user id: <id>. Pairing code: UV7NHR3N. Ask the bot owner to approve with: openclaw pairing approve telegram UV7NHR3N"`. Telegram bots are discoverable by username, so any stranger who finds the bot learns it runs OpenClaw, receives a live pairing code, and is handed the exact phrasing to socially-engineer the owner into approving them. Justin raised this unprompted on first sight.
- [ ] 15.2 Find the setting that suppresses or blanks that reply and make silence the default before the gateway touches the real bot. If no such setting exists, that is a blocking finding, not a preference — the app's current behaviour is to ignore an unauthorised sender entirely and log the rejection, disclosing nothing.
- [x] 15.3 **Closed by Justin, 2026-08-01: standard platform behaviour, not a finding for this change.** 19b.4 already asserts `dmPolicy == "allowlist"` at deploy, which is the control that matters.

## 14. Media can only be sent from an allowlisted directory (found 2026-08-01, live)

- [ ] 14.1 **The gateway refuses to send media from an arbitrary path**: `OutboundDeliveryError: Local media path is not under an allowed directory: /tmp/spike-card.png`. Sending the same file from `/home/node/.openclaw/workspace/` succeeded. Good security control, and it **breaks an assumption in D2**: `gateway_client.send_file(path)` was designed to hand the gateway a path on the *app's* filesystem, which will never be allowlisted — and cannot be, because 7.0a deliberately denies the gateway any sight of `app/data`.
- [x] 14.2 **BUILT AND PROVEN BOTH WAYS, 2026-08-02.** `app/openclaw/media_outbox.py` plus a `media_outbox` volume: `/data/outbox` in the app, `<stateDir>/media` read-only in the gateway. `gateway_client.send_card(target, png_bytes, …)` keeps the existing byte-passing signature, so the cutover diff stays small — the file-on-disk step is an artefact of the CLI wanting a path, not a caller's problem.

  **The real shape of the problem is two path spaces for one file.** The app writes `/data/outbox/probe-<rand>.png`; the gateway must be told `/home/node/.openclaw/media/probe-<rand>.png`. `publish()` therefore returns the *gateway's* path, so a caller cannot hand the CLI one it will refuse.

  **Verified with a real card, both directions:**
  - Rendered 21 live claims through `claim_card.render` (161 KB), published through the outbox, sent with a `command` button: delivered, message id 42. Buttons on a photo still work (re-confirms 0.2).
  - The same file named by its **app** path: `OutboundDeliveryError: Local media path is not under an allowed directory: /data/outbox/probe-….png`. Loud, not silent — a rarity on this platform and worth noting as one.

  Two implementation details that are not style: files are written **write-then-rename**, because a truncated PNG sends "successfully" and arrives corrupt; and names carry a random component rather than a claim id, since they land in a directory the gateway can read. Expiry is swept on publish rather than on a timer — nothing reads these back once Telegram has its copy.

  Original: Decide how rendered artifacts reach the gateway, given the isolation decision. Options: a narrow shared "outbox" volume carrying only rendered cards and claim PDFs (not the data dir); an HTTP fetch by URL from the app; or base64/stdin if the CLI supports it. **The isolation choice and the media allowlist together mean there must be exactly one narrow path between the two containers — design it deliberately rather than discovering it at cutover.** Whatever wins, it must not become a general mount of `app/data`.
- [x] 14.5 **Through the CLI, a presentation's `text`/`context`/`divider` blocks never render. Only `buttons` and `select` survive.** Found 2026-08-01 when Justin reported "nothing showed for B" — the message arrived with its `--message` line and a working button, and the entire table silently absent. **Silent-failure mode #8**, and a different one from #7: the payload was valid, the send returned `ok:true`, and the platform rendered *part* of it.

  Cause, from `action-runtime.ts:238`: `presentationText` is computed **only when `explicitContent == null`**. Supplying `--message` sets `explicitContent`, which suppresses `renderMessagePresentationFallbackText` entirely; the presentation is then consulted for buttons alone. Omitting `--message` does not help — the CLI rejects the send outright with `OutboundDeliveryError: Message must be non-empty for Telegram sends` before that path is reached.

  So the two are mutually exclusive through the CLI, and the CLI insists on one of them. Net: text blocks are unreachable from `gateway_client`.

  **Consequences:**
  - Everything textual goes in `--message` as markdown. That works — a markdown table renders natively with `richMessages: true` — so nothing is lost in capability, only in structure.
  - This is a **CLI limitation, not a platform one**. The in-gateway plugin calls the API directly and can omit explicit content, so a plugin-sent message *can* use text blocks. Do not generalise this finding to the plugin path.
  - 19a.1 must widen: it currently asserts buttons are nested correctly. It should also assert that `gateway_client` never puts renderable content in a presentation text block, because doing so fails silently rather than erroring.
- [ ] 14.3 Note for whoever runs these commands from Git Bash on Windows: `/tmp/x` in a `docker exec` argument is rewritten to `C:/Users/.../Temp/x` by MSYS path translation, producing a misleading "not under an allowed directory" error naming a Windows path. Prefix with `MSYS_NO_PATHCONV=1`.
- [x] 14.4 **The allowlist is a default, and here is exactly what it contains** (`dist/local-roots-*.js`, `buildMediaLocalRoots`): the OpenClaw-preferred tmp dir, `<configDir>/media`, and `<stateDir>/{media,canvas,workspace,sandboxes}`. Nothing else, and `/tmp` is **not** among them — the 14.1 failure was the allowlist working, not a misconfiguration. Verified live: a PDF in `/tmp` was refused, the identical file in `<stateDir>/media` sent.
  Two consequences for 14.2, which narrow the option set rather than settling it:
  - The shared-outbox option must mount **into one of those roots** (`<stateDir>/media` is the natural one). Mounting the app's render directory anywhere else on the gateway filesystem does nothing — the allowlist ignores mounts it does not know about.
  - `localRoots` can be set to the string `"any"`, which disables the check outright (`assertLocalMediaAllowed` returns immediately). Do not. Record it here so nobody later "fixes" a path error by reaching for it: it is the whole control, and it is one word.
  - 19b should assert the resolved roots do not include `"any"` and do not include the data dir, for the same reason the other deploy-time assertions exist — it is configuration, so no app-side test can see it.

## 13. The gateway's own Telegram surface (found 2026-08-01 running it live)

- [x] 13.3 **The stock agent has its own onboarding personality, and it talks to Justin unprompted.** Three consecutive replies in his chat asked him to name it, pick its "creature" type and its "vibe", choose an emoji, and confirm its pronouns, so it could write `IDENTITY.md`, `USER.md` and `SOUL.md` — driven by a `BOOTSTRAP.md` it says it must complete "before I can respond normally".
  **Answered: yes, these are shipped complete rather than interviewed for, and they are ordinary files.** The gateway seeds seven markdown files into a new workspace and injects them into the system prompt every turn (`injectedWorkspaceFiles` in `systemPromptReport`). Nothing is interactive about them; the interview happens only because `BOOTSTRAP.md` is present and tells the model to run one. Its own last instruction is *"When you are done, delete this file."*
  So the work is: author `IDENTITY.md`, `USER.md` and `SOUL.md` in the repo, ship them into the workspace at deploy, delete `BOOTSTRAP.md`, and set `agents.defaults.skipBootstrap` so an upgrade cannot re-seed the template versions. Version them with the app — they are prompt content, and a silent edit to `SOUL.md` changes behaviour with no code change and no diff anyone reviews.
  **What goes in them is narrow.** They are prompt text, so nothing that must hold goes here: the two harness refusals, the proposal gate and the no-send rule stay in code (11.0), for exactly the reason 13.4 demonstrates. These files carry tone, how to address Justin, the `#id`-in-every-message convention, and the pets/timezone context — the things whose worst failure is awkwardness rather than a wrong claim.
  Sizes are also a token line-item: 14,341 chars of the 22,810-token turn is these files, `AGENTS.md` alone 8,654. See 17.9 — replacing `AGENTS.md` is the second-largest cut available.
- [ ] 13.4 **The stock agent asserted a mailbox check it cannot have performed** — *"No urgent emails or calendar events"* — in a runtime with no Gmail credential and no calendar. This is the exact failure ADR-0016 and the `conversational-agent` spec exist to prevent ("the agent never claims mailbox access it does not have"), reproduced by the platform's own default agent on its first contact. It is strong evidence that the tool-inventory enforcement in 2.3 / 19a.7 cannot be relaxed to prompt-level discipline: the stock prompt does not hold this line.

- [ ] 13.1c **CONTRADICTION FLAGGED — the option Justin chose does not exist as configuration.** He asked for the app's five commands plus `/status` and `/models`. There is no per-command menu allowlist. `commands.native` is a single boolean for the entire native command surface, and disabling it excludes plugin commands from the catalog too (`bot-native-commands.ts:1056`, `...(nativeEnabled ? pluginCatalog.commands : [])`) — which would break every `command` button, the whole basis of D12. So the achievable choices are **all ~60 commands, or none plus a broken button path.**

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
- [x] 13.1 **SUPERSEDED by 13.1c — the choice recorded here is not configurable.** Original: keep the app's five commands plus `/status` and `/models`, prune the rest.** He wants to diagnose the gateway from the phone without opening a terminal, which is why this is not the clean "prune everything of theirs" I recommended. Original finding follows.
- [ ] 13.1a **The gateway registers 61 slash commands on the bot**, and logged *"menu text exceeded the conservative 5700-character payload budget; shortening descriptions to keep 61 commands visible."* Justin's bot today offers a handful — `/mark`, `/pet`, `/history`, `/actions`, `/start`. After cutover his command menu is largely OpenClaw's. This is a direct cost to "don't lose the Telegram UI I built" and was on nobody's list. Decide: prune the gateway's command set, or accept the menu changing shape. Check first whether the app's own commands can coexist or are displaced.
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
  - **Confirmed on the phone, not just in the source.** Justin's screenshot: the 58-byte message shows both buttons (`CMD-58`, `SHORT-ok`); the 59-byte message shows only `SHORT-ok`. No error, no placeholder, no gap — the button is simply not there. Anyone seeing only the second message would read it as a rendering bug or a missing feature, never as a payload-length problem.
- [ ] 18.6 **Gotcha to record in the module map:** `plugins list` / `plugins inspect` report `commands: []`, `hookCount: 0` and `Shape: non-capability` for a plugin whose commands demonstrably work. Those fields come from the **persisted registry**, not live runtime, and go stale silently (`persisted-registry-stale-policy`). Never diagnose a plugin from them; test the behaviour.
- [ ] 18.7 **Two silent enablement gates** for a `plugins.load.paths` plugin: the entry must ALSO be enabled via `plugins.entries.<id>.enabled = true`, and the default export must be wrapped in `definePluginEntry` from `plugin-sdk/plugin-entry`. A plain object export loads without error and never runs. Neither failure produces a diagnostic.

## 17. Agent turn size: 22,810 stock, 5,355 after the cuts — Groq fits (resolved 2026-08-01)

**Current state:** a trimmed turn is **5,355 tokens** against Groq free tier's 12,000 TPM, leaving ~6,600 for the claims tool inventory. See 17.8 for the measurements and 17.9 for the breakdown.

**This section's title used to read _"The default agent request is 23.5k tokens — Groq's free tier cannot run it"_.** That was the conclusion for most of the day and it was wrong. The superseded items are kept below, marked and unedited, because the *way* they were wrong is the reusable lesson: all three read a single total off a provider's error message. Nothing measured the composition until `openclaw agent --json` was tried.

- [x] 17.1 **(SUPERSEDED 2026-08-01 by 17.8 — the blocker was cleared by cutting the surface. Original text kept verbatim.)** **Hard blocker, not a tuning issue.** A single agent turn on a stock gateway measured **23,438–23,513 tokens** against Groq free tier's **12,000 TPM**. Verbatim: `413 Request too large ... on tokens per minute (TPM): Limit 12000, Requested 23438`. This is not exhaustion after heavy use — one turn is ~2x the per-minute ceiling, so it can **never** succeed no matter how long you wait. For comparison this repo's own chat request is ~2.6k tokens (`config.py`): **the gateway agent's turn is ~9x larger**.
- [x] 17.2 **(PARTLY SUPERSEDED 2026-08-01 by 17.8. The instinct was right — tool schemas were 31,972 of 33,774 chars and are the largest single cost. The conclusion "adding claims tools makes it worse" now has a number: ~6,600 tokens of headroom, not unlimited. What is superseded is only the implied hopelessness. Original text kept verbatim.)** **Justin's D8 instinct was righter than my answer.** He asked whether MCP would burn tokens; I said the tool inventory is a per-turn tax but framed it as manageable. Measured, the default surface alone breaks the provider this project standardises on. The cause is everything shipped on every turn: 45 loaded plugins' tool schemas plus **61 registered slash commands**. Adding claims tools makes it worse, not better.
- [x] 17.7 **(SUPERSEDED 2026-08-01 by 17.9 and 17.8. The half about plugins holds — they are not the lever. The "~29k floor" does not: it was a total read off a 413 error, and the itemised turn is 22,810 stock and 5,355 trimmed. Original text kept verbatim, including its own method note, which caught one measurement error and missed a larger one.)** **MEASURED: plugins are NOT the lever. The floor is ~29k tokens.** Using Groq's `413 ... Requested N` as a measuring instrument via `openclaw agent --agent main --session-key <fresh>`:
  | Surface | Fresh session | Requested tokens |
  |---|---|---|
  | 45 plugins, 61 commands | no (accumulated) | 22,560 |
  | 1 plugin (`plugins.allow`) | no (accumulated) | 30,159 |
  | 1 plugin (`plugins.allow`) | **yes** | **28,991** |
  Cutting 44 plugins did **not** reduce the turn — a minimal surface measured *higher* than the full one. The bulk is the core agent prompt, core tools and skills (doctor reports 14 eligible), none of which `plugins.allow` touches. **Conclusion: Groq free tier's 12k TPM cannot run the OpenClaw agent, and no amount of plugin pruning changes that.** Gemini handles it comfortably (1M context) and is what the spike now uses.
  **Method note, twice-learned:** the first two rows are contaminated by session history — `agent:main:main` accumulates turns, so the numbers measure conversation, not surface. Always pass a fresh `--session-key`. Reporting "disabling plugins made it worse" from row 2 would have been a false finding.
- [x] 17.9 **The turn is itemised, and 17.7's "~29k floor" was wrong.** Stop using Groq's 413 error as the instrument — `openclaw agent --json` returns a `systemPromptReport` that breaks the prompt down by source. Fresh session key, stock spike, Gemini: **22,810 prompt tokens** for the message `hi`.

  | Component | Chars | Note |
  |---|---:|---|
  | Tool schemas | **31,972** | the single largest item |
  | System prompt total | 33,774 | = project context 14,519 + core 19,255 |
  | — injected workspace files | 14,341 | AGENTS.md 8,654 · SOUL.md 1,797 · BOOTSTRAP.md 1,510 · TOOLS.md 910 · IDENTITY.md 693 · USER.md 534 · HEARTBEAT.md 243 |
  | Skills | 4,206 | 13 skills: clawhub, meme-maker, weather, notion, diagram-maker, taskflow … |

  **What this corrects:** 17.7 concluded there was a ~29k floor that pruning could not move. That conclusion came from a blunt instrument — a total, with no breakdown, read off a provider error. Itemised, the turn is 22.8k and three-quarters of it is content we choose: tool schemas, workspace files and skills. **None of it is the "core agent prompt" that 17.7 blamed** — the non-project system prompt is 19,255 chars, under a third of the total. The floor is real but far lower than recorded, and 17.8's untested lever is now the *main* lever, not a fallback.

  Three cuts, in order of size, none of which needs a provider change:
  - **Tool schemas (31,972 chars).** `agents.defaults.contextPruning.tools.allow`. Our agent needs the claims read tools and nothing else — no filesystem, shell or browser tool, which 19a.7 and `gmail-isolation-boundary` already require to be absent for security. The security requirement and the token requirement want the same thing.
  - **AGENTS.md (8,654 chars).** Its headings are `Group Chats`, `Know When to Speak!`, `React Like a Human!`, `Heartbeats — Be Proactive!`, `Heartbeat vs Cron`, `Memory Maintenance`. A single-user DM assistant with deterministic taps needs approximately none of it. Replace, don't trim.
  - **Skills (4,206 chars).** Thirteen loaded, zero relevant. `meme-maker` and `weather` are on every claims turn.

  Two mechanical notes: `bootstrapMaxChars` is 20,000 per file and 60,000 total, so workspace files are capped but nowhere near it; and `systemPromptReport` names every contributor with its size, so **19b.1 should assert the itemised report rather than a single total** — a regression in one component is invisible in a sum.
- [x] 17.8 **CUTS MADE AND MEASURED 2026-08-01. 22,810 → 5,355 tokens. Groq is viable after all.**

  | Step | Prompt tokens | Tool schema chars | Workspace file chars |
  |---|---:|---:|---:|
  | Stock | 22,810 | 31,972 (32 tools) | 14,341 (7 files) |
  | + authored workspace files | 20,616 | 31,972 | 6,508 (6 files) |
  | + tool allowlist | **5,355** | **304 (1 tool)** | 6,508 |

  The config key is **`tools.allow`** at the top level, not `agents.defaults.tools.allow` (rejected: `Unrecognized key: "tools"`) and not the `agents.defaults.contextPruning.tools.allow` the earlier note guessed at. Restart required. `alsoAllow` adds to the active profile; `allow` replaces it — `allow` is the one that cuts.

  **This overturns 17.1 and 17.2's conclusion.** Groq free tier's 12,000 TPM was recorded as a hard blocker that no pruning could clear. A trimmed turn is 5,355, leaving **~6,600 tokens of headroom** — which is the real budget for 19a.3, replacing "a declared maximum" with a number derived from the provider's limit rather than from taste.

  **Caveats, all load-bearing:**
  - Measured with `tools.allow = ["read"]` as a stand-in for "a short list". `read` is a filesystem tool and `gmail-isolation-boundary` forbids it; the shipped allowlist is the claims MCP tools instead. The 304 chars it contributed are noise, but the *shape* of the result would not change.
  - No claims tools existed at measurement time. The headroom is what they must fit inside, not free space.
  - 13 skills still contribute 4,206 chars, none of them relevant (`meme-maker`, `weather`, `notion`, `clawhub`…). Roughly another 1k tokens available, untaken.
  - The core system prompt is 18,536 chars and did not move. That is the actual floor, and it is ~4.6k tokens.
- [x] 17.10 **Skills removed 2026-08-01: 5,355 → 3,865 tokens.** `agents.defaults.skills = []`, restart required. Saved 1,490 tokens, half again more than the ~1k estimated, because the skills block costs more than its raw chars suggest.

  **Running total: 22,810 → 3,865, a 83% cut.** Against Groq's 12,000 TPM that is ~8,100 tokens of headroom for the claims tool inventory, up from the ~6,600 recorded in 17.8 and ADR-0023. Both figures should be read as "the budget grew"; the ADR's number is the conservative one and is safe to keep as the design constraint.

  Config keys that were rejected before the right one was found, recorded so nobody repeats the search: `skills.enabled`, `agents.defaults.skills.enabled` (expects an array, not an object) and `agents.defaults.skillsLimits` all fail validation.
- [ ] 17.3 **7.6 gains a second, harder justification.** Pinning the plugin set was a security requirement (`gmail-isolation-boundary`); it is now also a **feasibility** requirement. Measure tokens per turn after disabling unused plugins and pruning the command surface, and treat "turn size under the provider's TPM" as an acceptance criterion with a number, not an aspiration.
- [ ] 17.4 **DECIDED by Justin, 2026-08-01: make the cuts, then measure, then choose — do not choose now.** The cuts (17.8/17.9) happen regardless because `gmail-isolation-boundary` wants the same tool allowlist. Groq stays the preference if the trimmed turn fits 12k TPM; Gemini is the fallback; a paid tier is the last resort even though he has said cost is covered. Decide the provider for the agent against that number, not against preference. Options: cut the surface until Groq's 12k TPM fits; use a provider with a higher TPM; or accept a paid tier. Note this is **per-minute**, a different constraint from ADR-0017's per-day budget — both now apply.
- [ ] 17.5 **Observed failover behaviour, relevant to ADR-0017 and D8.** OpenClaw retried the *same* model 3 times (10s/20s/30s backoff) then surfaced `decision=candidate_failed reason=rate_limit next=none`. Correct shape for a per-minute limit, and it confirms the gateway classifies Groq 429/413s as `rate_limit`. Two notes: with one model configured there is nothing to fall through to, and retrying a `413 request too large` is futile by construction — the request size never changes, so all 4 attempts were unsatisfiable.
- [ ] 17.6 **Operational: the spike shares the production Groq key.** The gateway agent competes for the same free-tier quota as the live claims service. Give the gateway its own key, or accept that agent traffic can starve `invoice_matching`'s extraction calls.

## 11. Fit the OpenClaw architecture — audit before keeping anything bespoke

Justin's principle (2026-08-01): the solution fits OpenClaw, not the reverse. Keep what this repo built **only** where the trade-off of losing it is real; the burden of proof is on keeping the bespoke thing. Each item below is currently an assumption in the design, not a measured decision. Answer with what the gateway actually does, then decide.

- [x] 11.1 `telegram_messages` vs the gateway's own message records.

  **Decided by Justin, 2026-08-01: HARD KEEP.** The training-dataset job is real and intended, and the gateway is unlikely to store raw payloads tagged with *this app's* version. The audit-trail and replay jobs may well be duplicated by the gateway; that duplication is accepted rather than investigated, because the dataset job alone justifies the table. Consequence for the build: `message_log` stays wired into the new transport, `record_inbound` keeps its write-before-handler ordering, and every send path through `gateway_client` must keep writing. Not to be reopened on fit-the-architecture grounds.
- [x] 11.2 **ANSWERED 2026-08-01 from `approval-view-model-*.js`. The prompt can show a full rendered outcome — and that moves the risk rather than removing it.**

  There are two approval kinds and they are not alike. An **exec** approval renders a fixed template: title `"Exec Approval Required"`, description `"A command needs your approval."`, plus the command text. A **plugin** approval (`buildPluginViewBase`) takes `title` and `description` **straight from the requester** as free text, alongside metadata rows for Tool, Plugin and Agent, and a severity.

  So the answer to the question this task was written to settle — "can it show `assign Aari to claim #7, Echo gets $28`, or only a tool name?" — is **yes, it can show the full outcome**, because the text is ours to write. The tool name appears too, in the metadata, which is a bonus rather than a limit.

  **But the danger the task was really about survives, relocated.** The concern was that a claim the model picked wrongly would be invisible at the moment of approval. With free-text title and description, that is now a question of *who composes the text*. If the model supplies it, a model that resolved the wrong claim will also describe the wrong claim convincingly, and the prompt becomes a rubber stamp with better typography. The prompt must be composed **by code from the resolved claim record** — id, pet, merchant, date, amount read back from the row about to change — never from the model's own account of what it intends.

  **Consequence for D3:** the native approval UI is usable, so "native vs bespoke" is no longer the deciding axis. What matters is that the text is code-generated and the commit is code-owned. Both readings of D3 can satisfy that, so this unblocks the decision without making it. Still Justin's call.

  Original text: Native approval buttons vs the MCP-side confirm gate (D3). **Justin, 2026-08-01: decide after seeing it — do not choose on my reasoning.** So this is now a capture task, not an analysis task: install the gateway, drive a real mutation on a real claim, and bring back what its approval prompt *actually says on the phone*. The question it must answer: can the prompt show a full rendered outcome ("assign Aari to claim #7, Echo gets $28"), or only a tool name? If only a tool name, a wrong claim chosen by the model would be invisible at the moment of approval. Present the capture, then he decides. **Blocks treating D3 as decided.**
- [x] 11.3 **DECIDED by Justin, 2026-08-01 after seeing both on his phone: KEEP the Pillow cards.** Same 12 claims from the live history, rendered by `claim_card.render` and sent as a photo, against the identical rows as a markdown table. He chose A.

  This is the fit-the-architecture principle producing a *keep*, which is the outcome that needs recording most — the burden of proof was on the bespoke thing and it carried it on inspection, not on argument. What the card does that the table cannot: month grouping with per-month subtotals, coloured status pills, and a fixed layout that does not reflow on a phone. ~~The table had to clip the vet name to 12 characters to fit; the card shows it in full.~~

**CORRECTION 2026-08-02 — the last sentence was wrong, and the decision still stands without it.** A real card rendered during the 14.2 probe shows `SAH INNER WEST PTY LT S…` and `MediPaws Sydney Leichha…`: the card truncates too, just at roughly twice the width. "Shows it in full" was an overstatement of a real but smaller advantage.

Flagged rather than quietly edited, because the *reason* recorded for a decision is the thing future readers rely on. Nobody would reopen "keep the cards" over this — month grouping, per-month subtotals and status pills are untouched, and Justin chose A on sight — but somebody might one day reject a text rendering on the strength of a claim that is not true. The honest version: **the card gives more room for a vet name, not unlimited room.**

  **Consequences, several of which stop being optional:**
  - **14.2's shared media outbox is now required.** Every history and actions view is a PNG that must reach the gateway from the Python container, and the media allowlist (14.4) means it must land inside one of the gateway's own roots — `<stateDir>/media`. This was the leading option; it is now the only one.
  - **16.4's caption discipline is now load-bearing**, not a nicety. Every tapped-message result lands on a photo caption, so `gateway_client.edit_message` must name `caption` explicitly or write a spurious `editMessage failed` on every successful tap.
  - **`claim_card.py` is retained** and comes off the audit list.
  - Minor upside worth noting against 17.9: an image costs no prompt tokens. A table sent as text would have been charged on any turn the agent could see it.
- [x] 11.3b **Method note.** The first attempt at B rendered nothing but its heading and buttons — the table was silently dropped, and Justin spotted it. Had he not, A would have "won" a comparison in which B never appeared. Recorded as 14.5; the relevant discipline is that a live A/B on this platform needs the B confirmed *visible* before the choice means anything.
- [x] 11.3a **ANSWERED 2026-08-01 from the presentation normalizer, and it reframes 11.3 rather than deciding it.**
  - The presentation contract has exactly five block types: `text`, `context`, `divider`, `buttons`, `select` (`normalizePresentationBlock`, `dist/payload-*.js`). **There is no table block.** What `richMessages: true` actually turns on is `supportsBlockTables` — *markdown* tables rendered into the message body — plus a text ceiling of 32,768 instead of 4,000 (`TELEGRAM_RICH_TEXT_LIMIT`). A "native table" is formatted text, not a structure.
  - So a table cannot carry per-row buttons, and this is not a Telegram limitation to work around: a `buttons` block is a flat list chunked three-per-row (`TELEGRAM_INTERACTIVE_ROW_SIZE = 3`) and attaches to the **message**, with no relationship to any row of text above it.
  - **But the conclusion drawn above — "the actions view stays an image" — does not follow.** The actions view is already one message per item, each with its own buttons; it is not one table. That structure maps onto native messages exactly: per-item `text` block + per-item `buttons` block. The per-row-button constraint is satisfied by having one message per row, which is what the app already does.
  - **11.3 therefore narrows to a genuine taste question and nothing else: is each item a rendered PNG or a text block?** No capability decides it. Note one asymmetry worth putting in front of Justin: a PNG caption edit is how `_append_result` reports a tap outcome today (16.4), and a text message can be edited the same way — so neither option loses that.
- [x] 11.4 **AUDITED 2026-08-01. Verdict: `reminders.py` goes, `tasks.py` mostly stays — and the native counterpart is not called `tasks`.**

  **The name collision is worse than warned.** The gateway's `tasks` is not the comparison at all. The feature that overlaps `tasks.py` is **`openclaw commitments`** — "inferred follow-up commitments", with statuses `pending | sent | dismissed | snoozed | expired`. That is a model inferring a follow-up from a message and tracking it to closure, which is exactly `create_task` + `_extract_follow_up`. Anyone auditing "tasks vs tasks" would have compared the wrong two things and concluded there was no overlap.

  **`reminders.py` (33 lines) — replaceable, and the one subtle property survives.** `cron add --at <when>` is a one-shot job with timezone handling and persistence across restart. The property worth checking was the comment in `schedule_reminder`: `misfire_grace_time=None`, "if the app was down when `when` passed, fire immediately on restart instead of treating the run as missed." The gateway does this natively — `planStartupCatchup` (`dist/server-cron-*.js:1897`) collects missed jobs on boot and runs them, with `skipAtIfAlreadyRan: true` so a one-shot that already fired does not repeat.
  Two numbers to carry into section 5 rather than discover later: catch-up runs at most **5** missed jobs per restart (`DEFAULT_MAX_MISSED_JOBS_PER_RESTART`), deferring the overflow rather than dropping it, and missed **agent** jobs are deferred a further **2 minutes** past startup. Both are logged (`cron: running missed jobs after restart`, `cron: staggering missed jobs to prevent gateway overload`). Our jobs are few enough that the cap is not a constraint, but a deferral is not a skip and the logs are how you tell.

  **`tasks.py` (64 lines) — keep, and the reason is provenance, not scheduling.** Roughly half the file is the LLM follow-up extraction that `commitments` would replace. What it would *not* replace is what the rest of the file exists for: `source` / `source_message_id` tying a task back to the Gmail message that produced it, `outcome` / `outcome_at` closing the loop, and rows living in the same SQLite file as claims so a task and a claim can be reasoned about together. Handing task state to the gateway also puts it behind the isolation boundary the whole design is built on (7.0a), for no gain.
  So the honest split: drop `reminders.py` for `cron --at`, keep `tasks.py`'s storage and provenance, and treat its `_extract_follow_up` as the only piece worth re-examining once `commitments` has been seen working. **This is a kept-because-measured decision for `tasks.py` and a replaced-because-measured one for `reminders.py`** — recorded that way for 11.6.
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
- [x] 11.5 **AUDITED 2026-08-01. Verdict: extraction keeps `llm.py`'s walk; chat delegates to the gateway and loses the daily-vs-minute distinction. Say so plainly — this one is a real, accepted loss.**

  **0.4's question is answered: the gateway does not classify daily-budget exhaustion.** Its failover has a single `rate_limit` bucket (`model-fallback-*.js:46`), which it treats as *transient* — `shouldAllowCooldownProbeForReason` and `shouldUseTransientCooldownProbeSlot` both include it, so an exhausted model is re-probed during cooldown, and `resolveSessionSuspensionReason` maps it to `quota_exhausted` regardless of window. This matches 17.5's live observation: three retries of the same model at 10s/20s/30s, then `candidate_failed reason=rate_limit next=none`.

  That is exactly the distinction ADR-0017 exists to make. Groq's ceiling is **100k tokens per day, per model**; when model A's day is gone, waiting is useless and only moving to model B's separate budget helps. The gateway will spend three retries discovering that, every time, until midnight UTC.

  **Verdict, split:**
  - **`llm.py` keeps its per-model daily walk for `extract()` and `extract_vision()`.** These are the calls that actually exhaust a daily budget — batch extraction over a mailbox — and ADR-0017's chain is the thing that keeps them working. Unaffected by the gateway, which never sees them.
  - **Chat delegates to gateway failover, with a multi-model chain configured** so `next=none` becomes `next=<model B>`. That recovers the cross-model move after the wasted retries, rather than failing the turn.
  - **Accepted loss, recorded:** three futile retries per exhausted-day chat turn, and a cooldown probe policy tuned for per-minute limits applied to a per-day one. Small in practice — chat is a handful of turns a day against a 100k budget — but it is a regression against ADR-0017 and should not be discovered later as a surprise.
- [x] 11.6 **Every verdict recorded with its basis, in the guiding-principle table in `design.md`.** Closed 2026-08-01: `telegram_messages` keep (owner decision), Pillow cards keep (compared live), confirm gate split (owner decision), `reminders.py` replace / `tasks.py` keep (measured), `llm.py` split (measured). Each row says *why*, so a kept-because-measured decision and a kept-because-nobody-checked one cannot be confused later — which was the point of the task.
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

**BUILT AND RUN AGAINST THE LIVE SPIKE, 2026-08-02.** Nine checks. Real output:

```
FAIL  boundary plugins disabled      phone-control is not explicitly disabled in config; LOADED AND RUNNING: ['phone-control']
PASS  access policy
PASS  media outbox narrow            default roots
PASS  gmail-isolation-boundary
PASS  gateway menu scopes unchanged
PASS  model serves a turn            llama-3.3-70b-versatile answered
PASS  turn size under ceiling        4884 tokens; toolSchemaChars=1422, workspaceFileChars=6508, skillChars=0
```

The `phone-control` FAIL is real — the spike never disabled it, and the check found a boundary plugin loaded and running that nobody had noticed in a day of working in that container. First thing the preflight caught, before it had a deploy to guard.

**Three of my own assumptions were wrong and the CLI said so.** Recording them because each would have shipped as a check that could never pass:
- `openclaw config get` **requires a dot path**; there is no whole-config dump. A bare `config get --json` exits *Missing required argument "path"*.
- **`openclaw command run` does not exist** — *"Unknown command: openclaw command"*. See 19b.6 below; this changed what that check can honestly assert.
- The menu-scope check grepped one line, and `TELEGRAM_COMMAND_MENU_SCOPES` spans several. It reported `['default']` and a scope change that had not happened. **A false FAIL erodes a preflight as fast as a false PASS**, and this one would have fired on every deploy until someone stopped believing the output.

Two structural notes: `check_boundary_plugins` reads config **and** `health.plugins.loaded`, because Telegram auto-enables itself without writing config (13.2), so the enabled set is partly implicit; and the turn check asserts the **itemised** report, per 17.9 — a component regressing under a passing total is the failure it exists to catch.

- [ ] 19b.1 **Agent turn size under a declared ceiling.** Measure with `openclaw agent --agent <id> --session-key <fresh> --message hi` and assert the result is under the configured model's per-minute limit. **Must use a fresh session key** — an accumulated session measures conversation history, not surface, and produced two false readings before this was caught.
- [ ] 19b.2 **The model can actually serve a turn.** A model id that config accepts can still fail at runtime with `model_not_found` (Groq did, before a custom provider entry existed). Assert one real turn completes.
- [ ] 19b.3 **Boundary plugins are disabled**: `browser`, `file-transfer`, and anything else granting filesystem, shell or browser reach. An upgrade re-enabling one must fail the deploy, not pass quietly.
- [ ] 19b.4 **Access configuration**: `channels.telegram.dmPolicy == "allowlist"`, `allowFrom` non-empty, `commands.ownerAllowFrom` non-empty. Default is `pairing`, which hands unknown senders a live pairing code and the command to socially-engineer approval.
- [ ] 19b.5 **The gateway holds no Gmail credential** and no Google key with a Gmail scope, and has no mount that can reach `app/data`.
- [x] 19b.6 **PARTLY BUILT, and the shortfall is stated rather than papered over.** Original: *Assert each one responds — not that `plugins list` reports it, since those fields come from a persisted registry that goes stale silently and reported `commands: []` for a working command. Enumerate from the button-building code so a new button cannot ship unasserted.*

  **The task asks for each command to be invoked, and there is no way to do that.** `openclaw command run` does not exist; the only real dispatch path is a Telegram tap, and a deploy script must not fake one against Justin's chat.

  What is asserted instead: the plugin POSTs `/internal/plugin/hello` at boot with the command list `api.registerCommand` actually accepted, the app holds it **in memory**, `/health.gateway_plugin` exposes it, and the preflight compares it against `gateway_client.BUTTON_COMMANDS`. That is a runtime signal from inside the registration call — stronger than reading the persisted registry (18.6), weaker than a tap. A plugin that loaded without running never reports, so the check fails rather than passing quietly, which covers 18.7's two silent gates.

  In-memory is the load-bearing part: persisting the report would recreate exactly the failure that makes `plugins list` useless. A restart of either runtime must empty it.

  **CLOSED 2026-08-02 by a real tap — the residual did not have to wait for 4.11.** Justin tapped the `Mark #7 sent` button on the 14.2 probe card and got back, in the chat:

  ```
  /mark failed (404). {"detail":"Not Found"}
  ```

  That single line proves the entire chain with the **shipped** plugin rather than the spike, and against the **real** `/internal` surface rather than an echo route: button → core's native command path → `claims` plugin handler → HTTP POST to the app → the app's genuine 404 for an endpoint slice 2 has not built → rendered back into Telegram. 16.2 proved the mechanism; this proves the artefact.

  It also proves the **failure-visibility** rule survives the new transport, which nothing had tested. The 404 arrived verbatim, attributed to `/mark`, with its status code. Not swallowed, not a silent no-op, not a cheerful acknowledgement of something that did not happen — which is exactly what the handler was written to do and exactly what could not be verified without a tap.
- [x] 19b.1–19b.5, 19b.7 **BUILT.** See the section header for the live run and for the three assumptions the CLI corrected.
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
