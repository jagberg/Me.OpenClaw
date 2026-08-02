#!/bin/sh
# The gateway's entire configuration, applied before it boots.
#
# Run by deploy.ps1 in a one-shot container with the state volume attached, the
# plugin source at /src and the agent workspace at /workspace.
#
# WHY EVERYTHING IS HERE, AND WHY IT IS A FILE.
#
# Config is a JSON file in a volume. Nothing about writing it needs the gateway
# running, so there is no reason to split it into a pre-boot half and a
# post-boot half -- and every reason not to. The gateway validates its whole
# config at startup and REFUSES to boot on a bad one; while it refuses,
# `config set` refuses too, because it validates first. One bad value written by
# a failed deploy bricks the volume, and the only way out is editing the JSON by
# hand. That happened: a stale `plugins.load.paths` took four restarts and
# tripped the restart-loop breaker. Applying it all before `up` means the
# gateway either starts configured or does not start, and the failure is here
# where it can be read.
#
# A file rather than a string built in PowerShell, because the same logic
# inlined was mangled three separate times on the way through PowerShell ->
# docker -> sh: a here-string arrived truncated with no error, a `node -e` had
# its quotes eaten, and a JSON array arrived as a bare string ("expected array,
# received string"). Three layers of quoting is not worth defending.
#
# It must also be LF-terminated. Git's autocrlf rewrote it once and `sh` read
# `set -e\r`, reporting "Illegal option -" -- an error naming the option and not
# the line ending. `.gitattributes` pins it.
set -e

PLUGIN_DIR=/home/node/.openclaw/plugins/claims
WORKSPACE=/home/node/.openclaw/workspace
oc() { node /app/openclaw.mjs "$@"; }

# --- the plugin -------------------------------------------------------------
# Copied, never bind mounted. A Windows bind mount lands as mode 777 and the
# gateway blocks a world-writable plugin directory -- correctly, since anything
# able to write there can run arbitrary code inside the gateway.
rm -rf "$PLUGIN_DIR"
mkdir -p "$PLUGIN_DIR"
cp /src/index.js /src/openclaw.plugin.json /src/package.json "$PLUGIN_DIR/"
chmod 755 "$PLUGIN_DIR"
chmod 644 "$PLUGIN_DIR"/*

# --- the agent workspace ------------------------------------------------------
# Injected into EVERY agent turn, so it is authored in the repo and copied in,
# never interviewed for. BOOTSTRAP.md is the file that drives the self-naming
# interview -- the stock agent asked Justin to name it, pick its species and
# choose its "vibe" across three messages before it would answer anything.
mkdir -p "$WORKSPACE"
cp /workspace/*.md "$WORKSPACE/" 2>/dev/null || true
rm -f "$WORKSPACE/BOOTSTRAP.md"
# A floor, not just the preflight's ceiling. `cp ... || true` above means a
# mistyped mount ships an EMPTY workspace, the seed exits 0 and the deploy
# passes -- while the spec scenario "a fresh workspace is deployed" asserts the
# shipped files are present. Without this the agent silently runs with no
# identity, no user context and no #id convention.
if [ "$(ls -1 "$WORKSPACE"/*.md 2>/dev/null | wc -l)" -lt 5 ]; then
  echo "FAIL: fewer than 5 workspace files in $WORKSPACE - is /workspace mounted?"
  exit 1
fi

# --- config -------------------------------------------------------------------
# A volume left invalid by an earlier failed deploy cannot be repaired with
# `config set`, so give doctor a chance first. `|| true` because a valid config
# makes this a no-op, which must not read as a failure.
oc config validate >/dev/null 2>&1 || oc doctor --fix || true

# Without this the gateway exits 78: "Missing config. Run `openclaw setup` or
# set gateway.mode=local".
oc config set gateway.mode local

# Both plugin gates, and both fail silently if missed. Set only now that the
# directory exists -- pointing at a missing path is what bricked the volume.
oc config set plugins.load.paths "[\"$PLUGIN_DIR\"]"
oc config set plugins.entries.claims.enabled true

# The tool surface. `claims__*` because MCP tools are namespaced
# <server>__<tool>; an allowlist written from the bare names resolves to nothing
# and the turn dies with "No callable tools remain". This is also the token
# budget -- the stock 32-tool surface was 31,972 chars of schema on every turn.
oc config set tools.allow '["claims__*"]'

# Thirteen skills, none relevant, 1,490 tokens per turn.
oc config set agents.defaults.skills '[]'

# Without this an upgrade re-seeds BOOTSTRAP.md and the interview returns.
oc config set agents.defaults.skipBootstrap true

# Access. The default is `pairing`, which hands an unrecognised sender their
# user id, a live pairing code and the exact command to ask the owner for
# approval -- on a bot discoverable by username.
if [ -n "$CLAIMS_TELEGRAM_CHAT_ID" ]; then
  oc config set channels.telegram.dmPolicy '"allowlist"'
  oc config set channels.telegram.allowFrom "[\"$CLAIMS_TELEGRAM_CHAT_ID\"]"
  oc config set channels.telegram.groupPolicy '"allowlist"'
  oc config set commands.ownerAllowFrom "[\"telegram:$CLAIMS_TELEGRAM_CHAT_ID\"]"
  oc config set channels.telegram.capabilities.inlineButtons '"all"'
  oc config set channels.telegram.richMessages true
else
  echo "WARN: CLAIMS_TELEGRAM_CHAT_ID unset - access policy not configured; the preflight will fail on it"
fi

# Groq as a custom OpenAI-compatible provider. OpenClaw bundles 38 providers and
# none of them is Groq, which is the one this project standardised on. `models`
# must be an ARRAY of objects with `id`; an object keyed by model id is rejected.
if [ -n "$GROQ_API_KEY" ]; then
  oc config set models.providers.groq "{\"baseUrl\":\"https://api.groq.com/openai/v1\",\"api\":\"openai-completions\",\"apiKey\":\"$GROQ_API_KEY\",\"models\":[{\"id\":\"llama-3.3-70b-versatile\",\"name\":\"Llama 3.3 70B\",\"input\":[\"text\"],\"contextWindow\":131072}]}"
  oc config set agents.defaults.model.primary '"groq/llama-3.3-70b-versatile"'
else
  echo "WARN: GROQ_API_KEY unset - the agent has no model; the preflight will fail on it"
fi

# The claims read surface. Service name, not host.docker.internal: the latter
# resolves through the host and NATs the source address to loopback.
oc config set mcp.servers.claims "{\"url\":\"http://app:8000/mcp\",\"transport\":\"streamable-http\",\"headers\":{\"X-OpenClaw-Secret\":\"$CLAIMS_INTERNAL_SECRET\"}}"

# The boundary plugins. 47 of 66 ship enabled, every boundary-relevant one among
# them, so this needs positive action on every deploy -- an upgrade re-enables
# them with no signal. The preflight then re-checks the RUNNING set, because
# Telegram auto-enables itself without writing config at all.
# Seven, matching gateway_preflight.BOUNDARY_PLUGINS and task 7.6's own list.
for p in browser file-transfer phone-control canvas device-pair memory-core talk-voice; do
  oc config set "plugins.entries.$p.enabled" false
done

# Authoritative: `config set` exits non-zero on warnings too, so this is the
# check that decides whether the gateway will start.
oc config validate
echo "SEED OK"
