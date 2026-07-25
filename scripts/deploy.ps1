# Deploy OpenClaw, stamping the image with the commit it was built from.
#
# Every Telegram message logged to telegram_messages carries APP_VERSION, so a
# behaviour change can be attributed to a specific deploy. Building with a bare
# `docker compose up --build` leaves it "unknown" and the app warns at startup.
#
# Run from the worktree you deploy from (see root CLAUDE.md).

$ErrorActionPreference = "Stop"

$sha = (git rev-parse --short HEAD).Trim()
$branch = (git rev-parse --abbrev-ref HEAD).Trim()

# A dirty tree means the running image doesn't match any commit — mark it so the
# message log doesn't claim otherwise.
$dirty = ""
if ((git status --porcelain) -ne $null) { $dirty = "-dirty" }

$env:APP_VERSION = "$sha+$branch$dirty"
Write-Host "Deploying APP_VERSION=$($env:APP_VERSION)"

docker compose up -d --build
if ($LASTEXITCODE -ne 0) { throw "docker compose failed with exit code $LASTEXITCODE" }

Start-Sleep -Seconds 12
Write-Host "`n--- /health ---"
Invoke-RestMethod -Uri "http://localhost:8000/health" -TimeoutSec 20 | ConvertTo-Json
