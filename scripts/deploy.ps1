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
$pluginSrc = (Resolve-Path "./app/gateway-plugin").Path -replace '\', '/'
docker compose run --rm --no-deps -v "${pluginSrc}:/src:ro" --entrypoint sh gateway -lc @'
set -e
mkdir -p /home/node/.openclaw/plugins/claims
cp /src/index.js /src/openclaw.plugin.json /src/package.json /home/node/.openclaw/plugins/claims/
chmod 755 /home/node/.openclaw/plugins/claims
chmod 644 /home/node/.openclaw/plugins/claims/*
node -e '
  const fs = require("fs"), f = "/home/node/.openclaw/openclaw.json";
  const c = fs.existsSync(f) ? JSON.parse(fs.readFileSync(f, "utf8")) : {};
  c.gateway = Object.assign({}, c.gateway, { mode: "local" });
  c.plugins = c.plugins || {};
  c.plugins.load = Object.assign({}, c.plugins.load, { paths: ["/home/node/.openclaw/plugins/claims"] });
  c.plugins.entries = Object.assign({}, c.plugins.entries, { claims: { enabled: true } });
  fs.writeFileSync(f, JSON.stringify(c, null, 2));
'
node openclaw.mjs config validate
'@ | Out-Null
$seedExit = $LASTEXITCODE
$ErrorActionPreference = "Stop"
if ($seedExit -ne 0) { throw "pre-boot seed failed ($seedExit) -- the gateway would not have started" }

$ErrorActionPreference = "Continue"
docker compose up -d --build
$buildExit = $LASTEXITCODE
$ErrorActionPreference = "Stop"
if ($buildExit -ne 0) { throw "docker compose failed with exit code $buildExit" }

Start-Sleep -Seconds 15

# --- health, per runtime, and a partial start is a failure --------------------

$failures = @()

Write-Host "`n--- app /health ---"
try {
    $appHealth = Invoke-RestMethod -Uri "http://localhost:8000/health" -TimeoutSec 20
    $appHealth | ConvertTo-Json -Depth 4
} catch {
    $failures += "app: /health unreachable ($($_.Exception.Message))"
    Write-Host "UNREACHABLE"
}

Write-Host "`n--- gateway health ---"
try {
    $ErrorActionPreference = "Continue"
    $raw = docker compose exec -T gateway node openclaw.mjs health --json
    $healthExit = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    if ($healthExit -ne 0) { throw "health exited $healthExit" }
    $gwHealth = $raw | ConvertFrom-Json
    # `ok` alone is not enough. The Telegram channel can be configured, enabled
    # and not running, which is the dead-updater failure ADR-0015 was written
    # for -- it just moved runtimes.
    $tg = $gwHealth.channels.telegram
    Write-Host "  ok=$($gwHealth.ok)  plugins=$($gwHealth.plugins.loaded -join ',')"
    Write-Host "  telegram: running=$($tg.running) connected=$($tg.connected) lastError=$($tg.lastError)"
    if (-not $gwHealth.ok) { $failures += "gateway: health reports not ok" }
    if ($gwHealth.plugins.errors.Count -gt 0) { $failures += "gateway: plugin errors $($gwHealth.plugins.errors -join ',')" }
    # NOT a failure here. Before the cutover the gateway deliberately holds no
    # token and runs no channel, so asserting "running" would fail every slice-1
    # deploy. Which runtime should be polling is direction-dependent, so it
    # belongs in the preflight's check_exactly_one_poller -- which fails on BOTH
    # polling (409 Conflict) and on neither (every message silently dropped).
    if ($tg -and $tg.lastError) { $failures += "gateway: telegram lastError = $($tg.lastError)" }
} catch {
    $failures += "gateway: health unreachable ($($_.Exception.Message))"
    Write-Host "UNREACHABLE"
}

if ($failures.Count -gt 0) {
    Write-Host "`nDEPLOY FAILED -- one runtime is down while the other is up:"
    $failures | ForEach-Object { Write-Host "  - $_" }
    throw "partial start"
}

# --- apply the config the preflight then verifies -----------------------------
#
# 7.6: the boundary plugins are DISABLED here rather than assumed off. 47 of 66
# ship enabled, every boundary-relevant one among them, so this needs positive
# action on every deploy -- an upgrade re-enables them silently. Applying and
# then asserting is deliberate: the preflight stays an independent check rather
# than a restatement of what this block just did.
$boundaryPlugins = @("browser", "file-transfer", "phone-control", "canvas", "device-pair")
$changed = $false
foreach ($plugin in $boundaryPlugins) {
    $ErrorActionPreference = "Continue"
    $current = docker compose exec -T gateway node openclaw.mjs config get "plugins.entries.$plugin.enabled"
    $ErrorActionPreference = "Stop"
    if ($current -notmatch "false") {
        docker compose exec -T gateway node openclaw.mjs config set "plugins.entries.$plugin.enabled" false | Out-Null
        Write-Host "  disabled boundary plugin: $plugin"
        $changed = $true
    }
}
if ($changed) {
    # Plugin enablement is read at startup, so a set without a restart leaves
    # the plugin running while config claims otherwise -- which is exactly the
    # divergence the preflight reads the RUNNING set to catch.
    Write-Host "  restarting the gateway to apply plugin changes"
    docker compose restart gateway | Out-Null
    Start-Sleep -Seconds 15
}

# --- apply the gateway's own configuration ------------------------------------
& ./scripts/gateway_configure.ps1

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
