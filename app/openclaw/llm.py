"""Provider-agnostic LLM access. Every LLM caller uses chat()/extract() here;
no other module imports a provider SDK directly (ADR supersedes 0001).

Groq and OpenAI both speak the OpenAI /chat/completions shape, so one client
with a configurable base_url covers them — swap by env var. Gemini stays
selectable (LLM_PROVIDER=gemini) via its own SDK behind the same interface,
extract() only, as a rollback path — and serves extract_vision() regardless of
provider (sole vision-capable backend, ADR-0010). Cerebras was removed
2026-07-23: its free inference tier is sold out for this account (ADR-0009).
"""
import json
import logging
import time

from . import config
from .gemini import _RateLimiter, _log_call  # reuse limiter + call logging (and the tests' anchor)

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 2

# provider -> (base_url, default_model, api_key)
#
# Gemini appears here as well as behind its own SDK, and the two are not
# redundant. Google publishes an OpenAI-compatible surface at
# `/v1beta/openai`, so one base_url change gives `chat()` — the tool loop —
# a Gemini backend, which the SDK path never supported (it is extract-only).
# The SDK path stays because `extract()`/`extract_vision()` already use it and
# rewriting a working extraction path buys nothing.
#
# WHY THIS MATTERED ON 2026-08-04, and why it is not a Groq outage: Groq now
# refuses this network outright. `GET api.groq.com/openai/v1/models` returns
# 403 `{"error":{"message":"Access denied. Please check your network
# settings."}}` **with no Authorization header at all**, and identically from
# inside both containers — so it is not the key, not the account, and not a
# rate limit. Nothing in ADR-0017's fallback chain can help: all four models
# are Groq, so the whole chain is behind the same block (the single-point
# redundancy `docs/failure-modes.md` already names). Gemini answered 200 on
# the same probes, including a tool call against a `claims__*`-shaped schema.
_PROVIDERS = {
    "groq": ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile", config.GROQ_API_KEY),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini", config.OPENAI_API_KEY),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai",
               "gemini-2.5-flash", config.GEMINI_API_KEY),
}

# Groq's daily token budget is PER MODEL ("Rate limit reached for model
# llama-3.3-70b-versatile … on tokens per day"), so an exhausted TPD is
# survivable by moving models — unlike TPM, where waiting is the only cure.
# ADR-0017. ADR-0009 made the provider swappable to absorb single-provider
# failure, but that swap is manual and this failure wasn't the provider's —
# Groq was healthy, one model's daily budget was gone, and the agent was dead.
# Ordered by capability: a weaker answer beats no answer, and the degradation is
# reported to Justin rather than passed off as normal (see agent.handle_message).
#
# Every link probed end-to-end 2026-07-25 against the real 15-tool schema — an
# unproven last link is exactly what fails when the chain is finally needed.
# All picked the right tool with the right date args and completed the loop:
#   openai/gpt-oss-120b    0.9s
#   llama-3.1-8b-instant   2.1s
#   openai/gpt-oss-20b     1.3s
# Deliberately EXCLUDED: qwen/qwen3.6-27b. It answers correctly but took 62s,
# which is not a fallback — it's a hang with a reply at the end.
#
# Gemini's chain was probed the same way on 2026-08-04, against the same
# `claims__*`-shaped tool, and every link below returned `finish_reason:
# tool_calls` with the right tool name:
#   gemini-3.6-flash        1.9s
#   gemini-3.5-flash-lite   1.3s
#   gemini-3.1-flash-lite   1.6s
# EXCLUDED and why, because the exclusions are the part that rots quietly:
#   gemini-3.5-flash        answers correctly, 5.7s — redundant behind faster links
#   gemini-2.5-flash-lite   404, retired by Google
#   gemini-2.0-flash{,-lite} 429 quota exceeded on this key — an unproven link
# This chain covers per-model exhaustion, NOT a provider outage: both entries
# here are still one provider each, which is the single-point redundancy
# `docs/failure-modes.md` names and which is exactly what Groq's network block
# hit on 2026-08-04.
_FALLBACK_MODELS = {
    "groq": ("openai/gpt-oss-120b", "openai/gpt-oss-20b", "llama-3.1-8b-instant"),
    "gemini": ("gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"),
}


class LLMUnavailableError(RuntimeError):
    """Raised when the LLM fails after retries or is misconfigured — never swallowed."""


_limiter = _RateLimiter(config.LLM_RATE_LIMIT_PER_MIN)
_client = None


def _resolve() -> tuple[str, str, str]:
    prov = config.LLM_PROVIDER
    if prov not in _PROVIDERS:
        raise LLMUnavailableError(f"Unknown LLM_PROVIDER {prov!r} (expected one of {list(_PROVIDERS)})")
    base_url, default_model, api_key = _PROVIDERS[prov]
    return base_url, (config.LLM_MODEL or default_model), api_key


def _openai_client():
    global _client
    if _client is not None:
        return _client
    base_url, _model, api_key = _resolve()
    if not api_key:
        raise LLMUnavailableError(f"{config.LLM_PROVIDER.upper()}_API_KEY is not configured")
    from openai import OpenAI  # imported lazily so an unconfigured backend never blocks startup

    _client = OpenAI(base_url=base_url, api_key=api_key)
    return _client


def _is_rate_limited(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    return status == 429 or "429" in str(exc)


def _is_malformed_tool_call(exc: Exception) -> bool:
    """Groq 400 `tool_use_failed`: the model emitted a broken pseudo-XML call
    (seen live: `<function=list_tasks,{"status":"open"}</function>`) instead of
    proper tool_calls JSON, and the API rejected its own output.

    A model formatting slip, not an outage — and nondeterministic, so the same
    prompt usually succeeds on the next attempt. Worth retrying precisely
    because it got MORE likely when the tool surface went 8 -> 15 (ADR-0016):
    without this a single garbled call surfaced to Justin as a raw 400 dump."""
    return "tool_use_failed" in str(exc)


def _is_request_shape_error(exc: Exception) -> bool:
    """A 400 that is NOT a malformed tool call: our own request is wrong (an
    unsupported field, a bad message shape), so every retry reproduces it and
    charges the day's budget to do so. Fail fast and let the caller see it."""
    status = getattr(exc, "status_code", None)
    return (status == 400 or "Error code: 400" in str(exc)) and not _is_malformed_tool_call(exc)


def _is_daily_budget_exhausted(exc: Exception) -> bool:
    """Groq's per-day token cap, distinct from the per-minute one. Only the 429
    body says which — the rate-limit headers never mention TPD at all, which is
    how it went undocumented long enough to take the agent down (ADR-0016)."""
    text = str(exc).lower()
    return "tokens per day" in text or "(tpd)" in text


def _try_model(client, model: str, messages: list, tools, purpose: str):
    """Retry loop for ONE model. Returns the assistant message, or raises the
    last error for _completion to decide whether another model can help."""
    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        _limiter.acquire()
        start = time.monotonic()
        try:
            kwargs = {"model": model, "messages": messages}
            if tools:
                kwargs["tools"] = tools
            response = client.chat.completions.create(**kwargs)
            _log_call(purpose, True, int((time.monotonic() - start) * 1000), None)
            return response.choices[0].message
        except Exception as exc:  # network/API errors — logged, not swallowed
            last_error = exc
            _log_call(purpose, False, int((time.monotonic() - start) * 1000), str(exc))
            if _is_daily_budget_exhausted(exc):
                raise  # no amount of retrying frees a daily cap
            if _is_request_shape_error(exc):
                raise  # retrying our own bad request only spends tokens
            if attempt < MAX_RETRIES:
                if _is_malformed_tool_call(exc):
                    continue  # no backoff: nothing is overloaded, the output was just malformed
                # Everything else is retried: a whitelist of retryable statuses
                # has to be extended by whatever edge response comes next, and
                # the one it was missing (a single `403 Access denied. Please
                # check your network settings.` from Groq, 2026-07-27) ended a
                # chat turn on its FIRST attempt — llm_calls holds one row for
                # it — while the identical request seconds later succeeded.
                time.sleep(BASE_BACKOFF_SECONDS * attempt)
                continue
            break
    raise last_error


# The model that answered the most recent successful call. Read by chat() so a
# caller can tell Justin when he got a fallback instead of the primary.
_last_model_used: str | None = None


def _completion(client, model: str, messages: list, tools, purpose: str):
    """One provider round-trip with retry/backoff, rate limiting and call logging.
    Falls through to the next model's own daily budget when this one's is spent.
    Returns the assistant message object. Raises LLMUnavailableError on failure."""
    global _last_model_used
    chain = [model, *(m for m in _FALLBACK_MODELS.get(config.LLM_PROVIDER, ()) if m != model)]
    last_error: Exception | None = None
    for candidate in chain:
        try:
            message = _try_model(client, candidate, messages, tools, purpose)
        except Exception as exc:
            last_error = exc
            if _is_daily_budget_exhausted(exc) and candidate != chain[-1]:
                logger.warning(
                    "%s daily token budget exhausted — falling back to the next model", candidate
                )
                continue
            break
        _last_model_used = candidate
        return message
    if _is_malformed_tool_call(last_error):
        # Distinct message: nothing is down, so "LLM unavailable" would send
        # Justin looking for an outage that isn't there.
        raise LLMUnavailableError(
            f"the model kept producing a malformed tool call after {MAX_RETRIES} attempts — "
            "try rephrasing the question"
        ) from last_error
    if _is_daily_budget_exhausted(last_error):
        raise LLMUnavailableError(
            f"every model's daily token budget is spent ({', '.join(chain)}). "
            "It's a rolling window, so try again shortly."
        ) from last_error
    raise LLMUnavailableError(f"LLM request failed after retries: {last_error}") from last_error


def _assistant_turn(message) -> dict:
    """The assistant's tool-call turn, reduced to the fields the API accepts BACK
    as input. Was `message.model_dump(exclude_none=True)`, which replayed every
    field the provider happened to emit.

    That broke live the moment a reasoning-capable model answered: gpt-oss-120b
    returns a `reasoning` field, and echoing it produced
    `messages[2].reasoning: reasoning is not supported with this model` — a 400
    that killed the turn. Latent since the tool loop was written; it could only
    surface once the fallback chain (ADR-0017) could route to such a model.

    A whitelist, not a `reasoning` blacklist: the next model to emit some other
    output-only field would otherwise reproduce this exactly. The loop needs
    nothing beyond these three — tool_calls to pair with the tool results, and
    content because some models put text alongside the calls."""
    return {
        "role": "assistant",
        "content": message.content or "",
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.function.name, "arguments": call.function.arguments or "{}"},
            }
            for call in (message.tool_calls or [])
        ],
    }


def chat(messages: list, tools: list | None = None, tool_impls: dict | None = None,
         purpose: str = "chat", max_iterations: int = 4) -> dict:
    """Bounded tool-calling loop over an OpenAI-compatible provider.

    tool_impls maps a tool name -> callable(**args) -> str (the tool's result
    text fed back to the model). The loop runs at most max_iterations rounds,
    then forces a final answer with tools disabled so it always terminates.
    Returns {"text": <assistant reply>, "model": <the model that answered>} —
    "model" may differ from the configured one if a daily budget ran out.
    """
    client = _openai_client()
    _base, model, _key = _resolve()
    convo = list(messages)
    for _ in range(max_iterations):
        message = _completion(client, model, convo, tools, purpose)
        if not getattr(message, "tool_calls", None):
            return {"text": message.content or "", "model": _last_model_used}
        convo.append(_assistant_turn(message))
        for call in message.tool_calls:
            name = call.function.name
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            if not isinstance(args, dict):  # model can emit "null"/a bare value
                args = {}
            impl = (tool_impls or {}).get(name)
            output = impl(**args) if impl else f"unknown tool: {name}"
            convo.append({"role": "tool", "tool_call_id": call.id, "content": str(output)})
    final = _completion(client, model, convo, None, purpose)
    return {"text": final.content or "", "model": _last_model_used}


def extract_vision(prompt: str, image_jpeg: bytes, purpose: str = "vision_extraction") -> str:
    """Prompt + one JPEG image -> model text. Gemini-only regardless of
    LLM_PROVIDER: it's the sole configured backend with vision (verified —
    this Groq account exposes zero vision models)."""
    from . import gemini

    try:
        return gemini.extract_image(prompt, image_jpeg, purpose)
    except gemini.GeminiUnavailableError as exc:
        raise LLMUnavailableError(str(exc)) from exc


def extract(prompt: str, purpose: str = "extraction") -> str:
    """Single-message completion — the drop-in for the old gemini.extract().
    Delegates to the legacy Gemini backend when LLM_PROVIDER=gemini.

    That delegation stays even though `gemini` is now an OpenAI-compatible entry
    too: this path carries `gemini.py`'s rate limiter and `llm_calls` logging,
    and invoice extraction is the one LLM caller whose output is cached forever
    (`email_extractions`). Changing which client produces it is a change nobody
    asked for. `chat()` is the half that had no Gemini backend at all."""
    if config.LLM_PROVIDER == "gemini":
        from . import gemini

        try:
            return gemini.extract(prompt, purpose)
        except gemini.GeminiUnavailableError as exc:
            # callers handle one failure type regardless of provider
            raise LLMUnavailableError(str(exc)) from exc
    return chat([{"role": "user", "content": prompt}], purpose=purpose)["text"]
