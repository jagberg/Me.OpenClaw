# ADR 0029: A plugin's `before_dispatch`/`message_received` hooks never invoke their handler in gateway 2026.7.1 — commands are the only proven dispatch path for a captionless Telegram document

- Status: accepted
- Date: 2026-08-10

## Context

`csv-upload-via-telegram` needed the gateway to hand the plugin a staged
Telegram document so it could forward the bytes to the app. The design
(`openspec/changes/archive/2026-08-10-csv-upload-via-telegram/design.md`,
Decision 6) planned to resolve this via a three-hook spike
(`message_received`, `inbound_claim`, `before_dispatch`) and pick whichever
one actually carried media metadata and fired for a plain document.

Reading the gateway's own shipped source (`toPluginMessageReceivedEvent` in
`telegram-ingress-spool` etc.) suggested `message_received` was the right
choice: fire-and-forget, no `pluginOwnedBinding` precondition, and its event
object includes `metadata.mediaPath`. `before_dispatch` — the hook this
plugin already uses live for `claims-pending-flow` — reads `event.context`
successfully in production, so it was assumed to work too, just without
media fields.

Both assumptions were live-tested and both were wrong in the same way.

## Decision

Neither hook invokes the plugin's handler for a real inbound document, in
this gateway version (`OpenClaw 2026.7.1`). Confirmed three separate ways:

1. `openclaw hooks list` reports `claims-csv-upload` (`before_dispatch`) and
   `claims-pending-flow` (`before_dispatch`) both `ready`.
2. A temporary diagnostic log placed as the FIRST line of the handler —
   logging on every invocation, not only matches — produced zero output
   across several real document sends, confirmed by grepping the full
   2.2MB `gateway.log` for the log's own literal string.
3. The staged file (`media/inbound/<name>---<uuid>.csv`) landed correctly
   every time; only the plugin's *reaction* to the message never ran. The
   document instead fell through to the agent as a chat turn every time
   (measured: a real Gemini `model-fetch` call fired for a captionless
   upload with no hook ever claiming it).

Root cause, read from the gateway's own `registry.js`: `registerHook`'s
internal wrapper is `async (evt) => { const context = evt.context; ... }` —
a single parameter. The dispatcher calls it as `hook.handler(event, ctx)` —
two positional arguments. `ctx` (the object actually carrying
`conversationId` etc.) is silently dropped; `evt.context` is `undefined` on
the merged event object `toPluginMessageReceivedEvent` returns (it has no
`context` key of its own). The wrapper then executes
`Object.hasOwn(context, "pluginConfig")` on `undefined`, throws, and the
gateway's own `handleHookError` swallows it with no log line visible in
either stdout or the log file. Nothing in the plugin's own code runs.

Why `claims-pending-flow` looked proven-live before this: nobody had
verified it against a real message since the gateway cutover. It is
registered identically and suffers the identical bug — "proven live for
months" in the codebase's own comments was carried forward from the
pre-cutover PTB era and never re-checked.

`/upload-tx` — a real registered COMMAND (`api.registerCommand`) — is the
actual working path. Send the CSV plain (no caption; Telegram gives no way
to attach a caption to a slash command from its own clients, measured
live), then send `/upload-tx`; the command handler finds the newest file in
`media/inbound` (10-minute window) and forwards it. Commands are the one
dispatch mechanism this project has verified live for months
(`mark`/`pet`/`resolve`/etc.), and they do not go through `registerHook` at
all — a different, unaffected code path.

## Alternatives considered

- **Fix the gateway's `registerHook` wrapper.** Not ours to fix — it's the
  vendored `ghcr.io/openclaw/openclaw` image, not this repo's code.
- **Wait for an upstream fix and keep building on `before_dispatch`.**
  Rejected: blocks the feature indefinitely on a third party's timeline for
  a bug this session couldn't even find a tracking issue for.
- **`inbound_claim`.** Never spiked once the other two failed for the same
  root cause — it additionally requires a `pluginOwnedBinding` this plugin
  never establishes, so it would need its own new mechanism regardless.

## Consequences

- The caption-less `before_dispatch` handler (`registerDocumentUpload` in
  `app/gateway-plugin/index.js`) is left registered anyway. It costs nothing
  while it never matches, and needs no changes if a future gateway version
  fixes the dispatch gap — at which point a document with no caption would
  start being handled automatically, in addition to `/upload-tx`.
- Every future feature that wants to react to an inbound message via a
  plugin hook should assume `before_dispatch`/`message_received` are
  unreliable in this gateway version until this is independently re-tested
  post-upgrade, and should default to a command-based design instead.
- `claims-pending-flow`'s condition-entry flow (task 4.3/12.2, the reason
  `before_dispatch` was adopted in the first place) is now suspect: it has
  not been proven live post-cutover either, by this same mechanism. Flagged
  here rather than fixed — verifying and, if broken, redesigning it is a
  separate piece of work.
