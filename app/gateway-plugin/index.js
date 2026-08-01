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
function correlationId(name, ctx) {
  const anchor = ctx?.messageId ?? ctx?.message?.id ?? `n${++sequence}`;
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
          }
        },
      });
      registered.push(name);
    }

    // Deliberately not awaited — `register()` must stay synchronous or the
    // gateway discards every registration made above (0.7).
    void claimCommandMenu(api.logger);
    void reportRegistration(registered, api.logger);

    api.logger?.info?.(`claims registered: ${registered.join(", ")}`);
  },
});
