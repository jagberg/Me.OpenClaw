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

# Groq as a custom OpenAI-compatible provider. OpenClaw's bundled catalogue
# (`models list --all`) has 20 providers and none is Google; it DOES carry a
# `groq` id, which this block overrides with the account's own model list.
# `models` must be an ARRAY of objects with `id`; an object keyed by model id is
# rejected.
#
# Configured, but NOT the primary any more. As of 2026-08-04 Groq refuses this
# network: `api.groq.com` answers 403 `Access denied. Please check your network
# settings.` to a request carrying **no Authorization header**, and identically
# from inside both containers. Not the key, not the account, not a rate limit,
# and not fixable from here. It stays configured so that a network which can
# reach Groq gets it back by editing one line rather than rebuilding a provider.
if [ -n "$GROQ_API_KEY" ]; then
  oc config set models.providers.groq "{\"baseUrl\":\"https://api.groq.com/openai/v1\",\"api\":\"openai-completions\",\"apiKey\":\"$GROQ_API_KEY\",\"models\":[{\"id\":\"llama-3.3-70b-versatile\",\"name\":\"Llama 3.3 70B\",\"input\":[\"text\"],\"contextWindow\":131072}]}"
fi

# Gemini, the provider this network CAN reach, and therefore the primary.
# Google publishes an OpenAI-compatible surface at `/v1beta/openai`, so it is
# the same `openai-completions` shape as Groq rather than a new integration.
# Probed before being written, per the repo rule about validating against the
# product rather than reasoning about it: `POST /chat/completions` with a
# `claims__pending`-shaped tool returned `finish_reason: tool_calls` and the
# right tool name (2026-08-04).
#
# `max_tokens` matters here in a way it did not for Llama: 2.5-flash spends
# tokens on internal reasoning first, so a 16-token cap returned
# `finish_reason: length` with EMPTY content and a 200. A silent-looking empty
# reply is the failure mode to expect if a caller caps output tightly.
if [ -n "$GEMINI_API_KEY" ]; then
  # FOUR models, not one, and the reason is a live failure rather than caution.
  # 2026-08-04: a day of deploys and probes exhausted
  # `GenerateRequestsPerDayPerProjectPerModel-FreeTier` for gemini-2.5-flash, and
  # the deploy failed on `model serves a turn` with the gateway reporting only
  # "API rate limit reached". With one model declared there was nowhere to go --
  # the gateway cannot fail over to a model its provider entry never mentions.
  #
  # This is ADR-0017's walk, rebuilt on the gateway side. `llm.py` has had it for
  # the app since July and it is why invoice extraction kept working through the
  # same exhaustion. Same chain, same order, and every link was probed against a
  # `claims__*`-shaped tool before being written down (see llm._FALLBACK_MODELS).
  oc config set models.providers.gemini "{\"baseUrl\":\"https://generativelanguage.googleapis.com/v1beta/openai\",\"api\":\"openai-completions\",\"apiKey\":\"$GEMINI_API_KEY\",\"models\":[{\"id\":\"gemini-2.5-flash\",\"name\":\"Gemini 2.5 Flash\",\"input\":[\"text\"],\"contextWindow\":1048576},{\"id\":\"gemini-3.6-flash\",\"name\":\"Gemini 3.6 Flash\",\"input\":[\"text\"],\"contextWindow\":1048576},{\"id\":\"gemini-3.5-flash-lite\",\"name\":\"Gemini 3.5 Flash Lite\",\"input\":[\"text\"],\"contextWindow\":1048576},{\"id\":\"gemini-3.1-flash-lite\",\"name\":\"Gemini 3.1 Flash Lite\",\"input\":[\"text\"],\"contextWindow\":1048576}]}"
  oc config set agents.defaults.model.primary '"gemini/gemini-2.5-flash"'
  # The daily quota is PER MODEL, so moving models is the only cure for a spent
  # day -- waiting cannot help until the reset. The gateway classifies every quota
  # error into one `rate_limit` bucket and treats it as transient (ADR-0009's
  # accepted gap), so it will still waste retries on the exhausted model before
  # moving; a chain turns that from "the agent is dead until tomorrow" into "the
  # agent answers on a weaker model and says so".
  oc config set agents.defaults.model.fallbacks '["gemini/gemini-3.6-flash","gemini/gemini-3.5-flash-lite","gemini/gemini-3.1-flash-lite"]'
elif [ -n "$GROQ_API_KEY" ]; then
  oc config set agents.defaults.model.primary '"groq/llama-3.3-70b-versatile"'
  echo "WARN: GEMINI_API_KEY unset - falling back to Groq, which is network-blocked here; the preflight will fail on it"
else
  echo "WARN: no model provider key - the agent has no model; the preflight will fail on it"
fi

# --- the heartbeat, off ---------------------------------------------------------
# `0m` disables it (docs/gateway/heartbeat.md, "Defaults"); there is no `enabled`
# flag. Off rather than lengthened, because a heartbeat here has nothing to do:
# every scheduled thing this deployment runs is a gateway cron job hitting the
# app's internal endpoints, and none of those needs a model. HEARTBEAT.md says
# exactly that and the reply was always the literal `HEARTBEAT_OK`.
#
# The cost was not theoretical. A full agent turn every 30 minutes -- 48 a day --
# spent the whole Gemini free-tier daily quota, and the four-model fallback chain
# above is per-model insurance against exactly one model being exhausted, not
# against a schedule that exhausts them in order. Once spent, a TYPED message
# answers "API rate limit reached" while taps keep working, because taps are the
# plugin's deterministic path and never reach a model. So the heartbeat's only
# measurable effect was to break the one surface that needs the model.
#
# `0m` also drops HEARTBEAT.md from normal bootstrap context, which is why the
# file stays in the workspace rather than being deleted: it costs nothing now and
# it is the thing to edit if a real poll ever exists.
oc config set agents.defaults.heartbeat.every '"0m"'

# Acknowledgement reactions, which the gateway does natively and this project
# spent two deploys hand-rolling in the plugin instead.
#
# THE ACTUAL REASON THE THUMBS-UP NEVER APPEARED: the shipped default is
# `ackReactionScope: "group-mentions"`, and Justin's chat is a DM. So it was
# configured off for the only chat that exists here -- which is also why he never
# saw it work in the pre-gateway version. No hook was ever going to fix that.
#
# "all" rather than "direct" so a group ever added gets it too. The emoji is a
# JSON \u escape, not a literal: this file is read on a cp1252 console and a raw
# emoji in the seed's echoed output is mojibake at best.
oc config set messages.ackReactionScope '"all"'
oc config set messages.ackReaction '"\ud83d\udc4d"'
# Lifecycle reactions on the trigger message: queued -> thinking -> done/error.
# Telegram requires this explicitly true; unset is not enough (Discord is the
# only channel that infers it from ack reactions being active).
#
# statusReactions stays OFF, and turning it on was a mistake worth recording.
# It replaces the sticky ack with a LIFECYCLE emoji on the same message: queued
# -> thinking -> done, cleared at the end. `ackReactionPromise` becomes
# `statusReactionController.setQueued()` rather than a plain reaction
# (telegram-ingress-spool ~5566), so the 👍 stops being an acknowledgement that
# stays and becomes a progress indicator that vanishes -- which is exactly what
# Justin saw: slow to appear on a typed message, and gone again after /actions.
# It also carried a 700ms `timing.debounceMs` before the first emoji.
#
# A command has no agent lifecycle to display, so there is nothing for the
# controller to show anyway. Off means line 5566's other branch runs: one
# reaction, added once, left alone (`removeAckAfterReply` is false).
oc config set messages.statusReactions.enabled false

# --- logging that outlives the container ------------------------------------
# Task 13.5 / 9.x. The gateway's own log had two sinks and neither survived a
# deploy: stdout (`docker compose logs`, gone when the container is recreated)
# and `/tmp/openclaw/openclaw-<date>.log`, which is inside the container and
# also gone. So "an access denial leaves no trace" was partly a retention
# problem, not only a level problem -- and every deploy destroyed the evidence
# for the previous one.
#
# `/home/node/.openclaw` is the state VOLUME, so a file there persists across
# recreates with no new mount. Level pinned to info explicitly rather than left
# to the shipped default: the ingress drop lines are info
# (`dropping dm (not allowlisted)`, `skipping group message reason=not-allowed`),
# so a default that ever moves to warn would silently take them with it.
#
# NOT set: `logging.redactSensitive`. It takes "off" or "tools" -- not the
# boolean it reads like -- and the validator rejected `true` outright. Choosing
# between those two without knowing which the default is would be guessing at a
# control that decides whether tool payloads reach the log.
oc config set logging.level '"info"'
oc config set logging.file '"/home/node/.openclaw/logs/gateway.log"'

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
