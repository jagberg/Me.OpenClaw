"""The claims read surface, exposed to the gateway's agent over MCP.

This is the *conversational* half of the split. Deterministic work — a button
tap, a cron tick — goes to `/internal` and never touches a model (ADR-0025,
and Justin's "no MCP for deterministic calls"). What arrives here is a question
somebody typed, which needs a model to interpret and therefore needs the domain
exposed as tools.

**Read only, and that is a boundary rather than a milestone.** Every mutation
is a proposal that commits behind a confirm tap, and none of that lives here.
The inventory below is also the `gmail-isolation-boundary` enforcement surface:
no filesystem tool, no shell, no browser, no mailbox search, nothing returning a
secret. `test_mcp_inventory_has_no_dangerous_tool` fails if one appears.

Every schema in this file ships in **every** agent turn, so the inventory is a
per-turn token cost with a measured budget: a trimmed turn is 3,865 tokens
against Groq's 12,000 TPM, leaving ~8,100 for this. One-line descriptions are
deliberate, and the count is asserted rather than left to taste.

## Why this is hand-rolled rather than the official SDK

Tried first, reverted. `mcp` 2.0.0 pulls starlette 1.3.1, and this app pins
fastapi 0.115.6, which requires starlette <0.42 — `pip check` went red
immediately. It also drags in opentelemetry, jsonschema, httpx2, pyjwt and
pywin32, into the one container whose small surface is part of the security
story. Streamable HTTP without a stream is a JSON-RPC POST endpoint; that is
what this is. Validated against the gateway's own `openclaw mcp probe`, which
opens a live MCP connection — the product's validator, per the project's rule
about never trusting a payload we merely believe is right.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, Response

from . import config, db

logger = logging.getLogger(__name__)

router = APIRouter()

# The MCP revision this server implements. A client asking for something else is
# answered with ours rather than refused — the spec's own negotiation rule, and
# refusing would turn a version skew into an outage.
PROTOCOL_VERSION = "2025-06-18"

SERVER_NAME = "openclaw-claims"

# Read at session start by the client and shown to the model. Deliberately says
# what this surface cannot do: the stock agent asserted it had checked a mailbox
# in a runtime holding no mail credential, so the absence has to be stated, not
# left to be inferred from an inventory nobody reads.
INSTRUCTIONS = (
    "Justin's vet-insurance claims. Call turn_context first — it carries today's date and the "
    "only pets that exist, both read live from the database. Never guess a pet name or resolve a "
    "relative date without it. Every claim you mention must carry its #id; he acts by id. "
    "This surface is READ ONLY and cannot search his mailbox, read files or change anything. "
    "If something needs changing, say so and let him tap."
)


def _tool(name: str, description: str, properties: dict, required: list | None = None) -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": {"type": "object", "properties": properties, "required": required or []},
    }


_PET = {"type": "string", "description": "pet name (partial ok)"}
_REF = {"type": "string", "description": "Petcover reference (partial ok)"}
_SINCE = {"type": "string", "description": "earliest transaction date, YYYY-MM-DD"}
_UNTIL = {"type": "string", "description": "latest transaction date, YYYY-MM-DD"}
_MERCHANT = {"type": "string", "description": "vet/merchant name (partial ok)"}

# One place, enumerated, no dynamic or wildcard registration — task 2.2. A tool
# that can only appear by being written here is a tool that can be counted, and
# the count is what 19a.3 asserts.
TOOLS = [
    _tool("turn_context", "Today's date and the only pets that exist, read live from the database. "
          "Call before answering anything involving a pet name or a relative date.", {}),
    _tool("query_claims", "List claims filtered by status, pet, vet and/or transaction-date range.",
          {"status": {"type": "string", "description": "e.g. pending_match, matched, drafted, sent, "
                      "acknowledged, info_requested, suspended, approved, settled, declined"},
           "pet": _PET, "merchant": _MERCHANT, "since": _SINCE, "until": _UNTIL}),
    _tool("pending_actions", "THE list of everything waiting on Justin, with claim ids, amounts and age. "
          "Use for any 'what do I need to do / what's outstanding' question.",
          {"since": _SINCE, "until": _UNTIL}),
    _tool("claim_detail", "One claim in full by id: invoice items, claimable, flag, and every reply "
          "with its dollar figures. Use for 'why is claim #N like this'.",
          {"claim_id": {"type": "integer"}}, required=["claim_id"]),
    _tool("claim_history", "A claim's Petcover reply/status-event history, found by pet and/or reference.",
          {"pet": _PET, "reference": _REF}),
    _tool("submissions_awaiting_reply",
          "Claims sent to PETCOVER and whether a reply came back, one entry per submission.", {}),
    _tool("list_tasks", "Justin's non-claim tasks (household admin, follow-ups).",
          {"status": {"type": "string", "description": "open or closed"}}),
]

TOOL_NAMES = [t["name"] for t in TOOLS]

# Words that must never name a tool here. Not a filter — a tripwire. The
# inventory is written by hand, so anything matching these got here on purpose
# and the point is that the suite says so out loud before a deploy does.
FORBIDDEN_TOOL_SUBSTRINGS = (
    "file", "read_", "write", "shell", "exec", "bash", "browser", "fetch", "http",
    "mail", "gmail", "inbox", "search_mail", "secret", "token", "credential", "env",
    "password", "key", "send", "draft", "delete", "sql", "query_db",
)


def turn_context() -> str:
    """Read at call time, never baked into agent config.

    The model invented 'Whiskers' and 'Fluffy' when it had to guess, and guessed
    "July 2025" with nothing to anchor on. A pet list written into a workspace
    file would be correct until the day a pet is added, and then wrong silently
    — which is why the shipped `USER.md` deliberately omits it.
    """
    with db.get_connection() as conn:
        pets = [r["name"] for r in conn.execute("SELECT name FROM pets ORDER BY name")]
    today = datetime.now(timezone.utc).date().isoformat()
    return (f"Today is {today}. The ONLY pets that exist: {', '.join(pets) if pets else '(none on file)'}. "
            "Resolve relative dates against today and state the range you used.")


def _impls() -> dict:
    """Reuse the chat agent's read implementations rather than restating them.

    They are the same answers the current bot gives, derived from the same
    queries — `pending_actions` in particular shares its derivation with the
    /actions cards specifically so chat and cards can never disagree. A second
    copy here would be a second answer to the same question, which is the shape
    this codebase has been bitten by five times.

    `_build_impls` takes a proposals list because the mutating tools append to
    it. Passing one that is thrown away is not a loophole: no `propose_*` name
    is in TOOLS, so nothing here can reach them.
    """
    from . import agent

    impls = agent._build_impls([], "")
    selected = {name: impls[name] for name in TOOL_NAMES if name in impls}
    selected["turn_context"] = turn_context
    return selected


def _error(rid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _result(rid, payload: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": payload}


def _call_tool(name: str, arguments: dict) -> dict:
    impls = _impls()
    fn = impls.get(name)
    if fn is None:
        # isError, not a JSON-RPC error: the spec routes tool failures back to
        # the model so it can recover, and a transport-level error would just
        # end the turn with nothing said.
        return {"content": [{"type": "text", "text": f"No such tool: {name}"}], "isError": True}
    try:
        text = fn(**(arguments or {}))
    except TypeError as exc:
        return {"content": [{"type": "text", "text": f"Bad arguments for {name}: {exc}"}], "isError": True}
    except Exception as exc:  # noqa: BLE001 — visible failure, never a silent empty answer
        logger.error("mcp tool %s failed: %s", name, exc, exc_info=True)
        return {"content": [{"type": "text", "text": f"{name} failed: {exc}"}], "isError": True}
    return {"content": [{"type": "text", "text": str(text)}], "isError": False}


def dispatch(message: dict):
    """One JSON-RPC message in, one response out — or None for a notification.

    Kept separate from the route so the suite can drive it directly, the same
    reason `internal_api.record_event` is split out.
    """
    method = message.get("method")
    rid = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        return _result(rid, {
            "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": config.APP_VERSION},
            "instructions": INSTRUCTIONS,
        })
    if method == "ping":
        return _result(rid, {})
    if method == "tools/list":
        return _result(rid, {"tools": TOOLS})
    if method == "tools/call":
        return _result(rid, _call_tool(params.get("name") or "", params.get("arguments") or {}))
    if rid is None:
        # A notification (`notifications/initialized` and friends). No response
        # is the correct answer; returning one is a protocol violation.
        return None
    return _error(rid, -32601, f"Method not found: {method}")


@router.post("/mcp")
async def mcp_endpoint(
    request: Request,
    x_openclaw_secret: str | None = Header(default=None),
):
    """Streamable HTTP, minus the stream.

    Guarded by the same shared secret as `/internal`. It has to be: the answers
    here name pets, vets, amounts and dates, so an open endpoint leaks the
    claims history to anything that can reach the port.
    """
    from . import internal_api

    if internal_api._authorized(request, x_openclaw_secret) is not None:
        logger.warning("mcp request rejected")
        return JSONResponse({"error": "rejected"}, status_code=403)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse(_error(None, -32700, "Parse error"), status_code=400)

    # A client may batch. Handle both shapes rather than assuming the singular
    # one and failing obscurely on the day something batches.
    if isinstance(body, list):
        responses = [r for r in (dispatch(m) for m in body) if r is not None]
        return JSONResponse(responses) if responses else Response(status_code=202)
    response = dispatch(body)
    return JSONResponse(response) if response is not None else Response(status_code=202)


@router.get("/mcp")
async def mcp_stream_unsupported():
    """No server-initiated stream. The spec says answer 405 rather than hang.

    Nothing here pushes: the agent asks, the app answers. Server-sent events
    would only matter if this surface needed to notify, and notification is the
    gateway's job on the other side of the boundary.
    """
    return Response(status_code=405)
