"""Assert the things about the gateway that no test in the app's suite can see.

A config check, not a test suite. Every assertion here corresponds to something
that was found **silently** wrong while spiking the gateway on 2026-08-01. All
of it is configuration rather than code, so `test_core.py` is structurally blind
to it, and every item fails without an error: an unset `dmPolicy` hands an
unknown sender a live pairing code; an upgrade can re-enable `browser` with no
signal; a `plugins list` reports `commands: []` for a command that works.

Run by `scripts/deploy.ps1`. A failure here fails the deploy, which is the whole
point — the alternative is discovering it from Justin's phone.

Two rules this file follows and future checks must keep:

1. **Assert behaviour, never a registry.** `plugins list` and `plugins inspect`
   read a persisted registry that goes stale silently. Invoke the thing.
2. **A check that cannot run is reported as SKIPPED, loudly.** A checklist that
   quietly omits an unverifiable item reads as full coverage (19c.1).
"""

import argparse
import json
import pathlib
import shlex
import subprocess
import sys
import urllib.request

# Groq free tier is 12,000 tokens per minute. The ceiling is set below it with
# room to spare: a turn that only just fits fails the moment an answer is long.
# Measured with the real claims inventory the turn is 4,934 (task 2.5), so this
# is roughly a 40% margin rather than a squeeze.
TURN_TOKEN_CEILING = 7000

# Itemised shares, asserted separately. A component regressing while the total
# stays under budget is exactly what a single total hides, and it is the failure
# this is meant to catch (17.9). Chars, as the platform reports them.
COMPONENT_CEILINGS = {
    "toolSchemaChars": 8000,       # measured 1,172 with the seven claims tools
    "workspaceFileChars": 8000,    # measured 6,508 for the six shipped files
    "skillChars": 100,             # skills are removed; anything here is a regression
}

# Must be off. Each grants filesystem, shell or browser reach, and 47 of 66
# plugins are enabled by default — every boundary-relevant one among them. The
# design assumed the opposite, so this needs positive action, not restraint.
BOUNDARY_PLUGINS = ("browser", "file-transfer", "phone-control", "canvas", "device-pair")

# The gateway writes its command menu into exactly these scopes. The app owns
# `chat`, which Telegram resolves first, and that only works while this list
# stays as it is. A future version widening it would overwrite the app's menu on
# every restart, and the menu would silently revert (13.1c).
EXPECTED_MENU_SCOPES = {"default", "all_group_chats"}

# Anything here in the gateway's environment or config is a boundary breach.
# The Gmail token and the database live on the app's side of the wall.
# Widened 2026-08-02 after the check PASSED on a container holding GOOGLE_API_KEY
# and GEMINI_API_KEY. 19b.5 says "no Google key with a Gmail scope", and a bare
# GOOGLE_API_KEY cannot be shown to lack one from outside -- the gateway's agent
# runs on Groq, so any Google credential here is both unnecessary and the same
# credential family as the Gmail token this boundary exists to keep out.
FORBIDDEN_GATEWAY_KEYS = ("GMAIL", "GOOGLE", "GEMINI", "DATABASE_PATH")


class Result:
    def __init__(self, name):
        self.name, self.status, self.detail = name, "PASS", ""

    def fail(self, detail):
        self.status, self.detail = "FAIL", detail
        return self

    def skip(self, detail):
        self.status, self.detail = "SKIP", detail
        return self

    def ok(self, detail=""):
        self.detail = detail
        return self


class Gateway:
    """Runs commands inside the gateway container.

    Two prefixes, deliberately separate. `run` invokes the openclaw CLI; `shell`
    runs a shell inside the same container. Folding them into one produced
    `openclaw sh -lc env`, which fails in a way that reads as "the check could
    not run" rather than "the check is wrong" — two SKIPs that looked like
    honest gaps.
    """

    def __init__(self, exec_prefix: str, shell_prefix: str):
        self.prefix = shlex.split(exec_prefix)
        self.shell_prefix = shlex.split(shell_prefix)

    def run(self, *args: str, timeout: int = 120) -> tuple[int, str, str]:
        proc = subprocess.run(self.prefix + list(args), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
        return proc.returncode, proc.stdout or "", proc.stderr or ""

    def shell(self, script: str, timeout: int = 60) -> tuple[int, str, str]:
        proc = subprocess.run(self.shell_prefix + [script], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
        return proc.returncode, proc.stdout or "", proc.stderr or ""

    def json(self, *args: str, timeout: int = 120):
        code, out, err = self.run(*args, timeout=timeout)
        if code != 0:
            raise RuntimeError(f"{' '.join(args)} exited {code}: {(err or out).strip()[:300]}")
        try:
            return json.loads(out)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{' '.join(args)} returned non-JSON: {out[:200]}") from exc

    def config(self, path: str) -> dict:
        """`config get` requires a dot path — there is no whole-config dump.

        Checked against the CLI's own help rather than assumed: a bare
        `config get --json` exits with *Missing required argument "path"*. Each
        caller asks for the subtree it needs, and a missing subtree comes back
        as `{}` rather than an error, which is the correct reading — an unset
        key is the dangerous case these checks exist for.
        """
        code, out, _ = self.run("config", "get", path, "--json")
        if code != 0 or not out.strip():
            return {}
        try:
            value = json.loads(out)
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    def health(self) -> dict:
        return self.json("health", "--json", timeout=60)


def check_turn_size(gw: Gateway, session_key: str) -> list[Result]:
    """19b.1 / 19b.2 — one real turn, measured by component.

    The fresh session key is not a detail. An existing session's accumulated
    history is counted in the request, which produced two false readings before
    it was caught: a minimal surface measured *higher* than the full one, and
    "disabling plugins made it worse" was nearly reported as a finding.
    """
    served, sized = Result("model serves a turn"), Result("turn size under ceiling")
    try:
        payload = gw.json("agent", "--agent", "main", "--session-key", session_key,
                          "--message", "hi", "--json", timeout=300)
    except Exception as exc:  # noqa: BLE001
        # A model id config ACCEPTS can still fail at runtime with
        # model_not_found — Groq did, before a custom provider entry existed.
        # "It validated" is not verification.
        return [served.fail(f"no turn completed: {exc}"), sized.skip("no turn to measure")]

    meta = (payload.get("result") or {}).get("meta") or {}
    report = meta.get("systemPromptReport") or {}
    tokens = (meta.get("agentMeta") or {}).get("promptTokens")
    served.ok(f"{(meta.get('agentMeta') or {}).get('model', '?')} answered")

    if not isinstance(tokens, int):
        return [served, sized.fail("the turn reported no promptTokens; cannot assert the ceiling")]

    failures = []
    if tokens > TURN_TOKEN_CEILING:
        failures.append(f"turn is {tokens} tokens, ceiling {TURN_TOKEN_CEILING}")

    measured = {
        "toolSchemaChars": (report.get("tools") or {}).get("schemaChars", 0),
        "workspaceFileChars": sum(f.get("injectedChars", 0)
                                  for f in report.get("injectedWorkspaceFiles") or []),
        "skillChars": (report.get("skills") or {}).get("chars", 0),
    }
    for key, ceiling in COMPONENT_CEILINGS.items():
        if measured[key] > ceiling:
            failures.append(f"{key} is {measured[key]}, ceiling {ceiling}")

    detail = f"{tokens} tokens; " + ", ".join(f"{k}={v}" for k, v in measured.items())
    return [served, sized.fail("; ".join(failures)) if failures else sized.ok(detail)]


def check_boundary_plugins(gw: Gateway) -> Result:
    """19b.3 — an upgrade that re-enables `browser` must fail the deploy.

    Two sources, both required. `plugins.entries` says what config asks for;
    `health.plugins.loaded` says what is actually running. The second is the one
    that matters — 47 of 66 plugins are enabled by default, Telegram
    *auto-enables* itself on detecting a token without writing config, so the
    enabled set is partly implicit and cannot be read from config alone (13.2).
    """
    result = Result("boundary plugins disabled")
    entries = (gw.config("plugins.entries") or {})
    problems = [f"{p} is not explicitly disabled in config"
                for p in BOUNDARY_PLUGINS
                if (entries.get(p) or {}).get("enabled") is not False]
    try:
        loaded = set((gw.health().get("plugins") or {}).get("loaded") or [])
    except Exception as exc:  # noqa: BLE001
        problems.append(f"could not read the running plugin set: {exc}")
    else:
        running = sorted(set(BOUNDARY_PLUGINS) & loaded)
        if running:
            problems.append(f"LOADED AND RUNNING: {running}")
    return result.fail("; ".join(problems)) if problems else result.ok()


def check_access_policy(cfg: dict) -> Result:
    """19b.4 — the default is the dangerous one.

    Left at `pairing`, the gateway answers an unrecognised sender with their
    user id, a live pairing code, and the exact command to ask the owner for
    approval. Telegram bots are discoverable by username. Justin saw this on
    first contact (15.1).
    """
    result = Result("access policy")
    telegram, commands = cfg["telegram"], cfg["commands"]
    problems = []
    if telegram.get("dmPolicy") != "allowlist":
        problems.append(f"dmPolicy is {telegram.get('dmPolicy')!r}, must be 'allowlist'")
    if not telegram.get("allowFrom"):
        problems.append("channels.telegram.allowFrom is empty")
    # Two different concepts, and the app has only one. DM pairing controls who
    # may talk to the bot; ownerAllowFrom controls who may run privileged
    # commands. Getting it wrong locks Justin out or lets a paired stranger in.
    if not commands.get("ownerAllowFrom"):
        problems.append("commands.ownerAllowFrom is empty")
    return result.fail("; ".join(problems)) if problems else result.ok()


def check_isolation(gw: Gateway) -> Result:
    """19b.5 — the gateway holds no Gmail credential and cannot reach the DB.

    Read from the container's real environment and mount table rather than from
    compose, because compose describes what was intended and this describes what
    is running.
    """
    result = Result("gmail-isolation-boundary")
    code, env, _ = gw.shell("env")
    if code != 0:
        return result.skip("could not read the gateway's environment")
    leaked = sorted({line.split("=", 1)[0] for line in env.splitlines()
                     for key in FORBIDDEN_GATEWAY_KEYS if line.upper().startswith(key)})
    problems = [f"forbidden env: {leaked}"] if leaked else []

    code, mounts, _ = gw.shell("cat /proc/mounts")
    if code != 0:
        problems.append("could not read the gateway's mount table")
    else:
        for line in mounts.splitlines():
            target = (line.split() + ["", ""])[1]
            if "/data" == target or target.startswith("/data/"):
                problems.append(f"a mount reaches the data dir: {target}")
    return result.fail("; ".join(problems)) if problems else result.ok()


def check_exactly_one_poller(gw: Gateway, app_health: dict | None) -> Result:
    """The single most dangerous configuration in this whole change.

    Telegram answers a second long-poller on the same token with `409 Conflict`,
    and the bot stops working. The app polls whenever `telegram_bot.start_polling`
    runs; the gateway starts polling the *instant* it detects a token, and
    announces it as `auto-enabled plugins for this runtime without writing
    config` — so simply putting the token in the gateway's environment is enough
    to break Telegram, with nothing in compose looking wrong.

    Both states are legitimate, at different times:
      - slice 1: the app polls, the gateway does not (no token, no channel)
      - after cutover: the gateway polls, the app's updater is off

    Neither is: two pollers fight, and zero means nobody is listening at all and
    every message is silently dropped. This check exists so the cutover is a
    verifiable step rather than a hope, in both directions.
    """
    result = Result("exactly one Telegram poller")
    if app_health is None:
        return result.skip("the app's /health was unreachable; the poller count is UNVERIFIED")

    app_polling = bool(app_health.get("polling_alive"))
    try:
        telegram = ((gw.health().get("channels") or {}).get("telegram") or {})
    except Exception as exc:  # noqa: BLE001
        return result.skip(f"could not read the gateway's channel state: {exc}")
    gateway_polling = bool(telegram.get("running"))

    if app_polling and gateway_polling:
        return result.fail(
            "BOTH runtimes are polling the bot token. Telegram answers the second poller with "
            "409 Conflict and the bot stops working. Clear TELEGRAM_BOT_TOKEN from the gateway, "
            "or disable the app's updater — not neither"
        )
    if not app_polling and not gateway_polling:
        return result.fail(
            "NEITHER runtime is polling. Nothing is listening to Telegram and every message is "
            "dropped in silence, which looks identical to a quiet day"
        )
    return result.ok("the gateway" if gateway_polling else "the app (pre-cutover)")


def check_media_roots(cfg: dict) -> Result:
    """19b.7 / 14.4 — `localRoots: "any"` disables the check outright.

    One word, and it is the whole control. Recorded here so nobody "fixes" a
    path error by reaching for it.
    """
    result = Result("media outbox narrow")
    roots = cfg.get("localRoots")
    if roots == "any":
        return result.fail("media.localRoots is 'any', which disables the allowlist entirely")
    if isinstance(roots, list) and any("/data" in str(r) for r in roots):
        return result.fail(f"a media root reaches the data dir: {roots}")
    return result.ok(f"localRoots={roots!r}" if roots else "default roots")


def check_menu_scopes(gw: Gateway) -> Result:
    """13.1c — the app owns Telegram's per-chat scope only while it stays free.

    The gateway writes `default` and `all_group_chats`, and Telegram resolves
    `chat` first. If a future version adds `chat` to its own list it would
    overwrite the app's menu on every restart, silently.
    """
    result = Result("gateway menu scopes unchanged")
    # The declaration spans several lines. A single-line grep found only
    # `default` and the check reported a scope change that had not happened —
    # a false FAIL, which erodes a preflight as fast as a false PASS.
    code, out, _ = gw.shell(
        "sed -n '/TELEGRAM_COMMAND_MENU_SCOPES/,/^];/p' "
        "/app/extensions/telegram/src/bot-native-command-menu.ts")
    if code != 0 or not out.strip():
        return result.skip("could not read the scope list from the shipped source")
    found = {s for s in ("default", "all_group_chats", "all_private_chats", "chat") if f'"{s}"' in out}
    if found != EXPECTED_MENU_SCOPES:
        return result.fail(f"the gateway now writes {sorted(found)}; the app's chat scope may be overwritten")
    return result.ok()


def check_button_commands(gw: Gateway, app_health: dict | None, commands: tuple) -> Result:
    """19b.6 — every command a button can emit must be registered.

    This one is weaker than the task asks for, and the gap is stated rather than
    papered over.

    What the task wants is each command *invoked*. There is no way to do that
    from the CLI: `openclaw command run` does not exist (checked — *"Unknown
    command: openclaw command"*), and the only real dispatch path is a Telegram
    tap, which a deploy script must not fake against Justin's chat.

    What is asserted instead: the plugin reports, at boot, the command list that
    `api.registerCommand` actually accepted, and the app records it. That is a
    runtime signal from inside the registration call, not a read of the
    persisted registry — which matters, because `plugins list` reported
    `commands: []` for commands that demonstrably worked (18.6), and a plugin
    can load without ever running (18.7). A plugin that never ran never reports,
    so the check fails rather than passing quietly.

    Why any of this is worth the trouble: an unregistered command in a button is
    not an error and not a no-op. It reaches the agent as a chat turn and spends
    tokens — measured live, three times, in Justin's chat (16.8). `/mark 7 sent`
    arriving at a model as free text is precisely what the design exists to
    prevent.
    """
    result = Result("button commands registered")
    if app_health is None:
        return result.skip("the app's /health was unreachable; registration is UNVERIFIED")
    reported = (app_health.get("gateway_plugin") or {})
    if not reported:
        return result.fail(
            "the plugin has not reported its commands - it may have loaded without running "
            "(both enablement gates are silent), so every button tap would reach the model"
        )
    registered = set(reported.get("commands") or [])
    missing = sorted(set(commands) - registered)
    if missing:
        return result.fail(f"not registered, so a tap on these becomes a model turn: {missing}")

    # The report proves the plugin RAN. It cannot prove the plugin OWNS the
    # names: `registerCommand` neither throws nor returns a failure on
    # collision, and the gateway logs it asynchronously about a second later.
    # Observed 2026-08-02 with a second plugin loaded — the boot report claimed
    # all five while three had actually been refused. Reading the log is the
    # only place that failure is visible.
    code, log, _ = gw.shell("cat /tmp/openclaw/openclaw-*.log 2>/dev/null | grep -i 'command registration failed' | tail -20")
    if code != 0:
        return result.skip("registered, but the gateway log was unreadable so collisions are UNVERIFIED")
    # The log file is JSON per line, so the command name arrives as \"mark\"
    # rather than "mark". Matching the unescaped form found nothing and the
    # check PASSED over three real collisions -- caught only by going and
    # reading the file rather than believing the green line. Unescape first.
    log = log.replace('\\"', '"')
    stolen = sorted({name for name in commands if f'"{name}"' in log})
    if stolen:
        return result.fail(
            f"registration was REFUSED for {stolen} - another plugin owns those names, and the "
            "plugin's own boot report cannot see it. Taps on them run someone else's handler"
        )
    return result.ok(f"{len(registered)} reported, no collisions in the gateway log")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exec-prefix", default="docker compose exec -T gateway node openclaw.mjs",
                        help="how to run the openclaw CLI inside the gateway container")
    parser.add_argument("--shell-prefix", default="docker compose exec -T gateway sh -lc",
                        help="how to run a shell inside the gateway container")
    parser.add_argument("--session-key", default="preflight",
                        help="MUST be unused; an existing session measures conversation, not surface")
    parser.add_argument("--skip-turn", action="store_true",
                        help="skip the agent turn (it spends tokens against a real provider budget)")
    parser.add_argument("--app-health", default="http://127.0.0.1:8000/health",
                        help="the Python app's health URL")
    args = parser.parse_args()

    gw = Gateway(args.exec_prefix, args.shell_prefix)
    results = []
    try:
        gw.run("--version", timeout=30)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL  gateway unreachable: {exc}")
        return 1

    app_health = None
    try:
        with urllib.request.urlopen(args.app_health, timeout=15) as response:
            app_health = json.loads(response.read())
    except Exception as exc:  # noqa: BLE001
        results.append(Result("app reachable").fail(f"{args.app_health}: {exc}"))
    else:
        results.append(Result("app reachable").ok(f"version {app_health.get('app_version')}"))

    results.append(check_exactly_one_poller(gw, app_health))
    results.append(check_boundary_plugins(gw))
    results.append(check_access_policy({"telegram": gw.config("channels.telegram"),
                                        "commands": gw.config("commands")}))
    results.append(check_media_roots(gw.config("media")))
    results.append(check_isolation(gw))
    results.append(check_menu_scopes(gw))

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "app"))
    # The leaf module, not gateway_client: this runs from a worktree with no
    # virtualenv, and anything reaching config or db would fail to import.
    from openclaw.button_commands import BUTTON_COMMANDS

    results.append(check_button_commands(gw, app_health, BUTTON_COMMANDS))

    if args.skip_turn:
        # Named rather than omitted. A skipped budget check must not read as a
        # passing one — that is the whole of 19c.1.
        results.append(Result("model serves a turn").skip("--skip-turn"))
        results.append(Result("turn size under ceiling").skip("--skip-turn"))
    else:
        results.extend(check_turn_size(gw, args.session_key))

    width = max(len(r.name) for r in results)
    for r in results:
        print(f"{r.status:<5} {r.name:<{width}}  {r.detail}")

    failed = [r for r in results if r.status == "FAIL"]
    skipped = [r for r in results if r.status == "SKIP"]
    if skipped:
        print(f"\n{len(skipped)} check(s) SKIPPED - these are gaps, not passes.")
    if failed:
        print(f"\nPREFLIGHT FAILED: {len(failed)} check(s).")
        return 1
    print("\nPreflight OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
