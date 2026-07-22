"""Provider-agnostic LLM access. Every LLM caller uses chat()/extract() here;
no other module imports a provider SDK directly (ADR supersedes 0001).

Cerebras, Groq and OpenAI all speak the OpenAI /chat/completions shape, so one
client with a configurable base_url covers them — swap by env var. Gemini stays
selectable (LLM_PROVIDER=gemini) via its own SDK behind the same interface,
extract() only, as a rollback path.
"""
import json
import time

from . import config
from .gemini import _RateLimiter, _log_call  # reuse limiter + call logging (and the tests' anchor)

MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 2

# provider -> (base_url, default_model, api_key)
_PROVIDERS = {
    "cerebras": ("https://api.cerebras.ai/v1", "gpt-oss-120b", config.CEREBRAS_API_KEY),
    "groq": ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile", config.GROQ_API_KEY),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini", config.OPENAI_API_KEY),
}


class LLMUnavailableError(RuntimeError):
    """Raised when the LLM fails after retries or is misconfigured — never swallowed."""


_limiter = _RateLimiter(config.LLM_RATE_LIMIT_PER_MIN)
_client = None


def _resolve() -> tuple[str, str, str]:
    prov = config.LLM_PROVIDER
    if prov not in _PROVIDERS:
        raise LLMUnavailableError(f"Unknown LLM_PROVIDER {prov!r} (expected one of {list(_PROVIDERS)} or 'gemini')")
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


def _completion(client, model: str, messages: list, tools, purpose: str):
    """One provider round-trip with retry/backoff, rate limiting and call logging.
    Returns the assistant message object. Raises LLMUnavailableError on failure."""
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
            if _is_rate_limited(exc) and attempt < MAX_RETRIES:
                time.sleep(BASE_BACKOFF_SECONDS * attempt)
                continue
            break
    raise LLMUnavailableError(f"LLM request failed after retries: {last_error}") from last_error


def chat(messages: list, tools: list | None = None, tool_impls: dict | None = None,
         purpose: str = "chat", max_iterations: int = 4) -> dict:
    """Bounded tool-calling loop over an OpenAI-compatible provider.

    tool_impls maps a tool name -> callable(**args) -> str (the tool's result
    text fed back to the model). The loop runs at most max_iterations rounds,
    then forces a final answer with tools disabled so it always terminates.
    Returns {"text": <assistant reply>}.
    """
    if config.LLM_PROVIDER == "gemini":
        raise LLMUnavailableError("chat() needs an OpenAI-compatible provider; gemini supports extract() only")
    client = _openai_client()
    _base, model, _key = _resolve()
    convo = list(messages)
    for _ in range(max_iterations):
        message = _completion(client, model, convo, tools, purpose)
        if not getattr(message, "tool_calls", None):
            return {"text": message.content or ""}
        convo.append(message.model_dump(exclude_none=True))  # assistant tool-call turn
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
    return {"text": final.content or ""}


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
    Delegates to the legacy Gemini backend when LLM_PROVIDER=gemini."""
    if config.LLM_PROVIDER == "gemini":
        from . import gemini

        try:
            return gemini.extract(prompt, purpose)
        except gemini.GeminiUnavailableError as exc:
            # callers handle one failure type regardless of provider
            raise LLMUnavailableError(str(exc)) from exc
    return chat([{"role": "user", "content": prompt}], purpose=purpose)["text"]
