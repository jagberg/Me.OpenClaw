# Apply the gateway configuration this app requires. Idempotent; run by deploy.ps1.
#
# WHY THIS EXISTS. Everything here was discovered by hand in a spike container
# whose state lived in a Docker volume -- invisible to git, unreviewable, and
# gone the moment the container was recreated. A gateway brought up fresh has
# none of it: no plugin loaded, no tool allowlist, no model provider, no access
# policy. It would start, look healthy, and do nothing this app needs.
#
# Config, not documentation. `scripts/gateway_preflight.py` then checks the
# result independently rather than restating what this just did.
$ErrorActionPreference = "Stop"

function Set-GatewayConfig($path, $value) {
    # `config set` exits 1 on a WARNING as well as on a failure, and prints
    # "Updated <path>" either way. Treating the exit code as authoritative
    # aborted a deploy over an advisory message while the setting had in fact
    # been written. Read what it says, not just how it exited.
    $ErrorActionPreference = "Continue"
    $out = docker compose exec -T gateway node openclaw.mjs config set $path $value 2>&1 | Out-String
    $code = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    if ($out -match "Updated") {
        if ($out -match "warning") { Write-Host "  note on ${path}: $(($out -split "`n" | Where-Object { $_ -match '^- ' }) -join '; ')" }
        return
    }
    throw "config set $path failed ($code): $($out.Trim())"
}

Write-Host "`n--- configuring the gateway ---"

# The plugin and its load path are applied BEFORE boot by deploy.ps1 -- a bad
# plugins.load.paths fails config validation and the gateway then refuses to
# start, which also makes `config set` refuse. See the pre-boot block there.

# The tool surface. `claims__*` because MCP tools are namespaced <server>__<tool>
# -- an allowlist written from the bare names resolves to nothing and the turn
# fails with "No callable tools remain". This is also the token budget: the
# stock 32-tool surface was 31,972 chars of schema on every single turn.
Set-GatewayConfig "tools.allow" '["claims__*"]'

# Skills are 13 files, none relevant, and 1,490 tokens per turn.
Set-GatewayConfig "agents.defaults.skills" "[]"

# Without this an upgrade re-seeds BOOTSTRAP.md, and the stock agent interviews
# the user about its own name and species before it will answer anything.
Set-GatewayConfig "agents.defaults.skipBootstrap" "true"

# Access. The default is `pairing`, which hands an unrecognised sender their
# user id, a live pairing code, and the exact command to ask the owner for
# approval -- on a bot that is discoverable by username.
if ($env:CLAIMS_TELEGRAM_CHAT_ID) {
    Set-GatewayConfig "channels.telegram.dmPolicy" '"allowlist"'
    Set-GatewayConfig "channels.telegram.allowFrom" "[`"$($env:CLAIMS_TELEGRAM_CHAT_ID)`"]"
    Set-GatewayConfig "channels.telegram.groupPolicy" '"allowlist"'
    Set-GatewayConfig "commands.ownerAllowFrom" "[`"telegram:$($env:CLAIMS_TELEGRAM_CHAT_ID)`"]"
    Set-GatewayConfig "channels.telegram.capabilities.inlineButtons" '"all"'
    Set-GatewayConfig "channels.telegram.richMessages" "true"
} else {
    Write-Host "  CLAIMS_TELEGRAM_CHAT_ID unset - access policy NOT configured. The preflight will fail on it."
}

# Groq as a custom OpenAI-compatible provider: OpenClaw bundles 38 providers and
# none of them is Groq, which is the one this project standardised on. `models`
# must be an array of objects with `id`; an object keyed by model id is rejected.
if ($env:GROQ_API_KEY) {
    $groq = @{
        baseUrl = "https://api.groq.com/openai/v1"
        api     = "openai-completions"
        apiKey  = $env:GROQ_API_KEY
        models  = @(@{ id = "llama-3.3-70b-versatile"; name = "Llama 3.3 70B"; input = @("text"); contextWindow = 131072 })
    } | ConvertTo-Json -Depth 6 -Compress
    Set-GatewayConfig "models.providers.groq" $groq
    Set-GatewayConfig "agents.defaults.model.primary" '"groq/llama-3.3-70b-versatile"'
} else {
    Write-Host "  GROQ_API_KEY unset - the agent has no model. The preflight will fail on it."
}

# The claims read surface. The app is reachable by service name over the compose
# network; host.docker.internal would NAT the source address to loopback.
$mcp = @{
    url       = "http://app:8000/mcp"
    transport = "streamable-http"
    headers   = @{ "X-OpenClaw-Secret" = $env:INTERNAL_API_SECRET }
} | ConvertTo-Json -Depth 5 -Compress
Set-GatewayConfig "mcp.servers.claims" $mcp

# The workspace markdown is injected into EVERY agent turn, so it is authored in
# the repo and copied in, never interviewed for. Copied rather than mounted
# because it lives inside the gateway's own state directory.
Write-Host "  copying the agent workspace"
$ErrorActionPreference = "Continue"
docker compose exec -T gateway sh -lc "mkdir -p /home/node/.openclaw/workspace" | Out-Null
Get-ChildItem "./app/gateway-workspace/*.md" | ForEach-Object {
    docker compose cp $_.FullName "gateway:/home/node/.openclaw/workspace/$($_.Name)" | Out-Null
}
# BOOTSTRAP.md is the file that drives the self-naming interview. Its own last
# instruction is "when you are done, delete this file"; we never let it start.
docker compose exec -T gateway sh -lc "rm -f /home/node/.openclaw/workspace/BOOTSTRAP.md" | Out-Null
$ErrorActionPreference = "Stop"

Write-Host "  restarting the gateway to apply"
$ErrorActionPreference = "Continue"
docker compose restart gateway | Out-Null
$ErrorActionPreference = "Stop"
Start-Sleep -Seconds 15
