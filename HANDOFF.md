# Handoff — 2026-08-06

Written at the end of a long session, for a fresh session with no memory of it.
Everything below was verified live on 2026-08-06, not recalled. Read root
`CLAUDE.md` first for the standing rules; this file only carries what is *true
right now* and what is *still open*.

**Delete this file once its open items are closed.** A stale handoff is worse
than none.

## What is running

```
meopenclaw-telegram-claimquery-app-1      11432d4+deploy    :8000    FastAPI app
meopenclaw-telegram-claimquery-gateway-1  OpenClaw 2026.7.1 :18789   gateway (healthy)
```

Deployed SHA equals `origin/master` (`11432d4`). The cutover is done: the
**gateway** owns the Telegram bot token, the five cron jobs and the chat agent;
the **app** owns the SQLite DB, Gmail and all claims logic. They talk over
`/internal/*` with a shared secret. `SCHEDULER_ENABLED=0` and
`TELEGRAM_UPDATER_ENABLED=0` — APScheduler and the PTB poller are both off.

Verify state with `curl -s http://localhost:8000/health` (app version, gateway
version, plugin command count, scheduler owner + overdue list, last inbound) and
`docker exec <gateway> openclaw cron list` (five `claims.*` declarations, all
persisted in the `gateway_state` volume, so they survive a container recreate).

## Where things live, and the two traps

| what | where |
|---|---|
| compose + deploy script | `C:\Code\Me.OpenClaw-telegram-claimquery` (branch `deploy`) |
| **the live DB** | `C:\Code\Me.OpenClaw\app\data\openclaw.db` — bound into both containers |
| master | checked out in `C:\Code\Me.OpenClaw-settlement`, **not** the main checkout |

**Trap 1 — the wrong `app/data`.** Compose binds the *main checkout's* data dir.
Every other worktree has its own `app/data/openclaw.db`, and they are stale
copies. Reading one silently returns plausible-but-old rows. I did this on
2026-08-06 and concluded "no messages since 31 July" from a DB that had stopped
being written weeks earlier. Always name the path in full:
`sqlite3.connect("file:C:/Code/Me.OpenClaw/app/data/openclaw.db?mode=ro", uri=True)`.

**Trap 2 — read-only or you take the app down.** A plain `connect()` checkpoints
and deletes the WAL sidecars from the Windows side, and every `get_connection()`
in the container then fails. This is ADR-0018 and it caused a 51-minute outage.
To *write* to the live DB, run the code inside the app container.

**Timestamps in the DB and `/health` are UTC.** Local is +10. I briefly declared
cron dead because a `last_ok` ten hours old was in fact fourteen minutes old.

## Deploying

From `C:\Code\Me.OpenClaw-telegram-claimquery`:
`git switch -C deploy origin/master` then `./scripts/deploy.ps1`.

**Known defect: the script reports `DEPLOY FAILED` on a good deploy.** Its
gateway health probe races Docker's `start_period`, so a healthy start reads as
`UNREACHABLE`. Two consequences: the script cannot be trusted to catch a *real*
partial start, which is the only reason it exists; and because it throws, its
post-boot cron-seeding step never runs. Harmless while declarations persist in
the volume, but a deploy that genuinely needed re-seeding would skip it in
silence. Fixing this is worth doing before the next deploy.

## Live faults, open

1. **The gateway heartbeat is burning the Gemini free-tier quota.** The gateway
   logs `[heartbeat] started` at boot and runs an agent turn every 30 minutes;
   every one has ended `failoverReason: rate_limit`, `gemini-2.5-flash`, `429`
   since at least 2026-08-05 16:13 UTC. Four fallback models are configured and
   all four are declared in config, so this is not the "fallbacks name
   undeclared models" trap — the daily quota is genuinely spent. **Effect: taps
   work, typed chat replies "API rate limit reached".** Lengthening or disabling
   the heartbeat is the first thing to try.
2. **The gateway is 2026.7.1; every CLI flag in `gateway_client.py` was verified
   against 2026.6.34.** Unreviewed since the bump. Re-verify against
   `openclaw message <sub> --help` before trusting a send path.
3. **`vet-nudge: never`** on `/health`. Correct, not a fault — it is weekly on
   Mondays and was declared on a Tuesday. First real firing is 2026-08-10, which
   is also the first proof that cadence works.

## Open work

- **`openclaw-telegram-cutover` section 6, scheduled Mon 2026-08-10.** Deletes
  `telegram_bot.py`, `scheduler.py`, the apscheduler dependency,
  `TELEGRAM_UPDATER_ENABLED` and `SCHEDULER_ENABLED`. Section 10's remaining
  tests wait on it because they test code that is about to go.
- **Section 9**, seven items left: 9.5, 9.7, 9.8, 9.9, 9.10, 9.11, 9.12. 9.12
  (dead-channel alert at ADR-0015 levels) is the substantive one; most of the
  rest are write-downs. 9.4 is `[~]` on purpose — its edit-parity half cannot be
  asserted while nothing on the gateway path produces an `edited_message`.
- **Six openspec changes remain in-progress** and are deliberately not archived.
  `telegram-agent-reach` (44/47) and `submission-group-id` (28/30) are close;
  their stragglers need `openspec/BACKLOG.md` entries first, because archiving
  with open tasks is how an open item vanishes.

## Decisions waiting on Justin

- **DC1-26-5992 Sr 4 — $135.00 claimed, $87.75 paid, matching no claim.** Not a
  bug: verified there is no $135 charge anywhere in the bank CSV, and no claim
  holds that serial (1, 2 and 3 are on claims #6, #1, #12). Either an invoice
  never captured, or Petcover assessed one we hold as several treatments. Only
  **Petcover's own status table** can say — it states a treatment date per
  serial, and it is already on record as the authority that proved our serial
  map wrong on all ten.
- **Claim #2 under-reports by that same $87.75**, and linking event #91 to it
  would make `_latest_settlement_detail` misattribute the money. Surfacing a
  letter and attributing its money are separate acts; the schema change is the
  real fix. See `openspec/BACKLOG.md`.
- **A `/link` verb** would make unlinked letters tappable. Today they are
  dashboard-only and `actionable: False`, because an unregistered verb reaches
  the agent as a chat turn and spends tokens.
- **The compose project name** is `meopenclaw-telegram-claimquery`, taken from a
  stale feature-branch directory name. Renaming orphans three volumes, and
  `gateway_state` (31 MB: pairing identity, agent sessions, plugin registry,
  cron state) is not regenerable — it would read as "the gateway forgot
  everything". Safe route is `name:` in compose plus explicit `volumes: {...:
  {name: ...}}` pinning the existing ones. The SQLite DB is unaffected: `/data`
  is a bind, not a volume.

## Repository hygiene, before the next session

1. **Push local `master`.** It is one commit ahead of `origin/master`
   (`e9e552d`, from a parallel session). Local and remote disagreeing is the
   single most confusing state to inherit.
2. **Reattach `C:\Code\Me.OpenClaw` to master.** It sits detached at `95ee5af`,
   far behind, while `master` is checked out in `Me.OpenClaw-settlement`. The
   main checkout is the natural home for master and is also where the live data
   dir lives.
3. **Prune finished worktrees** — `feature/logging-parity`,
   `feature/integration` and `feature/unlinked-letters` are all merged.
4. **One session at a time against this repo.** Most of 2026-08-06's confusion
   came from two sessions writing the same master and the same live DB — a
   hand-run serial remap from one while the other served four-commit-old code,
   and a `docker cp` hot patch into `/app-new` that was never on the import path
   and never took effect.
