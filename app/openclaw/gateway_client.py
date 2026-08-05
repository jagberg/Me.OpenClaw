"""The one seam for outbound messages once the gateway owns the bot token.

Today every send goes through `telegram_bot.LoggedBot` so nothing can reach
Telegram without landing in `telegram_messages`. That property is the whole
reason the message log is trustworthy, and it must survive the transport
change — so this module is the LoggedBot of the gateway era: one place that
builds the CLI invocation, one place that logs, one place that fails loudly.

Anything that shells out to the gateway directly bypasses the message log, in
exactly the way a bare `telegram.Bot` does today.

**Every flag here was verified against `openclaw message <sub> --help` on
2026-08-01** (gateway 2026.6.34), and the button payload against the platform's
own `normalizeMessagePresentation`. That matters because the first version of
this module guessed all of them and got all of them wrong: `--chat`, `--text`,
`--file`, `--caption` and `--buttons` do not exist. The platform discards a
malformed presentation *silently* and still returns `ok: true` with a real
message id, so nothing about a wrong payload announces itself.
"""

import json
import logging
import subprocess

from . import config, message_log, trace

logger = logging.getLogger(__name__)

# Telegram's callback_data ceiling is 64 bytes; the gateway prefixes a command
# button's data with "tgcmd:" (6 bytes), leaving 58 for the command string
# itself. Overflow is NOT an error: sanitizeTelegramCallbackData returns
# undefined, the button is filtered out of its row, an empty row is dropped, and
# a message whose only row was dropped arrives with no keyboard at all. Measured
# live at the boundary — 58 renders, 59 vanishes, both reporting success.
COMMAND_CALLBACK_BUDGET_BYTES = 58

# Re-exported so existing callers keep working. The declaration lives in
# `button_commands.py`, which imports nothing, because the deploy-time
# preflight reads it from a worktree with no virtualenv and importing this
# module would drag in config (dotenv) and db.
# The E402 suppression below carries that paragraph's weight: the placement is
# deliberate, so the linter is told rather than the import moved.
from .button_commands import BUTTON_COMMANDS  # noqa: E402, F401


class GatewaySendError(RuntimeError):
    """A send that did not happen. Raised, never swallowed."""


class PresentationError(ValueError):
    """A payload the platform would have discarded without telling us.

    Every condition raised here was checked against the shipped normalizer
    rather than inferred. They are raised because the alternative is a message
    that arrives looking deliberate with its buttons missing.
    """


def build_buttons(buttons: list[dict]) -> dict:
    """Turn `[{"label": ..., "command": ...}]` into a valid presentation.

    ponytail: callers never hand-write presentation JSON. Five sends were
    silently discarded for putting `buttons` at the top level instead of inside
    `blocks`, and the normalizer returns `undefined` for that shape — so the
    nesting lives in exactly one place, with the failures that produce
    `undefined` turned into exceptions here.
    """
    controls = []
    for button in buttons:
        label = (button.get("label") or "").strip()
        command = (button.get("command") or "").strip()
        if not label:
            # Verified: a single label-less button makes the normalizer return
            # undefined for the WHOLE presentation, so one bad button costs
            # every button on the message.
            raise PresentationError(f"button has no label: {button!r}")
        if not command.startswith("/"):
            raise PresentationError(f"button command must be a slash command, got {command!r}")
        verb = command[1:].split(" ", 1)[0]
        if verb not in BUTTON_COMMANDS:
            # `button_commands.py` says card-building code must draw from that
            # tuple; until now nothing made it so. An undeclared verb is not an
            # error at the gateway — it reaches the agent as a chat turn and
            # spends tokens (measured live 2026-08-01), and the preflight only
            # asserts the *declared* names are registered, so a button emitting
            # anything else ships unasserted.
            raise PresentationError(
                f"button command {command!r} is not in BUTTON_COMMANDS {BUTTON_COMMANDS} — "
                "the plugin registers only those, so this tap would reach the model"
            )
        size = len(command.encode("utf-8"))
        if size > COMMAND_CALLBACK_BUDGET_BYTES:
            # Bytes, not characters: a non-ASCII pet or condition name costs
            # more than one byte each and would slip past a len() check.
            raise PresentationError(
                f"button command is {size} UTF-8 bytes, over the "
                f"{COMMAND_CALLBACK_BUDGET_BYTES}-byte budget: {command!r}"
            )
        controls.append({"label": label, "action": {"type": "command", "command": command}})
    return {"blocks": [{"type": "buttons", "buttons": controls}]}


def _argv(action: str, target: str, args: list[str]) -> list[str]:
    """Build the gateway CLI invocation.

    ponytail: every command shape lives in this one function on purpose, which
    is what made correcting the guessed flag names a single edit.
    """
    return [
        config.OPENCLAW_CLI,
        "message",
        action,
        "--channel",
        config.OPENCLAW_CHANNEL,
        "--target",
        str(target),
        *args,
        "--json",
    ]


def _run(
    action: str,
    target: str,
    args: list[str],
    *,
    kind: str,
    summary: str,
    payload: dict,
    correlation: str | None = None,
    runner=None,
) -> dict:
    argv = _argv(action, target, args)
    # Never log argv wholesale — a caption can carry claim detail and the CLI
    # may grow a token flag. Log the action, not the payload.
    logger.info("gateway %s target=%s correlation=%s", action, target, correlation)
    run = runner or subprocess.run
    try:
        # The one step worth timing above all others: 6.6s of this is the CLI
        # initialising itself with no network involved, ~2.5s is connect + auth,
        # and under a second is the gateway's actual work. See `trace`.
        with trace.step(f"cli.{action}", correlation):
            completed = run(
                argv, capture_output=True, text=True, timeout=config.OPENCLAW_CLI_TIMEOUT_SECONDS
            )
    except FileNotFoundError as exc:
        raise GatewaySendError(f"gateway CLI not found at {config.OPENCLAW_CLI!r}") from exc
    except subprocess.TimeoutExpired as exc:
        raise GatewaySendError(
            f"gateway {action} timed out after {config.OPENCLAW_CLI_TIMEOUT_SECONDS}s"
        ) from exc

    if completed.returncode != 0:
        # The exit code alone is useless for diagnosis, so stderr goes into the
        # reason. This is the human-readable failure the project's rules require:
        # a send that did not happen must never look like one that did.
        stderr = (completed.stderr or "").strip()[:500]
        raise GatewaySendError(
            f"gateway {action} exit {completed.returncode}: {stderr or 'no stderr'}"
        )

    # Outbound logging stays on this path, so the gateway era keeps the same
    # audit trail and RL dataset the LoggedBot era had.
    with trace.step("log.outbound", correlation, kind=kind):
        message_log.record_outbound(
            kind, summary, {**payload, "correlation_id": correlation}, correlation=correlation
        )

    try:
        return json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        # A non-JSON success is not a failure of the send — say so and move on
        # rather than raising and letting a caller retry an already-sent message.
        logger.warning("gateway %s returned non-JSON output correlation=%s", action, correlation)
        return {}


def using_http_route() -> bool:
    """Whether the fast in-gateway path is configured. Both halves or neither —
    a URL with no token would 401 every send and read as an outage."""
    return bool(config.OPENCLAW_GATEWAY_HTTP_URL and config.OPENCLAW_GATEWAY_TOKEN)


def send_cards(target: str, cards: list[dict], correlation: str | None = None, poster=None) -> None:
    """Send N cards in ONE call to the plugin's in-gateway route.

    This is the fast path and the reason it exists is measured, not assumed: the
    CLI costs 9–13s per message of which ~6.6s is its own initialisation with no
    network contact at all, so N messages cost N × 9s no matter how they are
    scheduled. Here one local HTTP call dispatches `message.action` inside the
    gateway N times, and the only per-card cost is the gateway's own (0.3–1.1s,
    mostly Telegram's rate pacing).

    **Order is preserved**, which the CLI burst could not offer: the plugin
    dispatches sequentially, so `/actions` gets its summary card first again.

    Cards are the same dicts the command layer already builds — `text`/`buttons`
    or `png`/`caption`/`buttons`. Raises `GatewaySendError` if any card failed,
    naming the first reason, because a partial send must never read as a whole
    one.
    """
    import json as _json
    import urllib.error
    import urllib.request

    from . import media_outbox

    payload = {"channel": config.OPENCLAW_CHANNEL, "cards": []}
    for card in cards:
        entry: dict = {"target": str(target)}
        if card.get("png") is not None:
            # A path through the shared outbox, NOT base64. `SendParamsSchema`
            # does take a base64 `buffer`, and it was the first thing tried —
            # the gateway materialises it under `<stateDir>/media/outbound`,
            # which compose mounts READ-ONLY (14.2, deliberately), so every
            # image failed with `ENOENT: mkdir '/home/node/.openclaw/media/
            # outbound'`. Publishing to the outbox keeps the narrow mount and
            # costs 9ms.
            with trace.step("outbox.publish", correlation, bytes=len(card["png"])):
                entry["media_url"] = media_outbox.publish(card["png"], ".png", "card")
            if card.get("caption"):
                entry["message"] = card["caption"]
        else:
            entry["message"] = card.get("text") or ""
        if card.get("buttons"):
            # Same builder as the CLI path. A presentation the platform rejects
            # is discarded silently with `ok: true`, so it is never hand-written.
            entry["presentation"] = build_buttons(card["buttons"])
        payload["cards"].append(entry)

    url = config.OPENCLAW_GATEWAY_HTTP_URL.rstrip("/") + "/api/v1/claims/send"
    request = urllib.request.Request(
        url,
        data=_json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.OPENCLAW_GATEWAY_TOKEN}",
        },
    )
    logger.info("gateway http send cards=%s correlation=%s", len(cards), correlation)
    try:
        with trace.step("http.send_cards", correlation, cards=len(cards)):
            opener = poster or urllib.request.urlopen
            with opener(request, timeout=config.OPENCLAW_HTTP_TIMEOUT_SECONDS) as response:
                answer = _json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        body = (exc.read() or b"").decode(errors="replace")[:500]
        raise GatewaySendError(f"gateway send route returned {exc.code}: {body}") from exc
    except Exception as exc:  # noqa: BLE001 — URLError, timeouts, bad JSON
        raise GatewaySendError(f"gateway send route unreachable: {exc}") from exc

    sent = int(answer.get("sent") or 0)
    # Log what actually left, per card, so the audit trail and the RL dataset do
    # not go thinner on the fast path than they were on the CLI one.
    with trace.step("log.outbound", correlation, kind="cards"):
        for card in cards[:sent]:
            kind = "file" if card.get("png") is not None else "text"
            summary = card.get("caption") if kind == "file" else card.get("text")
            message_log.record_outbound(
                kind,
                summary or "",
                {
                    "buttons": card.get("buttons"),
                    "correlation_id": correlation,
                    "via": "plugin_route",
                },
                correlation=correlation,
            )

    failures = answer.get("failures") or []
    if failures or not answer.get("ok"):
        first = failures[0].get("reason") if failures else "the route reported failure"
        raise GatewaySendError(
            f"{len(failures) or len(cards) - sent} of {len(cards)} card(s) did not send: {first}"
        )


def send_message(
    target: str,
    text: str,
    buttons: list[dict] | None = None,
    correlation: str | None = None,
    runner=None,
) -> dict:
    """Plain text, optionally with command buttons.

    All renderable content goes in `--message`. It cannot go in a presentation
    `text` block: `action-runtime.ts` computes the presentation's text only when
    no explicit content was supplied, and the CLI refuses a send with neither.
    So through this path a presentation carries buttons and nothing else, and
    text blocks would be dropped with no error (found live 2026-08-01).
    """
    args = ["--message", text]
    if buttons:
        args += ["--presentation", json.dumps(build_buttons(buttons))]
    return _run(
        "send",
        target,
        args,
        kind="text",
        summary=text,
        payload={"text": text, "buttons": buttons},
        correlation=correlation,
        runner=runner,
    )


def send_file(
    target: str,
    path: str,
    caption: str = "",
    buttons: list[dict] | None = None,
    correlation: str | None = None,
    runner=None,
) -> dict:
    """Photos (rendered cards) and documents (review PDFs) take the same path.

    There is no `--caption` flag. With `--media` set, `--message` *is* the
    caption — verified from the CLI's own help, which marks `--message` required
    "unless --media is set". Buttons on a media message were the blocking spike
    and they render (0.2).
    """
    args = ["--media", path]
    if caption:
        args += ["--message", caption]
    if buttons:
        args += ["--presentation", json.dumps(build_buttons(buttons))]
    return _run(
        "send",
        target,
        args,
        kind="file",
        summary=caption or path,
        payload={"file": path, "caption": caption, "buttons": buttons},
        correlation=correlation,
        runner=runner,
    )


def send_card(
    target: str,
    image: bytes,
    caption: str = "",
    buttons: list[dict] | None = None,
    correlation: str | None = None,
    runner=None,
    stem: str = "card",
) -> dict:
    """Send a rendered Pillow card.

    Callers keep handing over **bytes**, exactly as they do to
    `telegram_bot.send_photo_sync` today. The file-on-disk step is an artefact
    of the gateway CLI wanting a path, and of that path having to live inside
    the gateway's own media roots — neither of which is a caller's problem.
    Keeping the signature is also what keeps the cutover diff small.

    A publish failure raises before anything is sent, so a card that could not
    be written never looks like one that was delivered.
    """
    from . import media_outbox

    with trace.step("outbox.publish", correlation, bytes=len(image)):
        path = media_outbox.publish(image, ".png", stem)
    return send_file(
        target, path, caption=caption, buttons=buttons, correlation=correlation, runner=runner
    )


def edit_message(
    target: str, message_id: str, text: str, correlation: str | None = None, runner=None
) -> dict:
    """Append a tap's result.

    **This takes text only, and that is a platform limit, not an oversight.**
    The `editMessage` *action* accepts a caption and picks `editMode: "caption"`
    when given one — but the CLI exposes no `--caption` flag, so every edit from
    here takes `editMode: "auto"`, which calls `editMessageText` first and falls
    back to `editMessageCaption` only after Telegram rejects it.

    Consequence to expect rather than fix: editing a photo or document caption
    works, and writes `[telegram] editMessage failed: ... there is no text in
    the message to edit` into the *gateway's* log on every success. Since the
    Pillow cards were kept (11.3), that is the normal path for every tap result.
    A caption edit that genuinely fails looks identical in that log. Recorded in
    the deploy docs as an accepted gap; the fix, if it is ever worth one, is to
    move media edits into the in-gateway plugin, which calls the API directly.
    """
    args = ["--message-id", str(message_id), "--message", text]
    return _run(
        "edit",
        target,
        args,
        kind="edit",
        summary=text,
        payload={"message_id": message_id, "text": text},
        correlation=correlation,
        runner=runner,
    )


def react(
    target: str, message_id: str, emoji: str = "👍", correlation: str | None = None, runner=None
) -> bool:
    """The immediate acknowledgement. A failure here must never break the handler.

    Returns whether the reaction landed, so the caller can carry on either way —
    the ack exists so a slow handler does not feel dead, and losing the ack is
    strictly better than losing the handler.
    """
    try:
        _run(
            "react",
            target,
            ["--message-id", str(message_id), "--emoji", emoji],
            kind="react",
            summary=emoji,
            payload={"message_id": message_id, "emoji": emoji},
            correlation=correlation,
            runner=runner,
        )
        return True
    except (GatewaySendError, ValueError) as exc:
        logger.warning("gateway react failed correlation=%s: %s", correlation, exc)
        return False
