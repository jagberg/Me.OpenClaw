import threading
import time
from collections import deque
from datetime import datetime, timezone

from google import genai
from google.genai import errors as genai_errors

from . import config, db, ssl_compat

ssl_compat.patch_requests_to_use_os_trust_store()

MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 2


class GeminiUnavailableError(RuntimeError):
    """Raised when Gemini fails after retries — callers must not swallow this."""


class _RateLimiter:
    """In-memory sliding-window limiter, one process only — good enough for a single local app."""

    def __init__(self, max_per_minute: int):
        self.max_per_minute = max_per_minute
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            while self._calls and now - self._calls[0] > 60:
                self._calls.popleft()
            if len(self._calls) >= self.max_per_minute:
                sleep_for = 60 - (now - self._calls[0])
                if sleep_for > 0:
                    time.sleep(sleep_for)
            self._calls.append(time.monotonic())


_limiter = _RateLimiter(config.GEMINI_RATE_LIMIT_PER_MIN)
_client = genai.Client(api_key=config.GEMINI_API_KEY) if config.GEMINI_API_KEY else None


def _log_call(purpose: str, success: bool, latency_ms: int, error: str | None) -> None:
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO llm_calls (created_at, purpose, success, latency_ms, error) "
            "VALUES (?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), purpose, int(success), latency_ms, error),
        )


def _is_rate_limited(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    return status == 429 or "429" in str(exc)


def extract(prompt: str, purpose: str = "extraction") -> str:
    """Send a prompt to Gemini 2.5 Flash. Raises GeminiUnavailableError on unrecoverable failure."""
    if _client is None:
        raise GeminiUnavailableError("GEMINI_API_KEY is not configured")

    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        _limiter.acquire()
        start = time.monotonic()
        try:
            response = _client.models.generate_content(
                model=config.GEMINI_MODEL,
                contents=prompt,
            )
            latency_ms = int((time.monotonic() - start) * 1000)
            _log_call(purpose, True, latency_ms, None)
            return response.text
        except genai_errors.APIError as exc:  # covers 429/5xx from the API
            last_error = exc
            latency_ms = int((time.monotonic() - start) * 1000)
            _log_call(purpose, False, latency_ms, str(exc))
            if _is_rate_limited(exc) and attempt < MAX_RETRIES:
                time.sleep(BASE_BACKOFF_SECONDS * attempt)
                continue
            break
        except Exception as exc:  # network errors, etc. — still not silent
            last_error = exc
            latency_ms = int((time.monotonic() - start) * 1000)
            _log_call(purpose, False, latency_ms, str(exc))
            break

    raise GeminiUnavailableError(f"Gemini request failed after retries: {last_error}") from last_error
