#!/bin/sh
# Everything that must be true before the gateway will boot. Run by deploy.ps1
# inside a one-shot container, with the plugin source mounted at /src.
#
# THIS IS A FILE, not a string built in PowerShell, and that is the point. The
# same logic inlined as an argument was mangled twice on the way through
# PowerShell -> docker -> sh: first a here-string that arrived truncated with no
# error, then a `node -e` whose quotes were eaten and which died on
# "Unexpected end of input". Quoting through three layers is not worth
# defending. A file has no layers.
#
# Why any of this happens before `up`: the gateway validates its entire config
# at startup and refuses to boot on a bad one -- and while it is refusing,
# `config set` refuses too, because it validates first. One bad value written by
# a failed deploy bricks the volume. That happened: a stale `plugins.load.paths`
# took four restarts and tripped the restart-loop breaker.
set -e

PLUGIN_DIR=/home/node/.openclaw/plugins/claims

# The plugin is copied, never bind mounted. A Windows bind mount lands as mode
# 777 and the gateway blocks a world-writable plugin directory -- correctly:
# anything able to write there can run arbitrary code inside the gateway.
rm -rf "$PLUGIN_DIR"
mkdir -p "$PLUGIN_DIR"
cp /src/index.js /src/openclaw.plugin.json /src/package.json "$PLUGIN_DIR/"
chmod 755 "$PLUGIN_DIR"
chmod 644 "$PLUGIN_DIR"/*

# A volume left invalid by an earlier failed deploy cannot be repaired with
# `config set`, so give doctor a chance first. `|| true` because a *valid*
# config makes this a no-op and we do not want that to look like a failure.
node openclaw.mjs config validate >/dev/null 2>&1 || node openclaw.mjs doctor --fix || true

# Without this the gateway exits 78 on startup: "Missing config. Run `openclaw
# setup` or set gateway.mode=local".
node openclaw.mjs config set gateway.mode local

# Both plugin gates. Set only now that the directory above actually exists --
# pointing at a missing path is precisely what bricked the volume before.
node openclaw.mjs config set plugins.load.paths "[\"$PLUGIN_DIR\"]"
node openclaw.mjs config set plugins.entries.claims.enabled true

# Fail here rather than letting `up` discover it. `config set` exits non-zero on
# warnings too, so this is the authoritative check.
node openclaw.mjs config validate
echo "SEED OK"
