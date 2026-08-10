/**
 * The app's command surface, registered inside the gateway.
 *
 * WHY THIS EXISTS AT ALL. A button carrying `action.type: "command"` invokes a
 * *native* slash command through core's command path — no model, no callback
 * token, nothing to forge. But `/mark`, `/pet` and `/resolve` are this app's
 * commands, not the gateway's, so something inside the gateway has to register
 * them. That is this file's entire job.
 *
 * WHAT IT MUST NOT DO. No claims logic. Every handler forwards to `/internal`
 * and renders whatever comes back. The rules about ceilings, statuses and
 * required fields live in Python and stay there; a second copy here would
 * drift, and drift in this particular code means a wrong claim submitted to an
 * insurer.
 *
 * THE HAZARD THIS GUARDS. An unregistered command in a button is not an error
 * and not a no-op. It reaches the agent as a chat turn and spends tokens —
 * measured live on 2026-08-01, three times, in Justin's own chat. So
 * `/mark 7 sent` arriving at a model as free text is one typo or one failed
 * load away. Both of a plugin's enablement gates fail *silently* (18.7), which
 * is why `register()` ends by telling the app what it actually registered:
 * `/health.gateway_plugin` stays empty if this never ran, and the deploy fails
 * on it rather than shipping a chat surface that quietly routes taps to an LLM.
 *
 * FOUR THINGS THAT ARE NOT STYLE CHOICES:
 *
 *   1. `definePluginEntry` is mandatory. A plain object export loads without
 *      error and never runs (18.7).
 *   2. `register()` must be SYNCHRONOUS. The gateway discards its return value,
 *      so an `async register()` loses every registration inside it (0.7). The
 *      boot report is fired off deliberately without awaiting.
 *   3. The SDK import is an absolute container path. The docs say to import
 *      `openclaw/plugin-sdk/plugin-entry`; from a `plugins.load.paths`
 *      directory that fails with ERR_MODULE_NOT_FOUND, because the plugin lives
 *      outside the app's module resolution. Verified 2026-08-02.
 *   4. Never diagnose this plugin with `plugins list`. Those fields come from a
 *      persisted registry that goes stale and reported `commands: []` for
 *      commands that demonstrably worked (18.6). Test the behaviour.
 */

import { definePluginEntry } from "/app/dist/plugin-sdk/plugin-entry.js";
import { dispatchGatewayMethod } from "/app/dist/plugin-sdk/gateway-method-runtime.js";
import { randomUUID } from "node:crypto";

const APP = process.env.CLAIMS_APP_URL ?? "http://app:8000";
const SECRET = process.env.CLAIMS_INTERNAL_SECRET ?? "";
const CHAT_ID = process.env.CLAIMS_TELEGRAM_CHAT_ID ?? "";
const BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN ?? "";
const VERSION = process.env.GATEWAY_PLUGIN_VERSION ?? "dev";

/**
 * The commands a button may emit. MUST match `gateway_client.BUTTON_COMMANDS`
 * in the Python app — the preflight compares the two and fails the deploy when
 * they disagree, because a button whose command nobody registered is a tap that
 * reaches the model.
 */
const COMMANDS = [
  { name: "mark", description: "Mark a drafted claim as sent" },
  { name: "pet", description: "Assign a pet to a claim" },
  { name: "resolve", description: "Confirm an action is dealt with" },
  { name: "history", description: "Claim history" },
  { name: "actions", description: "What is waiting on you" },
  // The Confirm button on a chat-initiated proposal. Its whole payload is
  // `/confirm <row id>` -- no free text, well inside the 58-byte budget. The
  // app commits on this and only on this (ADR-0027).
  { name: "confirm", description: "Confirm a proposed change" },
  // The remaining action-card taps. Each exists because its card has a button,
  // and a button whose command nobody registered is not an error -- the tap
  // reaches the agent as a chat turn and spends tokens.
  { name: "unmatch", description: "This invoice is the wrong one" },
  { name: "invreq", description: "I have sent the invoice request" },
  { name: "dismiss", description: "Reviewed, dismiss the mismatch" },
  { name: "merge", description: "One invoice, one claim -- merge these charges" },
  { name: "reject", description: "Not the same invoice" },
  // The per-item condition walk. `/item <n>` picks a prior condition,
  // `/item type` waits for free text, `/item skip` marks it unclaimable.
  { name: "item", description: "Answer the current invoice item" },
];

async function callApp(route, body, correlationId) {
  const response = await fetch(`${APP}/internal/${route}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-OpenClaw-Secret": SECRET,
      "X-Correlation-Id": correlationId,
    },
    body: JSON.stringify(body),
  });
  return { status: response.status, text: await response.text() };
}

/**
 * Correlate a tap across two runtimes.
 *
 * `ctx.messageId` is NOT populated in a command handler — found in 16.2, where
 * every correlation id came through with the `x` fallback. Until a field that
 * survives is identified (task 9.1), a counter at least distinguishes two taps
 * in the same second, which a constant did not.
 */
let sequence = 0;
// The counter is module-level, so it RESETS on every plugin reload -- and the
// plugin reloads on every deploy. Before the epoch prefix, `tg-actions-n1` was
// emitted again after each restart: two different taps, identical correlation
// id, in the log and (once the app began writing inbound rows) as a UNIQUE key
// collision that silently dropped the newer row. The prefix is per process, so
// ids stay short and stay unique across restarts.
const RUN = Date.now().toString(36).slice(-4);
function correlationId(name, ctx) {
  const anchor = ctx?.messageId ?? ctx?.message?.id ?? `${RUN}n${++sequence}`;
  return `tg-${name}-${anchor}`;
}

/**
 * Claim Telegram's per-chat command menu (13.1c).
 *
 * The gateway writes only the `default` and `all_group_chats` scopes, and
 * Telegram resolves a private chat's menu most-specific-first: chat →
 * all_private_chats → default. So this scope is unclaimed, and writing it gives
 * Justin a five-command menu without overwriting, racing or deleting anything
 * the gateway owns. Every other command stays callable — visibility and
 * callability are decoupled.
 *
 * Re-applied on every start, because the gateway rewrites its own scopes on
 * restart and a future version could widen its list to include ours. The
 * preflight asserts that list is still the two it writes today.
 */
async function claimCommandMenu(logger) {
  if (!BOT_TOKEN || !CHAT_ID) {
    logger?.warn?.(
      "claims: not claiming the chat command menu — CLAIMS_TELEGRAM_CHAT_ID or TELEGRAM_BOT_TOKEN unset. " +
        "Justin's menu will show the gateway's ~47 commands instead of the app's five.",
    );
    return;
  }
  const body = {
    commands: COMMANDS.map((c) => ({ command: c.name, description: c.description })),
    scope: { type: "chat", chat_id: Number(CHAT_ID) },
  };
  // The try/catch is load-bearing, not defensive habit. This runs fire-and-forget
  // from register() during gateway boot, so an unhandled rejection here — a DNS
  // failure, api.telegram.org unreachable — exits the Node process, and the
  // gateway restarts into the same failure. That is a boot loop, and the
  // restart-loop breaker trips at four unclean boots in five minutes.
  //
  // It was missing until the 2026-08-02 eval, while its sibling
  // reportRegistration had it. Harmless only because slice 1 sets no token and
  // this returns above; it would have armed itself on the cutover, which is
  // exactly the worst moment for the gateway to refuse to start.
  try {
    const response = await fetch(`https://api.telegram.org/bot${BOT_TOKEN}/setMyCommands`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const result = await response.json().catch(() => ({}));
    if (result?.ok) {
      logger?.info?.(`claims: chat command menu set to ${COMMANDS.length} entries`);
    } else {
      // Loud, not fatal. A wrong menu is cosmetic; a plugin that refused to load
      // over it would take the whole tap path down with it.
      logger?.warn?.(`claims: could not set the chat command menu: ${JSON.stringify(result)}`);
    }
  } catch (err) {
    logger?.warn?.(`claims: could not reach Telegram to set the chat command menu: ${String(err)}`);
  }
}

/**
 * Tell the app what `registerCommand` actually accepted.
 *
 * Self-reported, and the limit is worth stating: this is a runtime signal from
 * inside the registration call, which beats reading the persisted registry and
 * loses to a real tap. A deploy script cannot fake a tap against Justin's chat,
 * so this is the strongest available evidence that the plugin ran.
 *
 * IT CANNOT SEE A COLLISION, and that is not a small caveat. `registerCommand`
 * neither throws nor returns a failure when the name is taken: the gateway logs
 * `command registration failed: Command "mark" already registered by plugin
 * "<other>"` about a second later, asynchronously, long after `register()` has
 * returned. Observed 2026-08-02 with two plugins loaded — this function
 * cheerfully reported all five while three belonged to somebody else.
 *
 * So the report proves the plugin RAN, not that it OWNS the commands. The
 * second half is covered by `scripts/gateway_preflight.py`, which reads the
 * gateway's own log for those failures. Do not "improve" this into a claim of
 * ownership it cannot make.
 */
async function reportRegistration(names, logger) {
  try {
    const { status, text } = await callApp(
      "plugin/hello",
      { plugin: "claims", version: VERSION, commands: names },
      "plugin-hello",
    );
    if (status !== 200) {
      logger?.warn?.(`claims: the app rejected the boot report (${status}): ${text}`);
    }
  } catch (err) {
    // Almost always the two INTERNAL_API_SECRET copies disagreeing, or the app
    // not up yet. Either way the preflight fails the deploy naming it, so this
    // does not need to be fatal here.
    logger?.warn?.(`claims: could not reach the app to report registration: ${String(err)}`);
  }
}


/**
 * Claim Justin's next typed message when a flow owns it (task 4.3 / 12.2).
 *
 * `before_dispatch` runs AFTER command routing and BEFORE the model -- the
 * gateway's own words: "inspect or handle a message before model dispatch;
 * first handler returning { handled: true } wins". That ordering is why it is
 * the right hook and `inbound_claim` is not: a slash command must still work
 * while a condition-entry flow is pending.
 *
 * THE DECISION IS NOT HERE. This asks the app and obeys. A plugin that decided
 * for itself would be a second copy of "is a flow pending", and the whole point
 * is that Justin's typed condition reaches `condition_text` verbatim with no
 * model in between -- the field the hard rules forbid inferring.
 *
 * FAILS OPEN. Any error means not claimed, so the message reaches the agent
 * rather than vanishing; a lost message is worse than a stray chat turn. The
 * app logs the failure at ERROR on its side, because a flow that stopped
 * claiming is a condition entry silently going to a model.
 */
function registerPendingFlowClaim(api) {
  if (typeof api.registerHook !== "function") {
    // Loud, not fatal. Without this the condition flow still works on the
    // PTB path; after the cutover it would silently route to the agent.
    api.logger?.error?.("claims: api.registerHook is unavailable -- typed condition entry will reach the model");
    return;
  }
  api.registerHook(
    "before_dispatch",
    async (event) => {
      const context = event?.context ?? {};
      const text = context.text ?? context.message?.text ?? "";
      if (!text || text.startsWith("/")) return {};
      const correlation = correlationId("claim", context);
      try {
        const { status, text: body } = await callApp(
          "telegram/claim",
          {
            text,
            username: context.senderUsername ?? context.userName ?? context.username ?? null,
            chat_id: context.chatId ?? context.conversationId ?? null,
            // The app acks with a reaction; it needs the id of the message it
            // is reacting to. A command handler's context has no usable one
            // (16.2), which is why the ack lives on this path and not there.
            message_id: context.messageId ?? context.message?.id ?? null,
          },
          correlation,
        );
        if (status >= 400) return {};
        const answer = JSON.parse(body);
        if (!answer?.claimed) return {};
        return { handled: true, reply: { text: answer.reply || "" } };
      } catch (err) {
        api.logger?.error?.(`claims: pending-flow claim check failed, message passed to the agent: ${String(err)}`);
        return {};
      }
    },
    // Hook names are GLOBAL. A collision pushes an error diagnostic and returns
    // without registering -- the same silent-ish class as registerCommand's --
    // so the name is prefixed.
    { name: "claims-pending-flow", description: "Route a typed reply to the claim flow that is waiting for it" },
  );
}

/** Matches the gateway's own placeholder body text for a saved, captionless
 * document (`formatSavedMediaPlaceholder`, telegram-ingress-spool.js) --
 * `<media:document>`, or `<media:document> (N attachments)` for more than
 * one. Read from the gateway's shipped source, not guessed: it carries no
 * filename or path, which is why the handler below locates the staged file
 * itself rather than parsing one out of this string.
 */
const SAVED_DOCUMENT_PLACEHOLDER_RE = /^<media:document>/;

/**
 * Forward an inbound Telegram document (the NetBank CSV upload, task 6) to
 * the app, and reply with whatever it says -- all inside `before_dispatch`,
 * not `message_received` (task 1.1-1.4's spike, redone against the real
 * gateway rather than its source alone).
 *
 * WHY NOT `message_received`, DESPITE CARRYING `metadata.mediaPath`. Tried
 * first, and measured live: the gateway's own `registerHook` plumbing calls
 * a plugin's internal wrapper as `handler(event, ctx)` -- two positional
 * arguments -- but that wrapper reads `evt.context` off its SOLE parameter,
 * so `ctx` (the object actually carrying `conversationId` etc.) is silently
 * dropped and `evt.context` is `undefined`. The wrapper's own
 * `Object.hasOwn(context, "pluginConfig")` then throws before our handler is
 * ever called, caught by the gateway's own hook-error handler with no
 * visible trace on our side. Confirmed live: a real document staged
 * correctly (media/inbound had the file) and still reached the agent as a
 * chat turn, with zero app-side log activity for the forward that should
 * have happened.
 *
 * UPDATE, after switching to `before_dispatch`: it does not fire either.
 * `openclaw hooks list` reports this hook `ready`, and a real document send
 * produced zero occurrences of it (or of `claims-pending-flow`, its
 * neighbour) anywhere in a 2.2MB gateway log. Staging into `media/inbound`
 * itself is unaffected and confirmed reliable every time -- it is
 * specifically a plugin hook's reaction to the message that this gateway
 * version (2026.7.1) never runs, for either hook this plugin registers on
 * `before_dispatch`. This handler is kept anyway: it costs nothing while it
 * never matches, and would need no changes if a future gateway version
 * fixes the dispatch gap. `registerUploadTxCommand` below is the path that
 * actually works today, because commands are the one dispatch mechanism
 * this project has verified live for months.
 *
 * WHY LOCATE THE FILE RATHER THAN READ A PATH OFF THE EVENT. `before_dispatch`
 * carries no media field at all (confirmed by reading `runBeforeDispatch`'s
 * two argument objects in `dispatch-B9if0XZc.js`), and the placeholder text
 * it DOES carry names no file. But the Telegram ingress step stages the
 * file into `media/inbound/` and constructs that same placeholder text in
 * one synchronous pass before `before_dispatch` ever runs, and this is a
 * single-user bot with no concurrent uploads -- so "the newest file in
 * media/inbound" is the file, not a guess. `mediaPath` metadata would be
 * more precise if a hook actually delivered it; none does.
 *
 * DOUBLES AS THE FALLTHROUGH GUARD (task 6.3): returning `{ handled: true }`
 * from `before_dispatch` is what stops the agent seeing the message at all
 * -- there is no separate suppression step needed once the file is handled
 * inside the hook that can actually say so.
 *
 * FAILS VISIBLY, NOT SILENTLY. Every branch that does not forward the file
 * replies with why -- never a bare "ok" and never nothing, per the hard
 * rule against silent no-ops. Registered SEPARATELY from
 * `claims-pending-flow` (own hook name) so a throw in one cannot take out
 * the other -- `runClaimingHooksList` tries the next handler on error.
 */
const INBOUND_MEDIA_DIR = "/home/node/.openclaw/media/inbound";

/**
 * The newest staged file in `media/inbound`, or `null`. Shared by both
 * upload paths below -- caption-less (`before_dispatch`) and `/upload-tx`
 * (a real command) -- because neither hook nor command carries a media path
 * (task 1.1-1.4's spike; see `registerDocumentUpload`'s docstring), and this
 * is single-user with no concurrent uploads, so "the newest file" is THE
 * file, not a guess.
 *
 * `maxAgeMs` matters for `/upload-tx` specifically: typed with no
 * attachment, it must not silently reprocess whatever was staged from an
 * earlier, unrelated upload minutes or hours ago.
 */
async function findNewestStagedFile(maxAgeMs) {
  const fs = await import("node:fs/promises");
  const path = await import("node:path");
  const entries = await fs.readdir(INBOUND_MEDIA_DIR).catch(() => []);
  if (entries.length === 0) return null;
  const stats = await Promise.all(
    entries.map(async (name) => {
      const full = path.join(INBOUND_MEDIA_DIR, name);
      return { full, mtimeMs: (await fs.stat(full)).mtimeMs };
    }),
  );
  stats.sort((a, b) => b.mtimeMs - a.mtimeMs);
  const newest = stats[0];
  if (maxAgeMs != null && Date.now() - newest.mtimeMs > maxAgeMs) return null;
  return { path: newest.full, basename: path.basename(newest.full) };
}

/** Reads and forwards a staged file to the app's CSV import route; returns
 * the reply text either way -- never a bare "ok" and never nothing. */
async function forwardStagedCsv(mediaPath, { username, correlation }) {
  const fs = await import("node:fs/promises");
  const path = await import("node:path");
  const bytes = await fs.readFile(mediaPath);
  const { status, text } = await callApp(
    "transactions/csv",
    { filename: path.basename(mediaPath), content_b64: bytes.toString("base64"), username, chat_id: CHAT_ID },
    correlation,
  );
  return status >= 400 ? `Upload failed (${status}). ${text}`.slice(0, 3500) : text.slice(0, 3500);
}

function registerDocumentUpload(api) {
  if (typeof api.registerHook !== "function") {
    api.logger?.error?.("claims: api.registerHook is unavailable -- an inbound CSV upload will reach the model as chat");
    return;
  }
  api.registerHook(
    "before_dispatch",
    async (event) => {
      const context = event?.context ?? {};
      const text = context.text ?? context.message?.text ?? event?.content ?? "";
      if (!SAVED_DOCUMENT_PLACEHOLDER_RE.test(text)) return {};

      const correlation = correlationId("csv-upload", context);
      const username = context.senderUsername ?? context.userName ?? context.username ?? null;
      try {
        const staged = await findNewestStagedFile(null);
        if (!staged) {
          throw new Error(`the placeholder said a document was saved, but ${INBOUND_MEDIA_DIR} is empty`);
        }
        const replyText = await forwardStagedCsv(staged.path, { username, correlation });
        return { handled: true, text: replyText, reply: { text: replyText } };
      } catch (err) {
        api.logger?.error?.(`claims: CSV upload forwarding failed: ${String(err)}`);
        const failText = `Upload failed -- could not read or forward the file: ${String(err)}`.slice(0, 3500);
        return { handled: true, text: failText, reply: { text: failText } };
      }
    },
    { name: "claims-csv-upload", description: "Forward an inbound document to the app's CSV import route" },
  );
}

/**
 * `/upload-tx`: send the CSV first (plain, no caption), then this command
 * to actually import it. Two messages, not one -- Telegram's clients have no
 * way to attach a caption while composing a slash command (measured live),
 * so a command can never carry the file itself.
 *
 * WHY THIS IS THE ONLY RELIABLE PATH. `registerDocumentUpload`'s
 * `before_dispatch` hook -- and, before that, a `message_received` hook --
 * were both measured live to never invoke the plugin's handler at all,
 * despite `openclaw hooks list` reporting both `claims-pending-flow` (this
 * plugin's OTHER before_dispatch hook) and `claims-csv-upload` as
 * `ready`, and despite a full 2.2MB gateway log carrying zero occurrences
 * of either hook's own diagnostic output for a real inbound document. The
 * file itself stages into `media/inbound` reliably every time (confirmed
 * repeatedly) -- it is specifically the plugin's hook-driven reaction to it
 * that this gateway version never runs. Commands are the one dispatch path
 * that has worked throughout this project (mark/pet/resolve/etc, unchanged
 * for months), so this reuses that path instead of a third attempt at a
 * hook. The `before_dispatch` handler above is left in place in case a
 * future gateway version fixes the dispatch gap; it costs nothing while
 * broken since it simply never matches.
 *
 * A generous window (10 minutes) because the two messages are typed
 * separately by a person, not machine-paced.
 */
function registerUploadTxCommand(api) {
  if (typeof api.registerCommand !== "function") {
    api.logger?.error?.("claims: api.registerCommand is unavailable -- /upload-tx cannot import the file");
    return;
  }
  api.registerCommand({
    name: "upload-tx",
    description: "Import the NetBank CSV just sent",
    acceptsArgs: false,
    handler: async (ctx) => {
      const correlation = correlationId("upload-tx", ctx);
      const username = ctx.senderUsername ?? ctx.userName ?? ctx.username ?? null;
      try {
        const staged = await findNewestStagedFile(10 * 60 * 1000);
        if (!staged) {
          return {
            text: "No recent file found -- send the CSV as a document attachment (no caption), then /upload-tx.",
          };
        }
        const text = await forwardStagedCsv(staged.path, { username, correlation });
        return { text };
      } catch (err) {
        return { text: `/upload-tx could not read or forward the file: ${String(err)}`.slice(0, 3500) };
      }
    },
  });
}

/**
 * "typing…" while a command runs — Justin's stated preference over any reaction.
 *
 * WHY THE PLUGIN AND NOT THE GATEWAY. The gateway does send a typing cue, from
 * the Telegram ingress path, for every non-`room_event` inbound message, and it
 * keeps typing alive for the length of an AGENT turn (`typing: { start:
 * sendTyping }` on the reply pipeline). Neither reaches a command:
 *
 *   * a button TAP is a callback query, not a message, so there is no inbound
 *     message to trigger the ingress cue at all — which is also why a tap gets
 *     no ack reaction, no matter how it is configured;
 *   * a typed `/actions` does get the single ingress cue, but Telegram expires
 *     typing after ~5s and `/actions` takes ~6s, so it stops before the cards
 *     land.
 *
 * So the plugin re-sends `sendChatAction` every 4s for as long as the app is
 * working, using the bot token it already holds for `setMyCommands`. Telegram's
 * own API, no gateway surface needed, and it covers the tap path.
 *
 * Fire-and-forget and individually caught: a failed cue must never delay or
 * break the command it is decorating.
 */
function startTypingCue(chatId, logger) {
  // A numeric chat id, or nothing. The first version took `ctx.channelId ??
  // ctx.from ?? ctx.senderId ?? CHAT_ID` and passed it through `Number()`, and
  // those fields carry channel-qualified strings like "telegram:8995277418" ->
  // NaN -> Telegram 400. Worse, only network errors were caught, so a rejected
  // cue logged NOTHING and "typing never showed" was indistinguishable from
  // "typing was never attempted". That is the silent no-op this project forbids,
  // and it cost the one deploy it took Justin to say he saw no typing.
  const numeric = String(chatId ?? "").match(/-?\d+/)?.[0] ?? String(CHAT_ID).match(/-?\d+/)?.[0];
  if (!BOT_TOKEN || !numeric) {
    logger?.warn?.(`claims: no typing cue -- token=${Boolean(BOT_TOKEN)} chat=${String(chatId)}`);
    return () => {};
  }
  let announced = false;
  const cue = () => {
    fetch(`https://api.telegram.org/bot${BOT_TOKEN}/sendChatAction`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: Number(numeric), action: "typing" }),
    })
      .then(async (response) => {
        const result = await response.json().catch(() => ({}));
        // Telegram answers 200 with `ok: false` for a bad chat, so the status
        // code alone proves nothing. Announced once per run, not per cue.
        if (!result?.ok && !announced) {
          announced = true;
          logger?.warn?.(`claims: typing cue rejected for chat ${numeric}: ${JSON.stringify(result)}`);
        }
      })
      .catch((err) => logger?.warn?.(`claims: typing cue failed: ${String(err)}`));
  };
  cue();
  // 4s, inside Telegram's ~5s expiry, so the indicator never blinks off mid-run.
  const timer = setInterval(cue, 4000);
  return () => clearInterval(timer);
}

/**
 * NO CUSTOM ACK HOOK, DELIBERATELY.
 *
 * Two deploys went into hand-rolling a 👍 here -- first on `message_received`,
 * then on `inbound_claim` -- and neither could have worked, for two reasons that
 * had nothing to do with hooks:
 *
 *   1. The gateway already does this. `messages.ackReaction` +
 *      `messages.ackReactionScope` are shipped config, and the default scope is
 *      `group-mentions`. Justin's chat is a DM, so the feature was configured
 *      off for the only chat that exists here. `scripts/gateway_seed.sh` now
 *      sets it to "all", and turns on `messages.statusReactions` for the
 *      queued -> thinking -> done lifecycle emoji.
 *   2. A TAP can never be acked by anyone. It arrives as a callback query, not a
 *      message, so there is no message to react to -- and the ctx a plugin
 *      command handler receives (`commands-CDhgE9eG.js`) carries no message id
 *      under any name. `startTypingCue` above is what covers the tap path.
 *
 * `/internal/telegram/ack` and `notify.ack` stay in the app: they are the
 * PTB-era path, still exercised by the suite, and section 6 removes that half
 * wholesale rather than piecemeal.
 */

/**
 * The fast outbound path: send from INSIDE the gateway (task 4.14).
 *
 * WHY. `openclaw message send` costs 9-13s per message. Measured 2026-08-03
 * from the app container: `--version` 0.06s, `--dry-run` **6.6s with no gateway
 * contact whatsoever** (the gateway logged zero RPCs for three of them),
 * `health` 2.4s, and the gateway's own `message.action` 0.3-1.1s landing in the
 * final second. So the dominant cost is the CLI initialising itself, once per
 * message, and one process per message means it never amortises. Five
 * concurrent sends took ~20s each rather than ~10s, because that init is
 * CPU-bound and they fight for cores.
 *
 * `dispatchGatewayMethod` runs the same `message.action` the CLI ends up
 * calling, in this process, with no spawn, no WebSocket and no handshake.
 *
 * THE TWO THINGS THAT MAKE IT WORK, both read out of the gateway's own shipped
 * code rather than assumed:
 *
 *   1. `contracts.gatewayMethodDispatch: ["authenticated-request"]` in
 *      `openclaw.plugin.json`. Without it `dispatchGatewayMethod` throws by
 *      design — `registry` sets `gatewayMethodDispatchAllowed` from the
 *      manifest contract alone.
 *   2. `auth: "gateway"` plus `gatewayRuntimeScopeSurface: "trusted-operator"`.
 *      A `plugin`-auth route is handed an EMPTY scope list
 *      (`createPluginRouteRuntimeClient`), so a write would be refused;
 *      `trusted-operator` with shared-secret bearer auth resolves to the CLI's
 *      own default operator scopes. `/app/dist/extensions/admin-http-rpc` is
 *      the product's reference implementation of this exact shape.
 *
 * Cards are sent SEQUENTIALLY and that is a feature, not laziness: in-process
 * each dispatch costs what the gateway costs, so ordering is finally free, and
 * `/actions` can put its summary card first again without paying a second round.
 *
 * Media travels as a PATH through the shared outbox, exactly as it does on the
 * CLI path. `SendParamsSchema.buffer` takes base64 and was the first thing
 * tried, because it would have removed the volume entirely — but the gateway
 * materialises that buffer under `<stateDir>/media/outbound`, which compose
 * mounts read-only by design (14.2), so every image failed with `ENOENT: mkdir
 * '/home/node/.openclaw/media/outbound'`. Publishing to the outbox costs 9ms
 * and keeps the narrow mount.
 */
async function readJsonBody(req, maxBytes) {
  const chunks = [];
  let total = 0;
  for await (const chunk of req) {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    total += buffer.byteLength;
    if (total > maxBytes) return { ok: false, status: 413, message: "payload too large" };
    chunks.push(buffer);
  }
  const raw = Buffer.concat(chunks).toString("utf8");
  if (!raw.trim()) return { ok: false, status: 400, message: "request body must be JSON" };
  try {
    return { ok: true, value: JSON.parse(raw) };
  } catch {
    return { ok: false, status: 400, message: "request body must be valid JSON" };
  }
}

function sendJson(res, status, body) {
  res.statusCode = status;
  res.setHeader("Cache-Control", "no-store");
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.end(JSON.stringify(body));
}

/** One card -> one `message.action` dispatch. Returns null on success, else why. */
async function dispatchCard(card, channel, logger) {
  const target = String(card.target ?? "").trim();
  if (!target) return "card has no target";
  const idempotencyKey = randomUUID();
  const params = { to: `${channel}:${target}`, idempotencyKey };
  if (card.message) params.message = String(card.message);
  // A path inside the gateway's own media roots, published by the app to the
  // shared outbox. NOT base64: `SendParamsSchema.buffer` exists and was tried
  // first, but the gateway materialises it under `<stateDir>/media/outbound`,
  // which compose mounts read-only on purpose -- every image failed with
  // `ENOENT: mkdir '/home/node/.openclaw/media/outbound'`.
  if (card.media_url) params.mediaUrl = String(card.media_url);
  // The app builds this with `gateway_client.build_buttons`, which is validated
  // against the platform's own normalizer. A malformed presentation is
  // discarded SILENTLY and the send still reports success, so it is never
  // hand-written on either side of this boundary.
  if (card.presentation) params.presentation = card.presentation;
  if (!params.message && !params.mediaUrl) return "card has neither text nor media";

  try {
    const response = await dispatchGatewayMethod("message.action", {
      channel,
      action: "send",
      params,
      idempotencyKey,
    });
    if (response?.ok) return null;
    const error = response?.error;
    return `${error?.code ?? "UNKNOWN"}: ${error?.message ?? "message.action failed"}`;
  } catch (err) {
    logger?.error?.(`claims: in-process send threw: ${String(err)}`);
    return String(err);
  }
}

function registerSendRoute(api) {
  if (typeof api.registerHttpRoute !== "function") {
    // Loud, not fatal: the app falls back to the CLI, which is slow but correct.
    api.logger?.error?.(
      "claims: api.registerHttpRoute is unavailable -- outbound sends will keep paying the ~9s CLI cost",
    );
    return;
  }
  api.registerHttpRoute({
    path: "/api/v1/claims/send",
    auth: "gateway",
    match: "exact",
    gatewayRuntimeScopeSurface: "trusted-operator",
    handler: async (req, res) => {
      if ((req.method ?? "GET").toUpperCase() !== "POST") {
        res.setHeader("Allow", "POST");
        sendJson(res, 405, { ok: false, error: "method not allowed" });
        return true;
      }
      // A rendered card is ~45KB of PNG, ~60KB base64; a review PDF is larger.
      const body = await readJsonBody(req, 16 * 1024 * 1024);
      if (!body.ok) {
        sendJson(res, body.status, { ok: false, error: body.message });
        return true;
      }
      const cards = Array.isArray(body.value?.cards) ? body.value.cards : [];
      if (cards.length === 0) {
        sendJson(res, 400, { ok: false, error: "cards must be a non-empty array" });
        return true;
      }
      const channel = String(body.value?.channel ?? "telegram");
      const failures = [];
      let sent = 0;
      for (const [index, card] of cards.entries()) {
        const failure = await dispatchCard(card, channel, api.logger);
        if (failure === null) sent += 1;
        else failures.push({ card: index, reason: failure });
      }
      // Partial success is reported as such and never as ok. A card that did not
      // arrive must not read like one that did.
      sendJson(res, failures.length === 0 ? 200 : 502, {
        ok: failures.length === 0,
        sent,
        failures,
      });
      return true;
    },
  });
  api.logger?.info?.("claims: in-process send route registered on /api/v1/claims/send");
}
export default definePluginEntry({
  id: "claims",
  name: "OpenClaw Claims",
  description: "Registers the claims app's slash commands and forwards them to it. No claims logic here.",

  register(api) {
    const registered = [];

    for (const { name, description } of COMMANDS) {
      api.registerCommand({
        name,
        description,
        acceptsArgs: true,
        handler: async (ctx) => {
          const args = ctx.args ?? "";
          const correlation = correlationId(name, ctx);
          // A command ctx carries no message id, but it does carry the chat:
          // `channelId`, else the DM's sender. CLAIMS_TELEGRAM_CHAT_ID is the
          // floor -- one user, one chat.
          const stopTyping = startTypingCue(
            ctx.channelId ?? ctx.from ?? ctx.senderId ?? CHAT_ID, api.logger);
          try {
            const { status, text } = await callApp(`command/${name}`, { args }, correlation);
            if (status >= 400) {
              // Visible failure, never a silent no-op: the tap did nothing and
              // must say so, or Justin reasonably assumes it worked.
              return { text: `/${name} failed (${status}). ${text}`.slice(0, 3500) };
            }
            return { text: text.slice(0, 3500) };
          } catch (err) {
            return { text: `/${name} could not reach the claims app: ${String(err)}` };
          } finally {
            stopTyping();
          }
        },
      });
      registered.push(name);
    }

    // Deliberately not awaited — `register()` must stay synchronous or the
    // gateway discards every registration made above (0.7).
    registerPendingFlowClaim(api);
    registerSendRoute(api);
    registerDocumentUpload(api);
    registerUploadTxCommand(api);

    void claimCommandMenu(api.logger);
    void reportRegistration(registered, api.logger);

    api.logger?.info?.(`claims registered: ${registered.join(", ")}`);
  },
});
