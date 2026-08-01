"""The one seam for outbound messages once the gateway owns the bot token.

Today every send goes through `telegram_bot.LoggedBot` so nothing can reach
Telegram without landing in `telegram_messages`. That property is the whole
reason the message log is trustworthy, and it must survive the transport
change — so this module is the LoggedBot of the gateway era: one place that
builds the CLI invocation, one place that logs, one place that fails loudly.

Anything that shells out to the gateway directly bypasses the message log, in
exactly the way a bare `telegram.Bot` does today.
"""

import json
import logging
import subprocess

from . import config, message_log

logger = logging.getLogger(__name__)


class GatewaySendError(RuntimeError):
    """A send that did not happen. Raised, never swallowed."""


def _argv(action: str, chat: str, args: list[str]) -> list[str]:
    """Build the gateway CLI invocation.

    ponytail: every command shape lives in this one function on purpose. The
    exact flags are UNVERIFIED until spike 0.2/0.3 runs against a real gateway
    (`openclaw message send` exists; its flag names are not yet confirmed), so
    when the spike answers, this is the only place that changes.
    """
    return [config.OPENCLAW_CLI, "message", action, "--chat", str(chat), *args, "--json"]


def _run(action: str, chat: str, args: list[str], *, kind: str, summary: str,
         payload: dict, correlation: str | None = None, runner=None) -> dict:
    argv = _argv(action, chat, args)
    # Never log argv wholesale — a caption can carry claim detail and the CLI
    # may grow a token flag. Log the action, not the payload.
    logger.info("gateway %s chat=%s correlation=%s", action, chat, correlation)
    run = runner or subprocess.run
    try:
        completed = run(argv, capture_output=True, text=True, timeout=config.OPENCLAW_CLI_TIMEOUT_SECONDS)
    except FileNotFoundError as exc:
        raise GatewaySendError(f"gateway CLI not found at {config.OPENCLAW_CLI!r}") from exc
    except subprocess.TimeoutExpired as exc:
        raise GatewaySendError(f"gateway {action} timed out after {config.OPENCLAW_CLI_TIMEOUT_SECONDS}s") from exc

    if completed.returncode != 0:
        # The exit code alone is useless for diagnosis, so stderr goes into the
        # reason. This is the human-readable failure the project's rules require:
        # a send that did not happen must never look like one that did.
        stderr = (completed.stderr or "").strip()[:500]
        raise GatewaySendError(f"gateway {action} exit {completed.returncode}: {stderr or 'no stderr'}")

    # Outbound logging stays on this path, so the gateway era keeps the same
    # audit trail and RL dataset the LoggedBot era had.
    message_log.record_outbound(kind, summary, {**payload, "correlation_id": correlation})

    try:
        return json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        # A non-JSON success is not a failure of the send — say so and move on
        # rather than raising and letting a caller retry an already-sent message.
        logger.warning("gateway %s returned non-JSON output correlation=%s", action, correlation)
        return {}


def send_message(chat: str, text: str, buttons: list | None = None, correlation: str | None = None,
                 runner=None) -> dict:
    args = ["--text", text]
    if buttons:
        args += ["--buttons", json.dumps(buttons)]
    return _run("send", chat, args, kind="text", summary=text,
                payload={"text": text, "buttons": buttons}, correlation=correlation, runner=runner)


def send_file(chat: str, path: str, caption: str = "", buttons: list | None = None,
              correlation: str | None = None, runner=None) -> dict:
    """Photos (rendered cards) and documents (review PDFs) take the same path.

    Whether buttons can ride a media message at all is spike 0.2 and it BLOCKS
    the cutover — the card interface is not negotiable.
    """
    args = ["--file", path]
    if caption:
        args += ["--caption", caption]
    if buttons:
        args += ["--buttons", json.dumps(buttons)]
    return _run("send", chat, args, kind="file", summary=caption or path,
                payload={"file": path, "caption": caption, "buttons": buttons},
                correlation=correlation, runner=runner)


def edit_message(chat: str, message_id: str, text: str | None = None, caption: str | None = None,
                 buttons: list | None = None, correlation: str | None = None, runner=None) -> dict:
    """Append a tap's result.

    Exactly one of text/caption applies: a message carrying a document or photo
    has no text, and editing text on one is what used to crash on precisely the
    review alerts that most need feedback. Caption editing through the gateway
    is spike 0.3.
    """
    if (text is None) == (caption is None):
        raise ValueError("edit_message takes exactly one of text or caption")
    args = ["--message", str(message_id)]
    args += ["--text", text] if text is not None else ["--caption", caption]
    if buttons is not None:
        args += ["--buttons", json.dumps(buttons)]
    return _run("edit", chat, args, kind="edit", summary=(text if text is not None else caption),
                payload={"message_id": message_id, "text": text, "caption": caption},
                correlation=correlation, runner=runner)


def react(chat: str, message_id: str, emoji: str = "👍", correlation: str | None = None,
          runner=None) -> bool:
    """The immediate acknowledgement. A failure here must never break the handler.

    Returns whether the reaction landed, so the caller can carry on either way —
    the ack exists so a slow handler does not feel dead, and losing the ack is
    strictly better than losing the handler.
    """
    try:
        _run("react", chat, ["--message", str(message_id), "--emoji", emoji],
             kind="react", summary=emoji, payload={"message_id": message_id, "emoji": emoji},
             correlation=correlation, runner=runner)
        return True
    except (GatewaySendError, ValueError) as exc:
        logger.warning("gateway react failed correlation=%s: %s", correlation, exc)
        return False
