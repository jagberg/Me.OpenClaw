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
#
# The 12k TPM that justified this number is GROQ's, and since 2026-08-04 the
# agent runs on Gemini, whose context is 1,048,576 tokens — the gateway reports
# `route: "fits"` with a 1,028,576-token prompt budget. So this is no longer a
# proxy for "the request will be rejected"; it is a cap on the surface the
# deployment chooses to ship on every turn, which is worth keeping for cost and
# for catching a regression. Kept at 7000 deliberately: the measured turn is
# 4,540 on Gemini, and moving the ceiling to match a roomier provider would
# discard the regression signal this exists for.
TURN_TOKEN_CEILING = 7000

# Itemised shares, asserted separately. A component regressing while the total
# stays under budget is exactly what a single total hides, and it is the failure
# this is meant to catch (17.9). Chars, as the platform reports them.
COMPONENT_CEILINGS = {
    # Measured twice on the same seven tools and it moved: 1,172 (task 2.5) and
    # 1,422 (the live run recorded at tasks.md:644). Unreconciled -- likely a
    # description edit between runs. The ceiling is set far above both on
    # purpose; treat either number as an order of magnitude, not a baseline.
    "toolSchemaChars": 8000,
    "workspaceFileChars": 8000,  # measured 6,508 for the six shipped files
    "skillChars": 100,  # skills are removed; anything here is a regression
}

# Must be off. Each grants filesystem, shell or browser reach, and 47 of 66
# plugins are enabled by default — every boundary-relevant one among them. The
# design assumed the opposite, so this needs positive action, not restraint.
# Widened 2026-08-02 by the eval: this listed five, while task 7.6 -- the task
# that produced it -- named SEVEN boundary-relevant plugins enabled by default.
# `memory-core` and `talk-voice` were outside the check that enforces
# `gmail-isolation-boundary`, so the guard was narrower than the finding that
# justified it. Both were running on the deployed gateway when this was found.
BOUNDARY_PLUGINS = (
    "browser",
    "file-transfer",
    "phone-control",
    "canvas",
    "device-pair",
    "memory-core",
    "talk-voice",
)

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

# ONE exception, added 2026-08-04, and narrowed to an exact name on purpose.
#
# The premise of the paragraph above -- "the agent runs on Groq, so any Google
# credential is unnecessary" -- stopped being true: Groq now refuses this network
# outright (403 to an unauthenticated request, see ADR-0009's amendment), and
# Gemini is the only provider the agent can reach. So the key IS necessary, and
# the honest move is to narrow the check rather than leave a rule that would fail
# every deploy.
#
# What was checked instead of assumed, because "it's only an API key" is exactly
# the kind of claim this repo has been wrong about: a bare Generative Language
# key is not the OAuth client + refresh token the Gmail path needs, and
# `gmail.googleapis.com/users/me/profile`, `.../users/me/messages` and
# `drive/v3/files` each answered **401** for this key. The boundary the check
# exists for -- the gateway cannot read Justin's mail -- still holds.
#
# EXACT names only, not a prefix: `GEMINI_API_KEY` is the credential the agent
# needs and nothing else Gemini-shaped has a reason to be here. A prefix
# exemption would silently readmit whatever Google names next.
ALLOWED_GATEWAY_KEYS = ("GEMINI_API_KEY",)


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
        proc = subprocess.run(
            self.prefix + list(args),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""

    def shell(self, script: str, timeout: int = 60) -> tuple[int, str, str]:
        proc = subprocess.run(
            self.shell_prefix + [script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
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
        payload = gw.json(
            "agent",
            "--agent",
            "main",
            "--session-key",
            session_key,
            "--message",
            "hi",
            "--json",
            timeout=300,
        )
    except Exception as exc:  # noqa: BLE001
        # A model id config ACCEPTS can still fail at runtime with
        # model_not_found — Groq did, before a custom provider entry existed.
        # "It validated" is not verification.
        # Name a daily exhaustion for what it is. "no turn completed" reads as
        # a broken deploy, and the operator's response is entirely different:
        # waiting until midnight UTC versus fixing config. The gateway itself
        # cannot make this distinction -- it has one `rate_limit` bucket and
        # treats it as transient (ADR-0017, task 11.5) -- so the preflight has
        # to, from the provider's own words.
        detail = str(exc)
        if "TPD" in detail or "tokens per day" in detail:
            return [
                served.fail(
                    "the model's DAILY token budget is exhausted, not a config fault. Groq's ceiling is "
                    "100k/day per model and it resets at midnight UTC. Retrying will not help; the agent "
                    "and the app's extraction calls share this key (task 17.6)"
                ),
                sized.skip("no turn to measure"),
            ]
        # Gemini says neither, and that ambiguity cost a wrong reading on
        # 2026-08-04: the deploy failed on `FailoverError: API rate limit
        # reached`, which is the gateway's single `rate_limit` bucket talking and
        # says nothing about WHICH limit. It was per-minute — the same key had
        # just served a probe turn — and a repeat 70 seconds later answered fine.
        #
        # So name the ambiguity instead of implying a diagnosis. The two responses
        # are opposite (retry now vs wait for reset), and the only way to tell them
        # apart is the provider's own quota detail: a per-day 429 carries a
        # `quotaId` containing `PerDay`, a per-minute one does not.
        if "rate limit" in detail.lower() or "resource_exhausted" in detail.lower():
            return [
                served.fail(
                    f"rate limited, and the gateway does not say which limit: {detail[:160]}. "
                    "Per-MINUTE clears in about a minute — re-run before treating this as a "
                    "failure. Per-DAY does not; check the 429's quotaId for `PerDay`"
                ),
                sized.skip("no turn to measure"),
            ]
        return [served.fail(f"no turn completed: {exc}"), sized.skip("no turn to measure")]

    meta = (payload.get("result") or {}).get("meta") or {}
    report = meta.get("systemPromptReport") or {}
    agent_meta = meta.get("agentMeta") or {}
    tokens = agent_meta.get("promptTokens")
    served.ok(f"{agent_meta.get('model', '?')} answered")

    # `promptTokens` is the provider's own count and is not always there. Groq
    # reported it; Gemini's OpenAI-compatible surface does not — `completion`
    # carries only a stop reason, and the 2026-08-04 provider switch turned this
    # check from PASS into the deploy's single FAIL for that reason alone.
    #
    # So fall back to the gateway's own `estimatedPromptTokens`, and SAY it is an
    # estimate in the detail rather than passing silently on a different quantity.
    # It is a pre-prompt estimate (`source: "pre-prompt-estimate"`), so it cannot
    # see the answer's own tokens — which is acceptable here because what this
    # asserts is the SURFACE the deployment chooses, not the reply's length.
    estimated = False
    if not isinstance(tokens, int):
        tokens = (agent_meta.get("contextBudgetStatus") or {}).get("estimatedPromptTokens")
        estimated = isinstance(tokens, int)
    if not isinstance(tokens, int):
        return [
            served,
            sized.fail(
                "the turn reported neither promptTokens nor estimatedPromptTokens; "
                "cannot assert the ceiling"
            ),
        ]

    failures = []
    if tokens > TURN_TOKEN_CEILING:
        failures.append(f"turn is {tokens} tokens, ceiling {TURN_TOKEN_CEILING}")

    measured = {
        "toolSchemaChars": (report.get("tools") or {}).get("schemaChars", 0),
        "workspaceFileChars": sum(
            f.get("injectedChars", 0) for f in report.get("injectedWorkspaceFiles") or []
        ),
        "skillChars": (report.get("skills") or {}).get("chars", 0),
    }
    for key, ceiling in COMPONENT_CEILINGS.items():
        if measured[key] > ceiling:
            failures.append(f"{key} is {measured[key]}, ceiling {ceiling}")

    detail = (
        f"{tokens} tokens{' (gateway estimate; the provider reported none)' if estimated else ''}; "
        + ", ".join(f"{k}={v}" for k, v in measured.items())
    )
    return [served, sized.fail("; ".join(failures)) if failures else sized.ok(detail)]


def check_cron_declared(gw: Gateway) -> Result:
    """Task 5.1/5.6 — the five schedules exist, are enabled, and point at this app.

    The failure this catches is the one the cutover creates: with the app's own
    scheduler off, a cron entry that was never declared or was left disabled
    produces the same silence as a week with no claims. `/health`'s
    `scheduler.overdue` catches it eventually; this catches it at deploy time,
    before Justin notices nothing has happened.

    Also asserts the payload is a COMMAND, not an agent turn. An agent-turn
    payload would still "work" -- and would spend model tokens deciding to do what
    a curl does, on every tick, forever.
    """
    result = Result("cron entries declared")
    expected = {"claims.tick", "claims.ingest", "claims.nudge", "claims.vet-nudge", "claims.expire"}
    try:
        payload = gw.json("cron", "list", "--json")
    except Exception as exc:  # noqa: BLE001
        return result.skip(f"could not read the cron list: {exc}")

    jobs = payload.get("jobs") if isinstance(payload, dict) else payload
    if not isinstance(jobs, list):
        return result.fail(f"cron list returned no job array: {str(payload)[:160]}")

    by_key = {j.get("declarationKey"): j for j in jobs if isinstance(j, dict)}
    problems = []
    for key in sorted(expected):
        job = by_key.get(key)
        if job is None:
            problems.append(f"{key} is not declared")
            continue
        if not job.get("enabled"):
            problems.append(f"{key} is disabled")
        kind = (job.get("payload") or {}).get("kind")
        if kind != "command":
            problems.append(f"{key} payload is {kind!r}, must be 'command'")
    if problems:
        return result.fail("; ".join(problems))
    return result.ok(f"{len(expected)} declared and enabled")


def check_boundary_plugins(gw: Gateway) -> Result:
    """19b.3 — an upgrade that re-enables `browser` must fail the deploy.

    Two sources, both required. `plugins.entries` says what config asks for;
    `health.plugins.loaded` says what is actually running. The second is the one
    that matters — 47 of 66 plugins are enabled by default, Telegram
    *auto-enables* itself on detecting a token without writing config, so the
    enabled set is partly implicit and cannot be read from config alone (13.2).
    """
    result = Result("boundary plugins disabled")
    entries = gw.config("plugins.entries") or {}
    problems = [
        f"{p} is not explicitly disabled in config"
        for p in BOUNDARY_PLUGINS
        if (entries.get(p) or {}).get("enabled") is not False
    ]
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
    leaked = sorted(
        {
            line.split("=", 1)[0]
            for line in env.splitlines()
            for key in FORBIDDEN_GATEWAY_KEYS
            if line.upper().startswith(key)
        }
        - set(ALLOWED_GATEWAY_KEYS)
    )
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
        telegram = (gw.health().get("channels") or {}).get("telegram") or {}
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


def check_app_can_send(app_container: str) -> Result:
    """The check whose absence cost a rollback on 2026-08-03.

    This script asserted eleven things about the deploy and never once asked
    whether the app could actually reach the gateway. `gateway_client` shells
    out to `openclaw`; every flag in it had been verified against `--help` on
    the *host*; nobody had run it from inside the app container, where there
    was no such binary. The first real tap after the cutover answered
    `gateway CLI not found at 'openclaw'`, and all outbound was broken.

    **`openclaw health`, not `message send --dry-run`.** The dry run was the
    obvious probe and it is worthless here: it answers `handledBy: "core"`
    without contacting the gateway at all, so it passes with a wrong URL, a
    wrong token, or an unpaired device — every one of the three failures this
    deploy actually hit. `health` opens the real connection.

    Not a real send: a preflight that messages Justin on every deploy trains
    him to ignore it.
    """
    result = Result("app can reach the gateway")
    # A **write-scoped** probe, and that detail is the whole check. Three
    # weaker probes were tried live on 2026-08-03 and each passed while a real
    # send failed:
    #   `message send --dry-run` answers `handledBy: "core"` and never contacts
    #     the gateway at all;
    #   `openclaw health` connects but needs only `operator.read`, so it passed
    #     against a device whose write scope was still pending approval;
    #   the gateway's own /health says nothing about this client.
    # Sending to target "0" asks for the write scope and is then rejected by
    # Telegram, which is the point: the connect, the pairing and the scope are
    # all exercised and no message is delivered. A preflight that messaged
    # Justin every deploy would train him to ignore it.
    probe = [
        "docker",
        "exec",
        app_container,
        "openclaw",
        "message",
        "send",
        "--channel",
        "telegram",
        "--target",
        "0",
        "--message",
        "preflight",
        "--json",
    ]
    try:
        proc = subprocess.run(probe, capture_output=True, text=True, timeout=90)
    except FileNotFoundError:
        return result.skip("docker not on PATH")
    except subprocess.TimeoutExpired:
        return result.fail("`openclaw health` did not return within 90s from the app container")
    blob = (proc.stdout + chr(10) + proc.stderr).strip()
    lowered = blob.lower()
    # Reaching the channel and being refused BY the channel is a pass: the
    # transport, the pairing and the write scope all worked, and target 0 is
    # not a chat. Checked before the return code, because this is a failure
    # exit for a successful probe.
    if "chat not found" in lowered or "chat_id is empty" in lowered or "invalid" in lowered:
        return result.ok(
            "the gateway accepted a write-scoped send and Telegram refused the dummy target"
        )
    if proc.returncode != 0:
        # The shapes seen live on 2026-08-03, named so the next person does not
        # have to re-derive any of them from an exit code.
        if "not found at" in lowered or "command not found" in lowered:
            return result.fail(f"the app has no gateway CLI: {blob[:180]}")
        if "more scopes than currently approved" in lowered or "scope upgrade" in lowered:
            return result.fail(
                "the app's device is paired but not approved for sending — "
                f"`openclaw devices approve <id>` on the gateway: {blob[:140]}"
            )
        if "pairing" in lowered:
            return result.fail(
                "the app's device is not paired — "
                f"`openclaw devices approve <id>` on the gateway: {blob[:140]}"
            )
        if "non-loopback" in lowered:
            return result.fail(
                f"OPENCLAW_GATEWAY_URL must be a literal address, not a DNS name: {blob[:140]}"
            )
        return result.fail(f"the write-scoped probe exited {proc.returncode}: {blob[:180]}")
    return result.ok("write-scoped send accepted by the gateway")


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


def check_inbound_document_path(gw: Gateway) -> Result:
    """csv-upload-via-telegram task 7.1 — `check_button_commands`' principle:
    a silently broken path must not look like a week with no uploads.

    Weaker than the real thing, and the gap is stated rather than papered
    over, exactly as `check_button_commands` states its own: there is no way
    to fake a real Telegram document send from a deploy script, so this
    checks the ONE structural prerequisite that has actually broken here.
    `media/inbound` sits under a mount that was read-only until
    csv-upload-via-telegram, and Docker recreates its parent as root:root
    every time the outbox mount's own directory is auto-vivified (task 1.6) —
    so both the mount mode and the ownership are checked, not just one.
    """
    result = Result("inbound document path writable")
    code, out, err = gw.shell(
        "mkdir -p /home/node/.openclaw/media/inbound "
        "&& touch /home/node/.openclaw/media/inbound/.preflight-write-test "
        "&& rm /home/node/.openclaw/media/inbound/.preflight-write-test "
        "&& echo WRITABLE"
    )
    if code != 0 or "WRITABLE" not in out:
        return result.fail(
            f"media/inbound is not writable by the gateway's own user -- an inbound "
            f"Telegram document will stage silently and never reach the app: {(err or out).strip()[:200]}"
        )
    return result.ok()


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
        "/app/extensions/telegram/src/bot-native-command-menu.ts"
    )
    if code != 0 or not out.strip():
        return result.skip("could not read the scope list from the shipped source")
    found = {
        s for s in ("default", "all_group_chats", "all_private_chats", "chat") if f'"{s}"' in out
    }
    if found != EXPECTED_MENU_SCOPES:
        return result.fail(
            f"the gateway now writes {sorted(found)}; the app's chat scope may be overwritten"
        )
    return result.ok()


def check_button_commands(gw: Gateway, app_health: dict | None, commands: tuple) -> Result:
    """19b.6 — every command a button can emit must be registered.

    This one is weaker than the task asks for, and the gap is stated rather than
    papered over.

    What the task wants is each command *invoked*. There is no way to do that
    from the CLI: `openclaw command run` does not exist (checked — *"Unknown
    command: openclaw command"*), and the only real dispatch path is a Telegram
    tap, which a deploy script must not fake against Justin's chat.

    What is asserted instead: the plugin reports, at boot, the command list it
    ATTEMPTED to register, and the app records it. It cannot report what
    `registerCommand` accepted -- that call returns nothing and surfaces a
    collision asynchronously -- so this proves the plugin loaded and RAN, not
    that it owns the names. Ownership is covered below by reading the gateway's
    log. A runtime signal, not a read of the persisted registry — which matters, because `plugins list` reported
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
    reported = app_health.get("gateway_plugin") or {}
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
    code, log, _ = gw.shell(
        "cat /tmp/openclaw/openclaw-*.log 2>/dev/null | grep -i 'command registration failed' | tail -20"
    )
    if code != 0:
        return result.skip(
            "registered, but the gateway log was unreadable so collisions are UNVERIFIED"
        )
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
    parser.add_argument(
        "--exec-prefix",
        default="docker compose exec -T gateway node openclaw.mjs",
        help="how to run the openclaw CLI inside the gateway container",
    )
    parser.add_argument(
        "--shell-prefix",
        default="docker compose exec -T gateway sh -lc",
        help="how to run a shell inside the gateway container",
    )
    parser.add_argument(
        "--session-key",
        default="preflight",
        help="MUST be unused; an existing session measures conversation, not surface",
    )
    parser.add_argument(
        "--skip-turn",
        action="store_true",
        help="skip the agent turn (it spends tokens against a real provider budget)",
    )
    parser.add_argument(
        "--app-container",
        default="meopenclaw-telegram-claimquery-app-1",
        help="container the app runs in; the send probe execs the gateway CLI there",
    )
    parser.add_argument(
        "--app-health", default="http://127.0.0.1:8000/health", help="the Python app's health URL"
    )
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
    results.append(check_cron_declared(gw))
    results.append(check_boundary_plugins(gw))
    results.append(
        check_access_policy(
            {"telegram": gw.config("channels.telegram"), "commands": gw.config("commands")}
        )
    )
    results.append(check_media_roots(gw.config("media")))
    results.append(check_inbound_document_path(gw))
    results.append(check_app_can_send(args.app_container))
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

    # The console is cp1252. A detail string carrying anything outside it --
    # Groq's rate-limit error contains a warning emoji -- raises
    # UnicodeEncodeError *while printing a FAIL*, so the script dies at the
    # exact moment it is trying to tell you something went wrong. Worse, piping
    # the run through anything reports the PIPE's exit code, so the crash read
    # as a clean pass. Sanitise on the way out.
    def ascii_only(text: str) -> str:
        return (text or "").encode("ascii", "replace").decode("ascii")

    width = max(len(r.name) for r in results)
    for r in results:
        print(f"{r.status:<5} {r.name:<{width}}  {ascii_only(r.detail)}")

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
