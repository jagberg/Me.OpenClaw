# Deploy OpenClaw: two runtimes, two versions, one command.
#
# `app` owns the claims domain, the database and Gmail. `gateway` owns Telegram,
# agent sessions and cron. Both come up here, both report their health, and a
# partial start is a FAILURE rather than a success -- one runtime up and the
# other down is the state that looks fine and silently does half the job.
#
# Every Telegram message logged to telegram_messages carries APP_VERSION, so a
# behaviour change can be attributed to a specific deploy. Building with a bare
# `docker compose up --build` leaves it "unknown" and the app warns at startup.
#
# Run from the worktree you deploy from (see root CLAUDE.md).

param(
    # The preflight spends one real agent turn against a live provider budget.
    # Worth it on a real deploy; skip it when redeploying repeatedly.
    [switch]$SkipPreflight,
    [switch]$SkipTurnCheck
)

$ErrorActionPreference = "Stop"

# The gateway's secrets live in the ROOT .env, not app/.env -- see .env.example
# for why that separation is the isolation boundary rather than an annoyance.
# Compose interpolation reads this file; `env_file:` does not feed `${...}`.
if (-not (Test-Path ".env")) {
    throw "No root .env. Copy .env.example to .env and fill in the gateway's three values."
}

$sha = (git rev-parse --short HEAD).Trim()
$branch = (git rev-parse --abbrev-ref HEAD).Trim()

# A dirty tree means the running image doesn't match any commit -- mark it so the
# message log doesn't claim otherwise.
$dirty = ""
if ((git status --porcelain) -ne $null) { $dirty = "-dirty" }

$env:APP_VERSION = "$sha+$branch$dirty"

# --- everything that must be true BEFORE the gateway boots --------------------
#
# The gateway validates its whole config at startup and REFUSES to boot on a bad
# one -- and while it is refusing, `config set` refuses too, because it also
# validates first. So a single bad value written by a failed deploy bricks the
# volume and the only way out is editing the JSON by hand. That happened: a
# stale `plugins.load.paths` pointing at a mount that no longer existed took
# four restarts and tripped the restart-loop breaker.
#
# The rule that falls out: anything that can fail startup validation is applied
# HERE, in a one-shot container, before `up`. Everything else waits until the
# gateway is running, where a mistake is recoverable.
#
# The gateway is stopped first because the service holds a static IP on the
# compose network and `compose run` collides with it ("Address already in use").
# That also has to happen before the version probe, which is itself a
# `compose run` -- getting the order wrong reported the version as "unknown".
$ErrorActionPreference = "Continue"
docker compose stop gateway | Out-Null

# The image is pulled, not built, so its version is whatever the tag resolves
# to. Read it rather than assume it: `latest` moved from 2026.6.34 to 2026.7.1
# during this work, and an upgrade is exactly what re-enables a boundary plugin.
docker compose pull gateway | Out-Null
$version = docker compose run --rm --no-deps --entrypoint sh gateway -lc "node openclaw.mjs --version"
$env:GATEWAY_VERSION = ($version | Where-Object { $_ -match "\S" } | Select-Object -Last 1)
if (-not $env:GATEWAY_VERSION) { $env:GATEWAY_VERSION = "unknown" }

Write-Host "Deploying"
Write-Host "  APP_VERSION     = $($env:APP_VERSION)"
Write-Host "  GATEWAY_VERSION = $($env:GATEWAY_VERSION)"

# One shot, with the plugin source bound in read-only:
#   - gateway.mode=local, or it refuses to start at all ("Missing config")
#   - the plugin copied into the state volume and chmodded. NOT bind mounted:
#     a Windows bind mount is mode 777 and the gateway blocks a world-writable
#     plugin directory, which is right -- anything able to write there can run
#     code inside the gateway.
#   - plugins.load.paths written by editing the JSON directly, because
#     `config set` will not run while the config it is fixing is invalid.
Write-Host "  seeding pre-boot config and the plugin"
# .Replace(), not -replace: the latter takes a REGEX, and a lone backslash is not
# a valid one. It silently produced an empty string, and docker then reported
# "invalid spec: :/src:ro: empty section between colons" -- an error about the
# mount that was really an error about the path.
$pluginSrc = (Resolve-Path "./app/gateway-plugin").Path.Replace('\', '/')
# The seed is a FILE (scripts/gateway_seed.sh), mounted and run. Building it as
# a string here failed twice -- a here-string arrived truncated with no error,
# then a `node -e` had its quotes eaten and died on "Unexpected end of input".
# Quoting through PowerShell -> docker -> sh is not worth defending.
$workspaceSrc = (Resolve-Path "./app/gateway-workspace").Path.Replace('\', '/')
$scriptsSrc = (Resolve-Path "./scripts").Path.Replace('\', '/')
$seedOut = docker compose run --rm --no-deps `
    -v "${pluginSrc}:/src:ro" -v "${scriptsSrc}:/seed:ro" -v "${workspaceSrc}:/workspace:ro" `
    --entrypoint sh gateway /seed/gateway_seed.sh 2>&1 | Out-String
$seedExit = $LASTEXITCODE
$ErrorActionPreference = "Stop"
if ($seedExit -ne 0) { throw "pre-boot seed failed ($seedExit) -- the gateway would not have started:`n$($seedOut.Trim())" }

$ErrorActionPreference = "Continue"
docker compose up -d --build
$buildExit = $LASTEXITCODE
$ErrorActionPreference = "Stop"
if ($buildExit -ne 0) { throw "docker compose failed with exit code $buildExit" }

# --- health, per runtime, and a partial start is a failure --------------------
#
# POLL to a deadline; do NOT sleep once and probe once. A fixed 15s wait raced
# the gateway's own startup and reported `DEPLOY FAILED -- UNREACHABLE` on
# deploys that were fine, which cost more than the check is worth in two ways.
# It trained the reader to disbelieve the one message whose entire job is to
# catch a real partial start. And because the failure path THROWS, it also
# skipped the cron declarations below -- so a deploy that genuinely needed
# re-seeding would have skipped it silently while blaming something else.
#
# Retrying does not weaken the check: a runtime that is really down still fails,
# just at the deadline instead of at 15 seconds.
function Wait-ForProbe {
    param([scriptblock]$Probe, [int]$TimeoutSeconds = 90)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ($true) {
        try { return & $Probe }
        catch {
            if ((Get-Date) -ge $deadline) { throw }
            Start-Sleep -Seconds 5
        }
    }
}

$failures = @()

Write-Host "`n--- app /health ---"
try {
    $appHealth = Wait-ForProbe { Invoke-RestMethod -Uri "http://localhost:8000/health" -TimeoutSec 20 }
    $appHealth | ConvertTo-Json -Depth 4
} catch {
    $failures += "app: /health unreachable after 90s ($($_.Exception.Message))"
    Write-Host "UNREACHABLE"
}

Write-Host "`n--- gateway health ---"
try {
    # `ok` is inside the retry, not asserted after it. A gateway three seconds
    # into booting answers `ok: false` rather than refusing the connection, so
    # asserting it once is the same race as probing once -- it just fails with a
    # different sentence.
    $gwHealth = Wait-ForProbe {
        $ErrorActionPreference = "Continue"
        $raw = docker compose exec -T gateway node openclaw.mjs health --json
        $healthExit = $LASTEXITCODE
        $ErrorActionPreference = "Stop"
        if ($healthExit -ne 0) { throw "health exited $healthExit" }
        $h = $raw | ConvertFrom-Json
        if (-not $h.ok) { throw "health reports not ok" }
        $h
    }
    # `ok` alone is not enough. The Telegram channel can be configured, enabled
    # and not running, which is the dead-updater failure ADR-0015 was written
    # for -- it just moved runtimes.
    $tg = $gwHealth.channels.telegram
    Write-Host "  ok=$($gwHealth.ok)  plugins=$($gwHealth.plugins.loaded -join ',')"
    Write-Host "  telegram: running=$($tg.running) connected=$($tg.connected) lastError=$($tg.lastError)"
    if ($gwHealth.plugins.errors.Count -gt 0) { $failures += "gateway: plugin errors $($gwHealth.plugins.errors -join ',')" }
    # NOT a failure here. Before the cutover the gateway deliberately holds no
    # token and runs no channel, so asserting "running" would fail every slice-1
    # deploy. Which runtime should be polling is direction-dependent, so it
    # belongs in the preflight's check_exactly_one_poller -- which fails on BOTH
    # polling (409 Conflict) and on neither (every message silently dropped).
    # "not configured" is the CORRECT pre-cutover state, not a fault: the
    # gateway deliberately holds no token, so the channel has nothing to
    # configure itself from. Anything else is a real error.
    if ($tg -and $tg.lastError -and $tg.lastError -ne "not configured") {
        $failures += "gateway: telegram lastError = $($tg.lastError)"
    }
} catch {
    $failures += "gateway: health unreachable or not ok after 90s ($($_.Exception.Message))"
    Write-Host "UNREACHABLE"
}

if ($failures.Count -gt 0) {
    Write-Host "`nDEPLOY FAILED -- one runtime is down while the other is up:"
    $failures | ForEach-Object { Write-Host "  - $_" }
    throw "partial start"
}

# --- cron declarations: POST-boot, unlike the seed -----------------------------
#
# `cron.add` is a gateway RPC method, so this cannot go in the pre-boot seed --
# checked, not assumed: `config get cron` holds scheduler settings and no job
# definitions, and the plugin SDK has no cron surface. Idempotent via
# --declaration-key, so running it on every deploy re-asserts the same five jobs
# rather than adding five more.
#
# A failure here fails the deploy. A gateway with no cron entries is a gateway
# where nothing runs, and after the cutover that looks exactly like a quiet week
# (which is why /health reports `scheduler.overdue` too).
Write-Host "`n--- cron declarations ---"
$ErrorActionPreference = "Continue"
# `exec`, not `run`: the jobs are added through the RUNNING gateway's RPC. And
# `exec` takes no -v, which is why compose mounts ./scripts at /seed on the
# gateway service itself rather than this passing a mount in.
$cronOut = docker compose exec -T gateway sh /seed/gateway_cron.sh 2>&1 | Out-String
$cronExit = $LASTEXITCODE
$ErrorActionPreference = "Stop"
Write-Host $cronOut.Trim()
if ($cronExit -ne 0) { throw "cron declaration failed ($cronExit) -- nothing would be scheduled:`n$($cronOut.Trim())" }

# --- preflight: the config assertions no app-side test can make ---------------

if ($SkipPreflight) {
    Write-Host "`nPREFLIGHT SKIPPED -- the config assertions did not run. This is a gap, not a pass."
} else {
    Write-Host "`n--- gateway preflight ---"
    $preflightArgs = @("scripts/gateway_preflight.py", "--session-key", "preflight-$(Get-Date -UFormat %s)")
    if ($SkipTurnCheck) { $preflightArgs += "--skip-turn" }
    # The deploy worktree has no virtualenv -- .venv is gitignored and never
    # travels with a branch. The preflight is deliberately stdlib-only so any
    # python can run it.
    $py = if (Test-Path "./app/.venv/Scripts/python.exe") { "./app/.venv/Scripts/python.exe" } else { "python" }
    & $py @preflightArgs
    if ($LASTEXITCODE -ne 0) { throw "gateway preflight failed -- see the FAIL lines above" }
}

Write-Host "`nDeployed. app=$($env:APP_VERSION) gateway=$($env:GATEWAY_VERSION)"
