## Context

Justin wants three things: send the NetBank CSV to the bot instead of the dashboard; have
the upload itself start the claims scan; and be told what period is already covered so he
knows what to export from NetBank next.

**The relevant state of the world, verified rather than assumed (2026-08-09):**

- The Telegram cutover is **live**. The gateway holds the bot token and serves inbound
  messages (`docker logs` shows `[telegram] Inbound message … -> @bettyvet_bot`). The app
  no longer polls. Note the repo-root `CLAUDE.md` and
  `openspec/specs/openclaw-gateway-runtime/spec.md`'s scope note both still say the cutover
  has not happened; `openspec/changes/openclaw-telegram-cutover/` is shipped but unarchived.
  Believe the running system.
- `netbank_csv.parse` / `import_rows` already do everything the ingest needs: positional
  4-column parse, `CsvParseError` on the first bad row (nothing partial), and
  `INSERT OR IGNORE` against `UNIQUE(date, amount, merchant)`. Overlapping re-uploads are
  already the specified normal case.
- `import_rows` inserts **every** parsed row, not only vet ones — `vet_detection` flags
  afterwards. So `MAX(date) FROM bank_transactions` genuinely means "the last day the bank
  export covered", not "the last vet charge".
- `main.upload_transactions` already calls `pipeline.run_once()` after import. So
  requirement 2 exists on the dashboard path — but it calls it **outside**
  `internal_api.run_exclusive`, which the cron-driven `/internal/tick` uses. A dashboard
  upload landing during a tick therefore runs two concurrent pipelines today. That is a
  live defect, not a hypothetical, and this change is the right place to fix it because it
  is about to add a second caller of the same shape.
- Nothing in the repo handles inbound media. `media_outbox` is one-way, outbound, and its
  module comment says so.

**The gateway's actual inbound-media mechanism, read out of its shipped code**
(`/app/dist`, version 2026.7.1) rather than inferred:

- `hasInboundMedia` counts `msg.document` as inbound media, and `resolveInboundMediaFileId`
  returns `msg.document.file_id`. So a CSV attachment is media to the gateway, not an
  unhandled kind.
- Telegram media is downloaded through the Bot API (hard limit 20 MB — the gateway defines
  `TELEGRAM_BOT_API_FILE_DOWNLOAD_LIMIT_MB = 20` and its own error type for it) and staged
  with `saveMediaBuffer(buffer, contentType, "inbound", …)`.
- `saveMediaBuffer` writes to `resolveMediaScopedDir("inbound")` =
  `path.join(resolveConfigDir(), "media", "inbound")`, i.e.
  `/home/node/.openclaw/media/inbound/<uuid><ext>`, preserving the original filename in the
  saved id.
- The staged path reaches plugins as **event metadata**.
  `message-hook-mappers-CLWKm8aG.js` builds both `toPluginMessageReceivedEvent` and
  `toPluginInboundClaimEvent` with `metadata.mediaPath`, `mediaPaths`, `mediaType`,
  `mediaTypes`, `mediaUrl`, `mediaUrls`. This is the mechanism, and it is verified: an
  inbound attachment is a **local file path handed to the plugin**, not a stream and not a
  file id.

**And the blocker, measured live in the running container:**

```
/dev/sdd on /home/node/.openclaw       type ext4 (rw,relatime)
/dev/sdd on /home/node/.openclaw/media type ext4 (ro,relatime)
mkdir: cannot create directory '/home/node/.openclaw/media/inbound': Read-only file system
```

`docker-compose.yml` mounts `media_outbox` at `<configDir>/media` **read-only**, on purpose
(the gateway sends those cards, it does not write them). But that is exactly where the
gateway wants to stage inbound media. So on today's deployment the inbound staging write
must fail. This is the same trap the plugin's own comment already records for the outbound
`buffer` path — *"the gateway materialises that buffer under `<stateDir>/media/outbound`,
which compose mounts read-only by design, so every image failed with `ENOENT: mkdir`"* —
arriving from the other direction. Nobody has ever sent this bot a document (the gateway's
whole log shows text-only inbound), so it has never fired.

## Goals / Non-Goals

**Goals:**

- One ingest entrypoint serving two channels, so the dashboard and Telegram cannot drift.
- The upload's effect is visible in the reply: counts, claims found, coverage watermark.
- Coverage is derived, never stored.
- Every failure on this path names itself to Justin.
- No model involved anywhere in it.

**Non-Goals:**

- Any automated bank feed, scrape, or credential. Hard rule; this change exists *because*
  of it and must not be read as a step toward one.
- Inbound media in general. This is a CSV path, not an attachment framework. Invoices
  arrive by email and stay there.
- Removing the dashboard upload. Two channels, one entrypoint.
- Backfilling coverage for exports uploaded before this change — `MAX(date)` already
  answers for them, which is the point of deriving it.

## Decisions

### Decision 1 — How the bytes cross the boundary: the plugin reads the staged file and POSTs it

Chosen: the plugin's hook receives `metadata.mediaPath`, reads that file with `node:fs`,
and POSTs `{filename, content_b64, username, chat_id}` to a new secret-guarded
`/internal/transactions/csv`. The app authorizes, parses, imports, scans, and returns the
text to render.

Why: it is the only option that keeps the boundary the plugin already keeps — the app
decides everything, the plugin moves bytes — and it reuses the exact transport the plugin
already uses for every command (`callApp`, shared secret, correlation id). A NetBank export
is tens of kilobytes; the plugin's own inbound route already reads bodies up to 16 MB, so
base64 in JSON costs nothing worth engineering around.

Alternatives considered:

- **A shared volume for inbound, mirroring `media_outbox`.** Rejected. It widens the one
  narrow path between the containers into a second, inbound one, and `media_outbox.py`'s
  comment — *"this is the ONE path between the two containers, and it must never grow"* —
  is a rule written after this exact temptation. It also does not remove the read-only
  problem, it relocates it.
- **An MCP tool the agent calls with the file.** Rejected outright. It puts a model between
  Justin's bank data and the parser, spends tokens on a deterministic act, and the agent
  cannot be handed file bytes anyway.
- **The plugin downloads from Telegram itself** with the bot token it already holds for
  `setMyCommands` and `sendChatAction`, bypassing the media store entirely. Attractive —
  it sidesteps the read-only blocker completely — but the hook event exposes **no
  `file_id`**: the mapper's `metadata` carries media *paths*, not Telegram identifiers.
  Kept as the fallback if Decision 2's spike says the staged path is unreachable, but it
  needs a way to get the `file_id` that has not been found. See Open Questions.

### Decision 2 — Fix the read-only media mount rather than working around it

The inbound staging directory must be writable or nothing else in this design can run.
Preferred fix: **stop mounting `media_outbox` over the whole media directory.** Mount the
gateway's own writable state at `<configDir>/media` and bind the shared outbox one level
down, at `<configDir>/media/outbox`.

The evidence this can work: the media root allowlist is built by `buildMediaLocalRoots`,
which includes `path.join(configDir, "media")` and `path.join(stateDir, "media")` as
**roots**, and containment is checked with `isPathInside`. A subdirectory of an allowed
root is inside it. So the outbox keeps working from one level deeper — *in principle*.

It is written as "in principle" deliberately. The repo's own note (14.4) says the media
allowlist "is a fixed set of roots and ignores mounts it does not know about", and
"anywhere else fails with a path error that reads like a permissions problem". That note
was written from a failure, and reading the roots function is not the same as sending a
card from the new location. **This must be proved by sending one real card from
`media/outbox` before anything else in this change is built**, and `media_outbox.publish()`
returns the gateway-side path, so that return value changes with it.

Alternatives considered:

- **Move the whole gateway config dir.** Rejected: it moves sessions, the plugin registry
  and the pairing identity — 31 MB of non-regenerable state (see BACKLOG's compose-rename
  entry for what losing it looks like) — to solve a media problem.
- **Configure the inbound media directory elsewhere.** No such config key was found; the
  path is derived from `resolveConfigDir()` in `store.ts`. Absence of a key in a minified
  bundle is weak evidence, so it is listed as an open question rather than closed here.
- **Accept the failure and use the Telegram-download fallback** (Decision 1's third
  alternative). Viable only if the `file_id` question resolves.

### Decision 3 — Derive the watermark, do not store it

`SELECT MAX(date) FROM bank_transactions`. No new column, no new table, no maintained
value.

Two reasons, both this repo's own: a new column on the live DB needs a hand-run
`ALTER TABLE` because `CREATE TABLE IF NOT EXISTS` will not touch an existing table; and
"derive, don't store" is a recorded house move with three existing instances
(`submission_group_id`, the derived `matched` label, status-as-projection), for the reason
that a second answer to the same question eventually disagrees with the first. A stored
watermark that drifts from the transactions is worse than no watermark: it tells Justin a
period is covered when it is not, and he skips it in the next export.

The rejected alternative had a real benefit worth naming: a `csv_imports` table recording
each upload's filename, row counts and max date would give **upload provenance** —
"which file did this row come from" — which `MAX(date)` cannot answer. Nobody has asked for
that. If it is ever wanted, it is an audit-log change with its own reasoning, and the
watermark should still be derived from the transactions rather than read off the log.

Known limitation, inherited not introduced: `UNIQUE(date, amount, merchant)` means two
genuinely distinct identical charges — same merchant, same day, same amount — collapse to
one row. It is the pre-existing dedupe rule and this change does not touch it, but a
Telegram upload makes re-uploading cheap and therefore makes the rule easier to trip over.
Recorded rather than discovered later.

### Decision 4 — The scan runs under the tick's own lock, and the dashboard joins it

Both upload paths call one function that does: parse → import → `run_exclusive("tick",
pipeline.run_once)` → build the reply. The lock **name must be `"tick"`**, matching
`/internal/tick`, or the mutual exclusion is decorative.

`run_exclusive` returns `(ran, result)`, and `ran=False` is not an error — it means a tick
was already in flight. The reply must say so, exactly as `/internal/tick` does: a skipped
run reported as a completed one is the silent no-op the hard rules forbid.

This is why the dashboard path moves onto the same function rather than being left alone.
It has the defect today; leaving it means the same bug exists on one channel and not the
other, which is the worst of the three states.

### Decision 5 — The watermark goes on the upload reply and the dashboard panel

**Decided 2026-08-09 by Justin: take the recommendation; this is no longer an open question.**

"His summary should show the date of the most recent transaction already provided" admitted
more than one reading — two live summaries and a third plausible one — and the table below is
kept because the reasoning is what makes the choice re-checkable. But the distinction did not
warrant a decision round-trip: the two chosen surfaces cost two lines between them and neither
changes what an existing surface means.

| Surface | Case for | Case against |
|---|---|---|
| **The upload reply** | It is the moment he needs it: he has just uploaded, and the next question is what to export next time. Costs one line of text. | Only visible at upload time. |
| **The dashboard upload panel** | Sits next to the file input he uploads from, answers "what do I export" before he opens NetBank. One Jinja line. | Only visible if he opens the dashboard, which is the thing this change is reducing. |
| **The `/actions` summary card** | It is the closest thing to "his summary" as a noun, and it is on Telegram. | It answers a different question — what is *waiting on him* — and the watermark is not an action. The card is a rendered PNG whose height is computed per group, so a footer line is a real edit to `claim_card.render_actions_summary`. |

**The upload reply and the dashboard panel; not `/actions`.** The first two put the answer
where the question is asked and neither changes an existing surface's meaning. `/actions` is
the one that would: it answers "what is waiting on me", and a coverage watermark is not an
action. It is also the expensive one — a rendered PNG with per-group height computation, so a
footer line is a real edit to `claim_card.render_actions_summary`, not a string change.

If the watermark later proves to be something he wants at a glance without uploading, the
cheap move is a `/basic`-style line, not the `/actions` card.

### Decision 6 — Which hook the plugin registers: unresolved, spike first

Three candidates, none confirmed for this case:

- **`message_received`** — documented as "observe inbound content, sender, thread, and
  metadata", and its plugin event definitively carries `metadata.mediaPath` (read from the
  mapper). But the catalog marks it observation-only and it is dispatched
  fire-and-forget, so the message **also proceeds to the agent**. That violates this
  change's own spec ("a forwarded document does not become a model turn") and would spend
  tokens describing a file it did not import.
- **`inbound_claim`** — documented as "claim an inbound message before agent routing
  (synthetic replies)", which is exactly the semantics wanted, and its event also carries
  `metadata.mediaPath`. But the only call site found in the shipped dispatch bundle is
  `runInboundClaimForPluginOutcome`, reached when a **plugin owns the conversation
  binding**. A generic `runInboundClaim(` call site was not found. Grepping a minified
  bundle for an absence is weak evidence and it is recorded as a question, not a finding —
  this repo's own rule is that a silent result is not a finding.
- **`before_dispatch`** — what `registerPendingFlowClaim` already uses successfully, live,
  today. It is the *proven* hook in this codebase. But the gateway's own hook catalog lists
  it under outbound ("inspect or rewrite an outbound dispatch before channel handoff"),
  which contradicts the plugin's own comment quoting the product as "inspect or handle a
  message before model dispatch". And the existing handler reads `context.text` and returns
  early on empty text — a document with no caption has no text — while whether its event
  carries media metadata at all is **unverified**.

The rule that applies here is the one this repo paid a full session for: *validate against
the product's own behaviour before building on it.* So the first task is a spike that sends
one real CSV to the bot and logs, from inside a handler on each candidate hook, what
arrives. Nothing else is designed until that returns. A confidently-wrong hook here is
worse than the gap.

### Decision 7 — Authorization, logging and the reply stay app-side

The plugin passes the sender's username through; the app calls `commands.is_authorized`,
the same check `/internal/command` uses. It tees an inbound row via `tee_inbound` before
the work, and settles it after, so `telegram_messages` stays complete and "did my upload
register?" is answerable from the log rather than from container output — ADR-0014's whole
point.

An unauthorized document is refused, logged, and answered. It is not silently ignored: the
one thing worse than refusing Justin's file is refusing it invisibly.

## Risks / Trade-offs

- **The read-only media mount stops the whole design** → Decision 2, proved by a spike that
  sends one real card from the relocated outbox *before* anything else is built. If it
  cannot be made writable safely, fall back to Decision 1's third alternative, which needs
  the `file_id` question answered first.
- **The chosen hook does not fire for a caption-less document** → Decision 6's spike tests
  the real thing rather than reasoning from prose. If no hook delivers it, the honest
  outcome is that this change cannot ship as designed and says so, rather than shipping a
  path that works for a CSV *with* a caption and silently not otherwise.
- **A file that looks like a NetBank export but is another account's** → the parser already
  refuses anything not matching the 4-column layout, and refusal names the row. What it
  cannot detect is a *correctly shaped* export from the wrong account. Out of scope, and
  worth stating: nothing in the system distinguishes one Commbank account from another.
- **An upload runs a full pipeline synchronously** → a tick can take tens of seconds. The
  plugin's typing cue already covers slow commands and should cover this. If it proves too
  slow to hold a reply open, the fallback is to reply with the import counts immediately and
  push the scan's result as a second message — but that is two messages for one act, so it
  is a fallback, not the plan.
- **Telegram's 20 MB Bot API download ceiling and the gateway's own `mediaMaxMb`** → far
  above any NetBank export, but the failure must be legible if it ever fires rather than
  reading as a lost file.
- **Two upload channels, one lock, one entrypoint** → the trade is that the dashboard path
  changes as part of a Telegram change. Accepted deliberately: leaving the concurrency
  defect on one channel only is worse than a slightly wider diff.

## Migration Plan

No data migration, no schema change, no `ALTER TABLE` — which is the direct benefit of
Decision 3.

Deploy is the usual `./scripts/deploy.ps1` from the deploy worktree, with one ordering
constraint: the compose change from Decision 2 relocates the outbox mount, so the gateway
must come up with the new mount and be proved able to send a card from it before the
inbound path is exercised. Rollback is the previous compose file plus reverting the plugin;
nothing written by this change persists, so there is nothing to undo in the database.

## Open Questions

1. **Which hook delivers a document to a plugin?** (Decision 6.) The evidence found:
   `metadata.mediaPath` is definitely present on both `message_received` and
   `inbound_claim` plugin events; `message_received` is observation-only and lets the
   message reach the agent; a generic `inbound_claim` call site was **not found** in the
   shipped dispatch bundle, only the plugin-conversation-binding variant; `before_dispatch`
   works live in this repo but is catalogued as outbound and its media exposure is
   unverified. **Not resolvable from documentation** — the product's docs never mention
   attachments in any message hook. Needs one real CSV sent to the bot with instrumented
   handlers.
2. **Can `<configDir>/media` be made writable without breaking the outbound path?**
   (Decision 2.) The roots function says a subdirectory of `media` is inside an allowed
   root; the repo's 14.4 note says the roots are fixed and unforgiving. These are not
   flatly contradictory but they were written from opposite ends, and only a real send
   settles it.
3. **Is there any config key for the inbound media directory?** None found; the path is
   derived from `resolveConfigDir()`. Absence in a grep of minified output is not proof.
4. **Can the plugin obtain the Telegram `file_id`?** The hook metadata carries media paths,
   not Telegram identifiers. If it can, Decision 1's third alternative removes the media
   store from this design entirely and question 2 stops mattering.
5. **Should the watermark say anything about gaps?** `MAX(date)` says how far coverage
   *reaches*, not whether it is continuous. A missing middle week looks identical to a
   covered one. Nobody has asked for gap detection and it is not designed here, but the
   watermark's wording should not overclaim — "latest transaction held", not "covered
   through".
6. **Does an upload need to distinguish "new rows, no new claims" from "new rows, new
   claims"?** The reply reports both counts, which answers it factually. Whether a zero-claim
   upload deserves different wording is a UX question best answered after he has used it.
