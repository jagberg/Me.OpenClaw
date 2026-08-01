## Context

The repo has been named `OpenClaw` since inception and has never depended on OpenClaw. Verified 2026-08-01: no `openclaw.json`, no `package.json`, and `requirements.txt` contains only FastAPI, APScheduler, `python-telegram-bot`, the Google clients, `pypdf` and Pillow. What exists instead is a hand-rolled equivalent of the gateway's edges — 2312 of 8689 lines across `telegram_bot.py` (1022), `agent.py` (692), `llm.py` + `gemini.py` (379), `message_log.py` (202) and `scheduler.py` (17) — documented by four ADRs (0009, 0014, 0015, 0017) that exist only to record its failure modes.

OpenClaw is a local-first gateway daemon: it owns channel transport across 25+ messaging platforms, agent sessions with per-sender isolation, model resolution with fallback chains, cron, and a plugin/MCP extension surface. It runs on Node 24.

What it does not own, and will not: ceiling matching (ADR-0007), the append-only status event log (ADR-0008), condition threads, excess accrual, Petcover reference learning, invoice segmentation. That is ~5300 lines derived from real emails, PDFs and CSVs over a year, and it stays in Python.

Constraints that shaped every decision below:

- **One bot token, one poller.** Telegram answers a second `getUpdates` caller with `409 Conflict`. The cutover is therefore a single atomic moment, not a gradual migration.
- **The hard rules are enforced by absence, not by policy.** "Never send email" holds because `send()` appears nowhere in `app/openclaw/`, while the OAuth token *does* carry `gmail.compose`. Introducing a runtime that offers shell, file and browser tools is the one part of this change that can regress a hard rule.
- **The SQLite database is not shareable carelessly.** A read-write open from the wrong side once deleted the WAL sidecars and took out both the scheduler and Telegram until a container restart (2026-07-25). The gateway must never touch the database file.
- **`.env` already diverges** between the main checkout and the deploy worktree. A second runtime with its own configuration doubles that surface.

## Guiding principle (Justin, 2026-08-01)

**The solution fits the OpenClaw architecture; OpenClaw is not bent to fit the solution.** Where the gateway already does something, use its version. Keep what this repo built only where losing it costs something real — the test is "is the trade-off small?", and the burden of proof sits on keeping the bespoke thing, not on adopting the native one.

This is a constraint, not a preference, and it settles arguments in advance. It also caught a mistake already: D2 originally forwarded every inbound message into the Python app, which is using an agent gateway as a dumb pipe and discards the reason to adopt it. Corrected below.

Applying it is not a one-off. Several things this repo built have native counterparts whose trade-offs were **unmeasured**, so each became an explicit audit item (tasks §11) rather than an assumption baked into this design. Status as of 2026-08-01:

| Bespoke thing | Verdict | Basis |
|---|---|---|
| `telegram_messages` | **Keep — decided, closed** | Owner decision: the training-dataset job is real and intended. Audit/replay duplication with the gateway is accepted, not investigated. |
| Confirm gate (D3) | **Open — capture then decide** | Not to be settled on my reasoning. Capture what native approval actually shows for a real claim, then Justin chooses (11.2). |
| Pillow claim cards | **Open — compare live** | Send both a rendered card and a native table to the real chat; he picks (11.3). May be pre-empted by whether tables carry per-row buttons (11.3a). |
| `tasks.py` / `reminders.py` | **Open — unexamined** | 11.4. |
| `llm.py` fallback chain | **Open — depends on 0.4** | 11.5. |

The principle cuts both ways: `telegram_messages` is kept *against* the native option because a specific, stated need outweighs it — which is the test working, not an exception to it.

## Goals / Non-Goals

**Goals:**

- Gateway owns Telegram transport, the chat agent loop, model resolution and cron.
- Claims domain stays Python, reachable by the agent through an enumerated MCP tool inventory.
- Telegram interface survives feature-for-feature: photo cards, inline keyboards, tap-to-resolve, 👍 ack, result-appending edits, paging.
- Gmail stays entirely inside Python, with the gateway holding no credential and the agent holding no tool that could reach one.
- Every mutation stays a proposal committed only by a confirm tap, enforced by code rather than prompt.
- 2312 lines of plumbing deleted, and every guarantee those lines carried either preserved or explicitly recorded as lost.

**Non-Goals:**

- Rewriting any domain module. `pipeline`, `claim_status`, `invoice_matching`, `claim_forms`, `vet_detection`, `claim_card`, `netbank_csv`, `gmail_client`, `db` are untouched.
- Moving the FastAPI dashboard to Live Canvas. It stays as it is.
- Enabling additional channels. WhatsApp/Signal become *possible*; turning them on is a later decision, and each new channel re-opens the single-authorized-user question.
- Adopting ClawHub skills or additional plugins. The plugin set is pinned deliberately (see risks).
- Resolving ADR-0003 (assistant-side reminders don't push). Noted as an open question, not fixed here.

## Decisions

### D1 — Gateway is the shell; Python keeps the domain

The gateway owns the edges; Python owns the middle. `pipeline.run_once` is unchanged and simply gets a different caller.

*Alternatives considered.* Porting the domain to TypeScript as an OpenClaw plugin — rejected: it discards the one asset that is genuinely hard to rebuild, and the stack is Python-shaped (`pypdf` for invoice segmentation, Pillow for cards, `google-api-python-client`, SQLite). Running the claims service as a subprocess of the gateway — rejected: it makes the domain's lifecycle depend on the gateway's, and the pipeline must keep advancing claim state when the gateway is down.

### D2 — Two seams: MCP inbound for the agent, CLI outbound for the app (SUPERSEDED 2026-08-01 by D12)

**Superseded by:** D12. **Why it changed:** the outbound seam was proved able to carry buttons after all, and the inbound seam collapsed to `command` actions — no callback bridge. Reasoning kept verbatim below because it is the record of what the corrections were correcting.

The integration is deliberately two one-directional seams rather than one bidirectional protocol.

- **Agent → domain:** a Python MCP server registered with `openclaw mcp set`. The agent pulls; every tool is enumerated.
- **App → Telegram:** `openclaw message send` / `edit` / `react`, invoked from Python for unattended notifications. The pipeline has no inbound event to reply to, so it needs an addressable send path of its own.
- **Gateway → app (callbacks only):** a thin Node plugin that **claims button callbacks** and forwards them to a localhost-only FastAPI endpoint. The plugin carries no logic; it is a wire.

**Corrected 2026-08-01.** That third seam originally forwarded *every* inbound message. It should not, and the docs make the intended split explicit: *"Callback clicks not claimed by a registered plugin interactive handler are passed to the agent as text: `callback_data: <value>`."* So a registered interactive handler is the supported way to intercept a tap before the agent sees it. Conversation needs no bridge at all — the agent is the message processor and reaches the domain through tools. Only deterministic UI callbacks need claiming:

| Traffic | Path | LLM in the loop? |
|---|---|---|
| Free-form chat | agent → MCP tools | Yes, correctly |
| Button tap (`sent:7`, `setpet:3:2`) | plugin interactive handler → `/internal` → existing handler | **No** |
| Unattended notification | `message send` from the pipeline | No |

**The failure mode this creates, and it is the dangerous one.** An unclaimed callback is not dropped or errored — it is handed to the model as text. Combined with a fact confirmed from a working plugin's source (*registering with the wrong API "registers silently but the hook never fires"*), a plugin that looks installed but registered nothing produces no error and turns every tap into an LLM interpreting `callback_data: sent:7`. A model deciding whether to commit a Petcover submission is exactly what the confirm-gate exists to prevent, reached by silent degradation. Registration must therefore be *proven* at startup by asserting a known token is claimed, and any `callback_data:` string reaching the agent must be treated as an error, never as input.

*Alternatives considered.* The gateway's OpenAI-compatible `/v1/chat/completions` — wrong direction, that is for consuming an agent, not for a tool answering one. `POST /api/v1/admin/rpc` — documented as a default-off plugin route; the CLI is the documented path and does not require opening an admin surface. A second bot token for the app's own sends — rejected: two bots means two chat threads and the card interface stops being one place. Putting logic in the Node plugin — rejected: it would create a second place where claims decisions live, which is exactly the drift the "thin adapter" requirement has always guarded against.

### D3 — The proposal gate moves from `telegram_bot._execute_action` into the MCP server (CONTRADICTION FLAGGED — unresolved)

**This contradicts D12 and Justin's 2026-08-01 decision that deterministic calls do not use MCP.** D3 places *the* commit gate in the MCP server. Under D12 a confirm tap is a `command` button → plugin → `/internal`, which never touches MCP, so the commit path for a tapped confirmation is Python behind `/internal`, not the MCP server. The chat-initiated half (a `propose_*` tool returning a confirmation) may still be MCP's.

Not resolved here on purpose: the invariant Justin called non-negotiable is *"a commit can never be a tool return value"*, and both readings satisfy it. Which component owns the gate is a real decision that needs making rather than absorbing. Feeds 11.2 and the section-3 rewrite.

Today the gate is a harness property, explicitly *not* a behaviour the model is trusted to observe. That property must survive a general-purpose agent runtime, so it moves down rather than out: `propose_*` tools write a pending action and return a confirmation; the commit lives on the confirm-callback path in Python. A tool call can never itself be a commit.

The same move applies to the two harness-enforced refusals — two-pets-named-is-never-one-pet, and refuse-the-split-when-items-have-no-amounts. Both lost when they were prompt rules; both stay code.

*Alternative considered — the gateway's own approval UX (`inlineButtons` gates "inline approval buttons").* **Provisional under the fit-the-architecture principle: this was reasoned, not measured.** The argument against it as the primary gate is that it approves *a tool call*, whereas the domain needs to approve *a described outcome* (which claim, which pets, what shares), and the harness refusals must be able to reject a call the model was permitted to make. That argument is sound if native approval is call-shaped, and wrong if it can carry an arbitrary rendered outcome.

**Justin, 2026-08-01: decide after seeing it.** Explicitly not to be settled on the reasoning above. 11.2 becomes a capture task — install, drive a real mutation on a real claim, bring back what the approval prompt actually says on the phone — and he chooses from that. The stake in plain terms: if the prompt reads "run propose_assign_pet" rather than "assign Aari to claim #7", then a model that picked the wrong claim produces an approval prompt that looks correct. That is the failure the gate exists to catch, so the prompt's *wording* is the whole decision.

### D4 — Gmail is walled off by capability, not by instruction

The gateway gets no Gmail credential and no Google key carrying a Gmail scope. The agent's inventory contains no filesystem, shell, or browser tool. Mail is reachable only through the three existing named sweeps, whose scopes are fixed and take no query argument.

The browser tool is the subtle one: a browser reaching a logged-in webmail origin bypasses the credential question entirely, which is why its absence is a requirement rather than a default.

*Alternative considered.* Narrowing the OAuth token to read-only scopes so sending is impossible even with the credential. Attractive, and arguably should happen anyway — but it breaks draft creation (`gmail.compose` is what drafts need), so it cannot be the mechanism here. Recorded as a standing limitation, not solved.

*Alternative considered and rejected — a native/plugin Gmail connector in the gateway (asked 2026-08-01).* Two independent reasons.

First, it inverts D4. Third-party write-ups describe the connector requesting `gmail.readonly`, `gmail.send`, `gmail.compose` and `gmail.modify`, with `modify` recommended, and describe optionally configurable automated sending. A `gmail.send` grant living inside the gateway ends the enforcement model: "never send email" is currently a structural fact (`send()` absent from `app/openclaw/`), and this would demote it to a policy the agent is asked to observe. Approval-gated sending is still a send path.

Second, it would not simplify much even if the scope problem vanished. The claims service does not need an email assistant; it needs `_build_queries`' merchant narrow/wide + spouse fallback + `-from:me`/SENT guards, `full_message_text` **including PDF text** (settlement breakdowns exist only in the attachment), the vision-OCR fallback and its 3-attempt cap, `email_extractions` caching, `find_invoice_segment`, context-phrase-only reference learning, and drafts carrying a filled Petcover PDF. A generic connector supplies none of it, so `invoice_matching`, `claim_forms` and `claim_status` stay regardless and the connector displaces only `gmail_client.py` — 88 lines. The concrete failure mode if adopted anyway: a body-only read answers "did Petcover pay?" with no figures rather than with an error.

*Caveat on the evidence.* `docs.openclaw.ai/tools` documents runtime, files, web, browser, messaging and media tools and mentions **no** Gmail connector. Every claim about scopes above comes from third-party sites, not from `openclaw.ai` or the GitHub repo, so whether a first-party connector exists is unverified. The rejection does not depend on resolving it.

*Where a connector would fit.* `gmail_ingest.py` (84 lines, email → tasks/reminders) is genuine read-and-summarize work of the shape a connector serves. It reads the same mailbox, so it inherits the same grant and the same scope objection. Noted, not adopted.

### D5 — `chat()` is deleted; `extract()` and the daily-budget walk stay

`llm.chat()` has exactly one caller (`agent.py:679`), so retiring the app's tool loop makes it dead code. `extract()` (3 callers) and `extract_vision()` (Gemini-only, ADR-0010) stay.

The daily-budget walk (ADR-0017) stays in Python and is **not** assumed to be inherited. The gateway's documented failover triggers on rate-limit responses only; a per-day token cap is a distinct condition, and `llm.py` deliberately splits `_is_daily_budget_exhausted` from `_is_rate_limited` because one means switch-model and the other means back-off-and-retry. Whether Groq's daily-exhaustion body is classifiable by the gateway is a spike, not an assumption.

Two requirements are removed outright, and in both cases the hazard transfers rather than disappearing: chat-provider constraints (now gateway config) and the output-only-field replay whitelist (`reasoning` fields killed live turns once the chain could reach a reasoning-capable model — now the gateway's replay to get right).

### D6 — Cron invokes a localhost-only internal endpoint

Gateway cron entries POST to FastAPI routes bound to `127.0.0.1` with a shared secret. Same mechanism as the event bridge, so there is one internal surface rather than two.

Overlap protection is the app's, not cron's: a tick takes an advisory lock and a second concurrent invocation returns immediately. Two `pipeline.run_once` calls against one SQLite database is the failure this prevents.

*Alternatives considered.* `docker exec` from the gateway into the Python container — rejected: needs the Docker socket, and couples the gateway to the deployment topology. Keeping APScheduler for the tick and using gateway cron only for new work — rejected: two schedulers is worse than either one, and the point is to delete one.

*What the lock is replacing, which is easy to miss.* APScheduler already refuses an overlapping run — `max_instances` defaults to 1 and no job in this repo overrides it. So the guarantee exists today, invisibly, and cron does not have it. The lock is that guarantee being rebuilt, not a new precaution. Without it, two `pipeline.run_once` calls would have `_draft_matched_claims` batch the same claims into two Gmail drafts — two Petcover submissions for one set of invoices — plus duplicate rows in the append-only `claim_status_events`, and duplicate invoice-request mail to the same vet.

**Decided (2026-08-01, Justin): a per-job in-process lock.** Not one global lock — keyed by job name, so a running tick does not block the nudge. Sufficient for this deployment and known to be, rather than assumed: the Dockerfile runs `uvicorn` with no `--workers`, and the gateway invokes this app over HTTP rather than running the jobs itself, so exactly one process ever enters them.

**The condition that silently invalidates it: `--workers` above 1.** Each uvicorn worker is its own process with its own lock, seeing nothing of the others. There is no error and no log line — the jobs simply start overlapping again. The same applies to any second container permitted to run them. This is recorded in three places on purpose, because the openspec change is archived and the hazard is not: a comment at the `CMD` line in `app/Dockerfile` (where someone would actually type `--workers`), the `ponytail:` note in `internal_api`, and here.

A `job_locks` table was written, tested and reverted the same session. Not because it was wrong — it removes the ceiling — but because nothing in this plan creates a second process, so it was insurance against a change no one has proposed. Recorded here because the reason it is not a drop-in is worth keeping: **a database row saying "running" outlives the process that wrote it.** A `threading.Lock` dies with its process, so a crash mid-tick releases it automatically; a table does not, and a naive one would make every subsequent tick skip forever on the strength of a dead run. Any future DB version therefore needs a staleness threshold set above the longest legitimate run (the slow case is a tick doing vision OCR on a scanned invoice), compare-and-swap on the timestamp so two processes cannot both decide they are stealing it, and release-only-by-holder so a hung process finishing late cannot drop its successor's lock. Guessing the threshold high costs a few missed ticks after a crash; guessing it low costs a duplicate claim submission — the asymmetry decides it.

The trigger to build it: `--workers` above 1, or any second invoker of the jobs. Until then the in-process lock is the whole mechanism and its comment names this path.

### D7 — `telegram_messages` stays (decided by Justin, 2026-08-01 — closed)

Asked directly whether the training-dataset job is real, the answer was yes and intended. That closes it: the table is kept even where the gateway duplicates part of it, and it is not to be reopened on fit-the-architecture grounds. The audit-trail and replay jobs may well be covered by the gateway — that overlap is accepted rather than investigated, because the dataset job alone carries the decision.

The gateway keeps its own session records; they do not replace this table. It is three things the gateway does not promise: the RL dataset (raw payload + `app_version`), the audit trail for "did my tap register?", and the replay queue whose `record_inbound`-before-handler ordering is what makes a mid-handler crash recoverable (ADR-0014).

Both delivery paths are at-least-once, and now there are two of them. The existing idempotence requirements carry the load; the sweeps' idempotence becomes load-bearing rather than incidental.

### D8 — Do we need MCP at all, given it burns tokens? (Justin, 2026-08-01)

**MCP costs tokens only on turns where the model is already running.** It is not a tax on deterministic flows — provided deterministic flows never reach the agent, which is precisely what D2's callback-claiming buys. A tap routed through the interactive handler costs zero tokens: no model, no schema, no turn. A pipeline notification costs zero. Only free-form chat costs anything, and that turn was going to happen regardless of how the domain is reached.

So the question resolves to: is the agent worth having? `conversational-agent` is a shipped capability in daily use, and the alternative — deterministic commands and the dashboard only — is a capability removal, not an architecture simplification. Keep it, and MCP is how it reaches the domain.

**The real cost the question surfaces, which is worth acting on.** This repo's own measurement: ~2.6k tokens per chat request *because the tool schema ships every time*, against a binding Groq ceiling of 100k tokens/day/model — roughly 38 chat turns a day. Every tool added to the inventory inflates **every** conversational turn, whether or not it is called. The inventory is therefore a per-turn tax, and `claims-mcp-surface` already requires it to be minimal and enumerated for security reasons. It now has a second, independent reason. Tool count should be treated as a budget with a measured number against it, not trimmed by taste.

*Alternative considered — `api.registerTool` instead of an MCP sidecar.* A transport choice with **zero** token difference: the schema ships in the request either way. The sidecar is the more reliable route, since `registerTool` is absent on some builds (a working plugin guards with `if (api.registerTool)`), and it keeps the tools in Python where the domain lives. No reason to prefer it on cost.

*Alternative considered — no agent tools at all; the agent answers only from what a command already produced.* Rejected: it makes every question a command Justin has to know, which is the interface the chat agent was built to replace.

### D9 — What "conversation never reaches the app" actually costs (SUPERSEDED 2026-08-01 by D12)

**Superseded by:** D12. **Why it changed:** D9 reasoned about consequences of a callback-claiming bridge that D12 removes. **Still live and NOT superseded:** the D7/D2 collision it found (the app cannot log conversation it never sees) survives as task 12.1, and the pending-free-text-flow problem survives as 12.2.

D2's correction is right, but it was written as a scope reduction and it is not only that. If inbound conversational messages go straight to the agent and only *callbacks* are claimed by the plugin, then five things the app does today have no path. Four are solvable; the first is a direct collision between two decisions taken an hour apart.

**1. The message log cannot see conversation — and that contradicts D7.** Justin decided `telegram_messages` is a hard keep *because the raw-payload training dataset is real*. But if his typed messages never reach the Python app, the app cannot log them, and the dataset silently narrows to callbacks and outbound only — the least interesting half. Two decisions, individually sound, that cancel out.

  Resolution, and it is cheap: the plugin forwards a **copy** of every inbound message to `/internal` for logging only, while the agent handles it. No logic, no routing decision, no claim on the callback — a tee, not a bridge. It keeps D2's correction intact (the app is not the message processor) and D7's dataset whole. It does mean the plugin sees every message, so the earlier framing of "conversation needs no bridge at all" was too strong: it needs no bridge *for handling*, and still needs one for recording.

**2. Pending free-text flows break.** Three in-memory dicts drive multi-step interactions: `_pending_condition` (tap "Other (type it)", then type the condition), `_pending_split` (walk line items one reply at a time), and `_pending_actions` (token → Confirm tap). The third survives — it is a callback, so the plugin claims it. The first two do not: they need the *next typed message* routed to the app rather than the agent, and under the correction the app is not consulted. Options, none free: teach the plugin to claim text while a flow is pending (needs a message-level claim, not just callbacks — reopens part of 0.8); re-express both flows as agent tools so the agent collects the value and calls in; or accept the agent handling them conversationally and delete the dicts. The third is most in keeping with the fit-the-architecture principle and the least like the interface Justin has today.

**3. The 👍 acknowledgement is no longer the app's to send.** It exists so a slow handler does not feel dead. If the app never sees the message, the ack must come from the gateway or the agent, or go away. Whether the gateway acks inbound automatically is unverified.

**4. The single-authorized-user check narrows to callbacks.** The app currently rejects any username that is not the configured one. Under the correction it only ever sees callbacks, so for conversation the gateway's own access control *is* the authorization — which is exactly why 0.5 (can the channel be pinned to one username, can DM pairing be disabled) stops being a nice-to-have and becomes load-bearing.

**5. Edited messages become the agent's problem.** The 2026-07-27 bug — an edit arriving where `update.message` is `None`, silently dropped and logged as an empty `other` row — was fixed in app code that no longer sits in the path. Whether the gateway delivers edits to the agent at all is unverified, and if it does not, a correction Justin types would once again vanish.

The through-line: D2's correction moves work out of the app, and four of these five are the gateway now having to do something the app used to. Each needs confirming against the real gateway rather than assuming the native behaviour is at least as good.

### D10 — RETRACTED same day. The CLI *can* send interactive messages; my payload was malformed five times

**Retracted 2026-08-01, minutes after being written.** Buttons render correctly from `openclaw message send`, on a **photo** message, with `style` honoured (primary/danger). D10's conclusion — "the integration must be a plugin" — was wrong, and so was the reasoning that produced it.

**The actual contract**, read out of `payload-*.js` rather than inferred:

```
presentation = { title?, tone?, blocks: [] }
blocks[]     = text | context | divider | buttons | select
buttons[]    = { label, action: { type: "command" | "callback", ... }, style? }
```

Every earlier attempt put `buttons` at the **top level** of `presentation`. `normalizeMessagePresentation` returns `undefined` for that shape — and returned `undefined` for all five variants tried — so the presentation was discarded before reaching Telegram. Hence `ok: true`, a real message id, and no buttons. Not a media limitation, not the `inlineButtons` capability, not a CLI gap. Wrong nesting.

**Consequences of the retraction:** `gateway_client` survives as the outbound seam, including for the card interface. Section 1's code stands; only the presentation shape needs fixing. No plugin is required to *send* interactive messages — a plugin is still the likely answer for deterministically *claiming* taps, which is a smaller job.

**Method, restated because it is now 4-for-5.** Four architectural conclusions today were wrong (raw-event forwarding, media paths, buttons-unsupported, plugin-required); every one came from reasoning off documentation and this repo's existing shape, and every one was settled in minutes by reading `/app/dist` or by running the platform's own normalizer against a candidate payload. **Validate payloads against the shipped validator before sending.** That single habit would have prevented five round-trips and four wrong write-ups.

#### Superseded reasoning (kept deliberately — this is what being wrong looked like)

**What was tested, live, against a real gateway and a real chat.** Sending a photo works. Editing a photo's *caption* works — verified visually, Telegram marked it edited, so `_append_result`'s caption path survives. **Buttons never rendered**, across three attempts: wrong payload shape, correct payload shape (`label` + `action`, the schema the Telegram extension's `toTelegramInlineButton` actually reads), text-only as a control, and with `channels.telegram.capabilities.inlineButtons = "all"`. Every send returned `ok: true` with a real message id and logged no warning. `--dry-run` then showed why: the built payload contains **no `presentation` field at all**. The CLI declares the option and the direct send path does not carry it.

Buttons are unquestionably a first-class feature — `button-types.ts` maps `label` + `action` to Telegram inline buttons, supports `style` (primary/danger/success), chunks rows at 3, and `action.type` is either **`command`** (tap invokes a slash command) or **`callback`** (opaque token, claimed by a plugin interactive handler; `interactiveHandlers` exists in core). They are simply produced by the agent-reply / interactive-block pipeline **inside** the runtime, not by one-shot external sends.

**Consequence, and it overturns D2's outbound seam.** `gateway_client` shelling out to `openclaw message send` can deliver text and media, and can edit captions — but it **cannot deliver the card interface**, which is buttons on a message and is the core of what Justin said must not be lost. Unattended pipeline notifications carrying tap-to-resolve buttons therefore cannot be sent from outside the gateway.

**Corrected shape.** The primary integration becomes an OpenClaw **plugin running inside the gateway container**, because a button and the handler that receives its tap are one unit and both live in the runtime:

- plugin registers an HTTP route (`api.registerHttpRoute` — confirmed present in the stock API surface) that the Python app POSTs notifications to, and the plugin renders them with buttons;
- plugin registers interactive handlers to claim taps, forwarding decoded intent to the app's `/internal` endpoints;
- Python MCP server continues to supply the agent's domain tools;
- the CLI stays useful only for plain text and media without interaction.

`action.type: "command"` is the find that makes this cheaper than it sounds: a tap can invoke a slash command directly, which maps onto the existing `/mark 7 sent` and `/pet 1 Aari` surface with no bespoke token routing at all.

**Residual uncertainty, stated plainly:** that a plugin *can* render buttons is inferred from the shipped code, not yet demonstrated. It needs one plugin spike before this design is relied on — and that spike replaces further CLI round-trips.

**Method correction.** Three wrong conclusions today (raw-event forwarding, media paths, buttons) all came from deriving the integration from documentation prose and from this repo's existing shape. Each was settled in seconds by reading `/app/dist` inside the container. For the remainder of this work the shipped code is the source of truth, and `--dry-run` is the way to check a payload rather than sending and asking Justin to look.

### D11 — The corrected architecture, built only from what was measured (SUPERSEDED 2026-08-01 by D12)

**Superseded by:** D12, hours later. **Why it changed:** D11 still assumed callback actions were the mechanism for taps; measurement showed callback values are opaquely re-encoded and unusable from outside a plugin.

Everything below was verified against a running gateway or read out of `/app/dist`. Where something is still inferred it says so.

**Four layers, and the plugin is smaller than D10 feared but larger than 16.3 hoped.**

| Layer | Owns | Status |
|---|---|---|
| Gateway (Docker, isolated) | Bot token, transport, sessions, model routing, cron | Running |
| Agent | Conversation; reaches the domain through MCP tools | Design unchanged |
| Python app | The claims domain, untouched; MCP server + `/internal` HTTP | Section 1 built |
| Channel plugin (Node, in-gateway) | Registering the app's commands; claiming callback taps | Not yet built |

**Outbound — settled.** `gateway_client` → `openclaw message send`, with buttons nested as `presentation.blocks[{type:"buttons"}]`. Verified working for text, media, caption edits, and buttons with `style`. Two constraints: media must sit in an allowlisted directory (14.1), and a malformed presentation is dropped **silently** with `ok:true`, so payloads must be validated against `normalizeMessagePresentation` before sending.

**Inbound — three distinct paths, not one.**
1. *Conversation* → agent → MCP tools. No bridge, no forwarding.
2. *`command` buttons* → invoke a native slash command through core's command path. **Verified working live.** Deterministic, no model.
3. *`callback` buttons* → "carry opaque plugin data through the channel's interaction path", handled by a channel plugin's interaction handler. Inert without one — verified.

**Correction to 16.3, which was too optimistic.** I concluded `command` actions remove the need for a plugin. They do not, for a reason I missed: a `command` action invokes a **native** slash command, and `/mark 7 sent` is *this app's* command, not one the gateway knows. Something must register it — `api.registerCommand` exists in the stock API surface. So a plugin is required either way; what changes is its size. It registers commands and claims callbacks, and does not need to own outbound rendering.

**Correction to my correction on unclaimed callbacks — the risk is real after all.** I observed an unclaimed callback doing nothing and downgraded task 9.10. That inference was wrong. Open issue [#46841](https://github.com/openclaw/openclaw/issues/46841) requests a `telegram.callbackRoutes` webhook bypass explicitly to skip `processMessage` for "zero-token" handling — which only makes sense if unclaimed callbacks *do* currently reach the agent session and spend tokens. The likeliest explanation for the silence in our test is that this spike gateway's agent (`openai/gpt-5.5`, no key) fails before producing output. **9.10 stands as originally written.** Confirm with a working model before relying on either reading.

**Access model — measured, and it is three settings where the app has one.** `channels.telegram.dmPolicy` ∈ `pairing|allowlist|open|disabled` (default `pairing`), plus `channels.telegram.allowFrom` (numeric ids) and `commands.ownerAllowFrom`. Production mapping: `dmPolicy: "allowlist"` + `allowFrom: [<id>]` + owner set. This also removes the pairing-code disclosure Justin objected to (15.1), since unknown senders are blocked rather than handed a code.

**What is still genuinely unknown:** whether a `command` action's payload is subject to Telegram's 64-byte callback limit (16.5 — decides whether condition buttons can be commands at all), and whether caption editing works on *document* messages as well as photos (16.4).

### D12 — The architecture, consolidated. Supersedes D2, D9, D10 and D11 (2026-08-01)

Those four decisions accumulated corrections faster than they were written. This is the single statement, and every claim in it was verified against a running gateway rather than reasoned from documentation.

**Justin's decision (2026-08-01): no MCP for deterministic calls.** Deterministic paths — a button tap, a slash command, an unattended notification — never involve a model or a tool schema. MCP survives only as the conversational agent's read/propose surface, and that scope is now also a cost control: at 23.5k tokens per turn against Groq's 12k TPM (§17), every tool in the inventory is charged on every chat turn.

**Four components.**

| Component | Runs in | Owns |
|---|---|---|
| Gateway | Its own Docker container, isolated | Bot token, transport, sessions, model routing, cron |
| `openclaw-claims` plugin | Inside the gateway | The app's slash commands; outbound rendering if needed; HTTP route inbound |
| Python app | Existing container | The claims domain, unchanged; `/internal` endpoints |
| Agent | Gateway | Conversation only, reaching the domain through a deliberately small MCP inventory |

**The tap path, which is the heart of it and is the simplest option considered:**

```
button (action.type "command", e.g. "/mark 7 sent")
  -> core native command path
  -> plugin command handler (api.registerCommand)
  -> HTTP to the app's /internal
  -> existing claim logic
```

No callbacks, no opaque tokens, no interactive handlers, no LLM. Verified end to end: a `command` button dispatches through core, and a plugin-registered command executes and replies.

**Why not `callback` actions.** `toTelegramCallbackData` wraps a callback value in `buildTelegramOpaqueCallbackData`, so the raw value never reaches Telegram and the namespace resolver cannot match it. The opaque form exists for a plugin's own send-and-decode round trip. Registration succeeds; the value simply never arrives in the expected shape. `command` actions avoid the problem entirely and reuse the command surface this project already has.

**Outbound.** `gateway_client` → `openclaw message send`, buttons nested as `presentation.blocks[{type:"buttons"}]`. Verified for text, media, caption edits and styled buttons. Media must sit in an allowlisted directory, so a narrow outbox path between the containers is required (§14) — never a general mount of `app/data`.

**Access.** `channels.telegram.dmPolicy = "allowlist"` + `allowFrom: [<id>]` + `commands.ownerAllowFrom`. Three settings where the app has one; the allowlist policy also removes the pairing-code disclosure to strangers (§15).

**The standing hazard: this platform fails silently.** Six distinct instances measured today — a malformed presentation dropped with `ok:true`; an unregistered callback namespace discarded; a plugin loaded but never run; a plugin missing `definePluginEntry` never run; `plugins list` reporting stale registry data as live truth; an access denial with no log line. **Success responses and inspection commands are not evidence here.** Verify behaviour, and validate payloads against the platform's own validators before sending.

## Risks / Trade-offs

- **Cutover 409** → The moment the gateway binds the token, the Python updater must already be stopped. Phase 3 is a single deploy step with the updater behind a flag, so rollback is flipping it back.
- **Inline buttons on photo cards may not be reachable through `message send`** → Spike before anything is retired. If unsupported, the cards keep their buttons on an accompanying text message; if that is also impossible, the swap stops at Phase 2 and the old transport stays. The UI is not negotiable, so this risk gates the change rather than being absorbed by it.
- **Caption editing may not be reachable through `editMessage`** → Same gate. `_append_result` exists because editing text crashes on PDF alerts; losing caption edits would regress exactly the messages that most need feedback. Fallback is a reply rather than an edit, recorded as a visible degradation.
- **Gateway does not fail over on daily budget exhaustion** → Extraction keeps its own walk in Python. Chat-side, capture a real exhaustion response and check the classification; if it fails fast, the agent gets a visible unavailable message rather than a silent weaker answer.
- **Inherited reasoning-field replay bug** → Verify against a reasoning-capable model in the configured chain before relying on it. Treat a recurrence as a gateway defect; do not re-fix locally.
- **Agent tool creep** → A future plugin or skill install can add a shell or file tool and silently breach D4. Mitigation: pin the plugin set, and assert the agent's tool inventory in the smoke suite so an added tool fails the tests rather than passing unnoticed.
- **New internal HTTP surface** → Bind `127.0.0.1` only, require a shared secret, and never expose claim mutation without the confirm path. The endpoint is a transport for events, not an API.
- **Gateway touching the database** → It must not, ever. It has no reason to and no credential for it; the WAL-sidecar outage (2026-07-25) is what happens when something opens that file from the wrong side. Keep the DB out of the gateway's config and out of any workspace it can see.
- **Configuration divergence doubles** → `.env` already differs between the main checkout and the deploy worktree, unrecorded whether deliberately. A second runtime with its own config makes this worse before it makes it better. Deploy must report both runtimes' versions and health, and a partial start must read as failure.
- **Observability splits** → LLM spend stops being answerable from `llm_calls` alone. Accepted and documented, not hidden.
- **Two runtimes to keep alive** → ADR-0015's dead-updater restart logic was written because a silently dead updater is indistinguishable from a quiet day. That problem now belongs to the gateway daemon, and whether its supervision is as loud needs checking rather than trusting.

## Migration Plan

Each phase is independently shippable and leaves the system working. The old transport is retired last, and only after the UI is proven.

**Phase 0 — spikes (no production change).** Answer the four unknowns: buttons on photos; caption edit; Groq daily-exhaustion classification; whether the gateway can be pinned to a single authorized username. Any negative answer changes the plan before code is written.

**Phase 1 — MCP server, read-only, alongside everything.** Python MCP server with the read tools; registered with the gateway; no channel bound to the gateway, so Telegram is untouched and there is no 409 risk. Agent reachable via its own surface for testing. Existing bot fully live.

**Phase 2 — proposals and the confirm gate in the MCP server.** `propose_*` tools, pending-action records, the two harness refusals. Commit path still `telegram_bot`'s. Still no channel bound.

**Phase 3 — Telegram cutover (the atomic step).** Stop the Python updater behind a flag, bind the gateway's Telegram channel, enable the event-bridge plugin, switch outbound sends to `openclaw message send`. Verify the full card interface against real messages before declaring it done. Rollback: flip the flag, unbind the channel.

**Phase 4 — cron.** Move tick, Gmail ingest and daily nudge to gateway cron with the advisory lock in place. Remove APScheduler wiring. Reminders' catch-up sweep replaces `misfire_grace_time=None`.

**Phase 5 — deletion.** Remove `agent.py`'s loop, `llm.chat()`, `scheduler.py`, the updater code path, `python-telegram-bot` and `apscheduler` from `requirements.txt`. Update the module map, README, and the affected ADRs; supersede rather than delete their reasoning.

**Rollback posture.** Phases 1–2 are additive and reversible by unregistering the MCP server. Phase 3 is the only irreversible-feeling step and is reversible by flag until Phase 5 deletes the code. Do not start Phase 5 until Phase 3 has run through at least one full claim lifecycle on real data.

## Open Questions

- **Does the gateway's supervision announce a dead channel as loudly as ADR-0015's restart-on-dead-updater does?** If not, that alerting has to be rebuilt on top rather than assumed.
- **Should the OAuth token be narrowed?** `gmail.compose` is needed for drafts, so read-only is not available. Whether a tighter scope exists that permits drafts but not sends is unchecked.
- **ADR-0003, reminders and push.** Assistant-side reminders still surface only on the dashboard. The gateway makes push trivial, which turns a long-standing gap into a decision someone now has to make. Explicitly not decided here.
- **Which model for the agent, and does the chain need verifying against the real tool schema?** ADR-0017 requires end-to-end verification of every model in a chain before it is relied on. That requirement should apply to the gateway's chain too, but it is now configured outside this repo.
- **Where does the agent's own session history live, and is it backed up?** `db_backup.py` covers the SQLite database. The gateway's state directory is currently outside any backup.
- **Do additional channels re-open authorization?** The single-authorized-user check is username-based. On WhatsApp or Signal there is no Telegram username, so enabling a second channel needs a different identity rule — worth knowing before someone turns one on casually.


## Changelog

Append-only. Material decision changes only — findings live in `tasks.md`.

## 2026-08-01 — Gateway is the shell; domain stays Python
**Decision:** Adopt the OpenClaw gateway for transport, sessions, model routing and cron; keep the claims domain in Python untouched.
**Reasoning:** ~2312 of 8689 lines are a hand-rolled equivalent of the gateway's edges, with four ADRs existing only to document their failure modes. The ~5300 lines of domain logic were derived from real emails, PDFs and CSVs over a year and are the asset.
**Trade-off accepted:** Two runtimes, two configs, and a second update surface on a deploy that already has a `.env` divergence problem.
**Supersedes:** n/a.

## 2026-08-01 — Fit the OpenClaw architecture, don't bend it (Justin)
**Decision:** Where the gateway does something natively, use its version; keep bespoke only where losing it costs something real.
**Reasoning:** Stated as a constraint to settle future arguments in advance. It immediately caught D2 using an agent gateway as a dumb pipe.
**Trade-off accepted:** Several working, well-understood components go up for re-litigation (tasks §11), and some will be replaced by less familiar native equivalents.
**Supersedes:** n/a.

## 2026-08-01 — `telegram_messages` is kept (Justin)
**Decision:** Retain the table even where the gateway duplicates parts of it. Closed; not reopenable on fit-the-architecture grounds.
**Reasoning:** Asked directly, Justin confirmed the raw-payload training dataset is real and intended. The gateway is unlikely to store raw payloads tagged with *this app's* version.
**Trade-off accepted:** Accepted duplication of the audit-trail and replay jobs, uninvestigated. Also forces task 12.1 — a logging tee — since the app otherwise never sees conversation it must log.
**Supersedes:** the open audit item 11.1.

## 2026-08-01 — No MCP for deterministic calls (Justin)
**Decision:** Deterministic paths — taps, commands, unattended notifications — never involve MCP. It survives only as the conversational agent's read/propose surface.
**Reasoning:** Justin's concern about token cost, which measurement vindicated: every tool schema ships on every chat turn, and the default surface alone is 23.5k tokens against Groq's 12k TPM.
**Trade-off accepted:** Two mechanisms rather than one — plugin commands for deterministic work, MCP for conversation. Leaves the D3 gate-ownership question unresolved (flagged above).
**Supersedes:** the parts of D2/D3 that routed deterministic work through MCP.

## 2026-08-01 — Buttons are `command` actions, not callbacks (D12)
**Decision:** Every button carries `action.type: "command"` invoking a plugin-registered slash command.
**Reasoning:** Verified end to end. `callback` values are wrapped by `buildTelegramOpaqueCallbackData` before reaching Telegram, so the namespace resolver never sees them — they only work for a plugin's own send-and-decode round trip.
**Trade-off accepted:** Every tap must be expressible as a command string, subject to a payload limit not yet measured (18.5). Justin has accepted trimming to fit.
**Supersedes:** D2, D9, D10, D11.
**Amended 2026-08-01 (18.5), the limit is now measured:** 58 UTF-8 bytes — Telegram's 64-byte `callback_data` ceiling less the platform's 6-byte `tgcmd:` prefix. No trimming is needed for anything currently built: the longest realistic command is ~46 bytes because buttons already carry ids and indices rather than text. The trade-off that actually bites is not length, it is **how overflow presents**: the button is dropped from the keyboard with no error and `ok: true`, so the failure looks like a rendering bug rather than a payload one. The constraint is therefore a test (19a), not a style rule.

## 2026-08-01 — Media edits pass `caption` explicitly rather than relying on the fallback
**Decision:** `gateway_client.edit_message` sends the `caption` parameter whenever the target message carries media, instead of sending only `content` and letting the platform's `auto` mode work it out.
**Reasoning:** `auto` does work — a document caption edits correctly, verified live. But it works by calling `editMessageText` first, catching Telegram's `400 there is no text in the message to edit`, and retrying as a caption edit. The failed attempt is logged as `[telegram] editMessage failed: ...` on the **successful** path. Every tap on a PDF review alert or a rendered card would write an error-shaped line, which is precisely how a genuine edit failure stops being noticeable. Secondary: the fallback is a regex over an English Telegram error string; passing `caption` does not depend on it.
**Trade-off accepted:** `gateway_client` must know whether the message it is editing carries media. It already tracks this — the existing code chose caption-vs-text for the same reason under the old transport — so the cost is carrying that flag through, not discovering it.
**Supersedes:** n/a — resolves the open scenario "Caption editing unsupported by the action", which turns out not to be the case.

## 2026-08-01 — D10 written and retracted the same day
**Decision:** Retracted "the CLI cannot send interactive messages; the integration must be a plugin".
**Reasoning:** The five failed button attempts were one malformed payload — `buttons` at the top level of `presentation` instead of inside `blocks[{type:"buttons"}]`. The platform's own `normalizeMessagePresentation` rejects that shape and the send API returns `ok:true` regardless.
**Trade-off accepted:** None; it was simply wrong. Kept in place rather than deleted as the record of a wrong turn.
**Supersedes:** itself.

## Known limitations, recorded up front

- **A plugin is still required** — not for outbound rendering, but to register the app's slash commands. `command` actions invoke *native* commands, and `/mark` is not one.
- **A `command` button is deterministic only while its command is registered.** Measured 2026-08-01: an unregistered command in a button is not an error and not a no-op — it is delivered to the agent as a chat turn. So the guarantee "a tap never involves a model" rests on a deploy-time assertion, not on the mechanism itself. If that assertion is ever skipped, the failure mode is a commit token being read by an LLM.
- **The stock agent will introduce itself before it will work.** Its default bootstrap interviews the user about its own name, species and vibe, and on first contact it claimed to have checked email it has no credential for. Both must be dealt with in configuration; neither is something this design chose.
- **The token blocker is unsolved.** 23.5k per turn against Groq's 12k TPM. Until the surface is cut, the agent cannot run on the provider this project standardises on.
- **This platform fails silently** — seven distinct modes measured. Success responses and inspection output are not evidence. The seventh, found 2026-08-01: a command button over 58 bytes is deleted from the keyboard, and if it was the only one the message arrives with no keyboard at all, `ok: true`.
- **No ADRs written yet.** Tasks 8.1–8.6 plan them; the plugin-centric architecture, the no-MCP-for-deterministic rule and the command-not-callback mechanism all qualify as decisions that would surprise a newcomer. Until those exist, this document is the only record — which is a gap, not a design.
- **Unrecorded intent:** whether the `.env` divergence between checkout and worktree was ever deliberate is still unknown, and was not invented here.
