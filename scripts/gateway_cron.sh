#!/bin/sh
# The five schedules, declared into the gateway's cron. Task 5.1.
#
# WHY THIS IS A SEPARATE SCRIPT FROM gateway_seed.sh.
#
# The seed runs PRE-boot and writes a config file, which needs no gateway. Cron
# jobs are not config: `openclaw cron` is "Manage cron jobs (via Gateway)" and
# `cron.add` is a gateway RPC method, so this can only run once the gateway is up.
# Checked before assuming -- `config get cron` returns scheduler settings
# (`enabled`, `retry`, `runLog`, `maxConcurrentRuns`) and no job definitions, and
# the plugin SDK exposes no cron surface at all (no registerCron/declareCron).
# Two scripts because they run at two different times, not for tidiness.
#
# WHY --command AND curl RATHER THAN AN AGENT MESSAGE.
#
# A cron payload is one of: an agent turn, a shell command, or a system event.
# These five jobs are deterministic calls into the app -- no model belongs
# anywhere near them, and an agent-message payload would spend tokens to decide
# to do what this does directly. `--no-deliver` because there is no chat message
# to send: the app notifies Justin itself, out of the work the endpoint does.
#
# WHY THE SECRET IS A $VARIABLE AND NOT INTERPOLATED HERE.
#
# The payload is stored as argv in the gateway's cron store
# (`/home/node/.openclaw/state/openclaw.sqlite`) and echoed back by `cron get`,
# `cron list` and the run log. Writing the secret in would persist it in all
# four. `sh -lc` runs as a child of the gateway, so `$CLAIMS_INTERNAL_SECRET` and
# `$CLAIMS_APP_URL` resolve from the gateway's own environment at fire time --
# verified end to end 2026-08-04: the app logged
# `internal ingest starting correlation=int-53e1d403c38c` and answered `200 OK`,
# which is the guard passing, not merely the route existing. (An earlier probe
# against a not-yet-deployed route returned 404, which proves nothing about the
# secret -- FastAPI routes before it guards.)
#
# WHY --declaration-key.
#
# It is the product's own idempotency handle, so re-running this on every deploy
# updates the same five jobs instead of adding five more. That is also task 5.5's
# answer: the store is sqlite and survives a gateway restart, so nothing needs
# re-registering -- this script asserts the declaration rather than creating it.
set -e

oc() { node /app/openclaw.mjs "$@"; }

# Cadences. These MIRROR app/openclaw/config.py's defaults --
# VET_CLAIM_PIPELINE_INTERVAL_MINUTES=15, GMAIL_POLL_INTERVAL_MINUTES=5,
# ACTION_NUDGE_HOUR=9, VET_NUDGE_DAY=mon -- and `test_core.py` fails if they
# drift, because two copies of a schedule is exactly the sort of duplication that
# disagrees silently six months later.
#
# SYDNEY, not UTC, and this is a deliberate behaviour CHANGE (Justin, 2026-08-04).
# APScheduler ran these in the app container's local time, which is UTC, so
# "hour 9" meant 09:00 UTC -- 7pm or 8pm in Sydney depending on daylight saving.
# A morning nudge that arrives in the evening is the wrong nudge. Cron takes an
# IANA zone, so DST is handled by the gateway rather than by an offset that would
# drift twice a year.
TZ_NAME="${CLAIMS_CRON_TZ:-Australia/Sydney}"
TICK_EVERY="${CLAIMS_TICK_EVERY:-15m}"
INGEST_EVERY="${CLAIMS_INGEST_EVERY:-5m}"
NUDGE_CRON="${CLAIMS_NUDGE_CRON:-0 9 * * *}"
VET_NUDGE_CRON="${CLAIMS_VET_NUDGE_CRON:-0 9 * * 1}"
EXPIRE_CRON="${CLAIMS_EXPIRE_CRON:-0 9 * * *}"

if [ -z "$CLAIMS_APP_URL" ] || [ -z "$CLAIMS_INTERNAL_SECRET" ]; then
  echo "FAIL: CLAIMS_APP_URL or CLAIMS_INTERNAL_SECRET missing from the gateway's environment"
  exit 1
fi

# `--agent main` is not about the payload -- a command job never reaches an agent.
# Without it every `cron add` writes "No --agent specified; the job will run with
# the configured default agent" to stderr, PowerShell renders each one as a
# NativeCommandError block, and five of those in the deploy output is exactly the
# noise that hides a real failure. Naming the agent the gateway would have picked
# anyway silences it without changing behaviour.
#
# `-f` so an HTTP error is a non-zero exit and lands in the run log; `-sS` so the
# log holds the error text and not a progress meter. Without `-f`, curl exits 0
# on a 500 and every failed tick would read as a successful run -- the silent
# no-op the hard rules forbid, one layer out from the app.
post() {
  printf 'curl -fsS -X POST -H "X-OpenClaw-Secret: $CLAIMS_INTERNAL_SECRET" "$CLAIMS_APP_URL/internal/%s"' "$1"
}

add_every() {
  oc cron add --agent main --name "$1" --declaration-key "$2" --display-name "$3" \
    --every "$4" --command "$(post "$5")" \
    --no-deliver --timeout-seconds 600 >/dev/null
  echo "  declared $1 (every $4)"
}

add_cron() {
  oc cron add --agent main --name "$1" --declaration-key "$2" --display-name "$3" \
    --cron "$4" --tz "$TZ_NAME" --command "$(post "$5")" \
    --no-deliver --timeout-seconds 600 >/dev/null
  echo "  declared $1 (cron $4 $TZ_NAME)"
}

# 600s timeouts: a tick that matches claims makes LLM and Gmail calls, and the
# CLI default of 30s would kill a healthy run mid-flight. The app's own
# `run_exclusive` is what prevents an overlap if one ever does run long.
add_every claims-tick        claims.tick        "Claims pipeline tick"   "$TICK_EVERY"   tick
add_every claims-ingest      claims.ingest      "Gmail ingest"           "$INGEST_EVERY" ingest
add_cron  claims-nudge       claims.nudge       "Daily action nudge"     "$NUDGE_CRON"      nudge
add_cron  claims-vet-nudge   claims.vet-nudge   "Weekly vet chase"       "$VET_NUDGE_CRON"  vet-nudge
add_cron  claims-expire      claims.expire      "Message queue expiry"   "$EXPIRE_CRON"     expire-queue

echo "CRON OK"
