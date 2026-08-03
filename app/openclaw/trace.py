"""Step timings in the log, so a slow path is measured rather than argued about.

Added 2026-08-03 because three successive explanations of `/actions` latency were
wrong, each derived from a total rather than from a decomposition. The totals
were real; the splits attributed to them were invented. What the log now carries
is one line per step, so the next answer comes from the log.

**What was measured before this existed**, from the app container against the
deployed gateway, and what it costs (min of 2–3 runs each):

| Probe | Time | What it proves |
|---|---|---|
| `openclaw --version` | 0.06s | node boot is free; the cost is not "starting a process" |
| `openclaw message send --dry-run` | 6.6s | **no gateway contact at all** (`handledBy: "core"`, and the gateway logged zero RPCs for three of them) — so ~6.6s is the CLI initialising the `message` subcommand, entirely local |
| `openclaw health` | 2.4s | a light command that does connect + auth + a read RPC |
| real send to the registered chat | 9.8–13.1s | the end-to-end cost the app pays per message |
| the gateway's own `message.action` | 0.3–1.1s | its work, logged as `[ws] ⇄ res ✓ message.action 521ms`, landing in the FINAL second of the 9.8s |

So the dominant term is **per-invocation CLI initialisation**, not the WebSocket
connection and not the gateway. That matters because it rules out the fix that
looked obvious: keeping a connection open saves the ~2.5s of connect, and leaves
the ~6.6s of local init untouched — the CLI is one process per message and
cannot be kept warm. The only shapes that remove the 6.6s are not shelling out
at all (a long-lived WebSocket client in this process) or sending from inside the
gateway (where the plugin already runs).

`ms` here is wall time and includes anything the step waited on. That is the
point — a step that is slow because it queued behind a thread pool should look
slow.
"""

import contextlib
import logging
import time

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def step(name: str, correlation: str | None = None, **fields):
    """Log `name` with its wall time, whether it succeeded or raised.

    ponytail: INFO, unconditional, no sampling and no config flag. One line per
    step is cheap next to a 9-second send, and a trace you have to turn on is a
    trace nobody has on when the slow thing happens. Delete the call sites when
    the latency work is finished — the module is small on purpose.

    A raising step still logs, tagged `failed=1`, because a path that is slow
    *because* it errors is exactly what a timing hunt needs to see.
    """
    start = time.perf_counter()
    failed = 0
    try:
        yield
    except BaseException:
        failed = 1
        raise
    finally:
        extra = "".join(f" {k}={v}" for k, v in fields.items())
        logger.info("trace step=%s ms=%.0f%s%s%s", name,
                    (time.perf_counter() - start) * 1000, extra,
                    f" correlation={correlation}" if correlation else "",
                    " failed=1" if failed else "")
