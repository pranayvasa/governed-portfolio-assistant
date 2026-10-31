"""Thin REST clients for Gemini embeddings and generation.

Plain HTTP via `requests` instead of the vendor SDKs keeps the serverless
bundle small and cold starts fast. Errors are raised as LLMError with no
request or response content attached, so nothing a user typed can leak into logs.
"""

import math
import time

import requests

from core import config


class LLMError(Exception):
    """A provider call failed. `status` is the HTTP status (0 if none)."""

    def __init__(self, provider: str, status: int, reason: str):
        super().__init__(f"{provider} failed: {status} {reason}")
        self.provider = provider
        self.status = status


# --- Circuit breaker --------------------------------------------------------
# Some errors won't clear on a retry: a rejected or restricted key, a billing
# or account problem (401/403, or a 400 about the key or a failed
# precondition), or an exhausted quota (429). Repeating them on every
# visitor's question just sends Google a stream of failing calls, so this
# instance stops calling Gemini for a while and the bot says chat is
# temporarily unavailable. A 500/503 ("overloaded") is ordinary and doesn't
# trip it: those still go to the backup model, then "busy".
ACCOUNT_PAUSE_S = 600
QUOTA_PAUSE_S = 60
_paused_until = 0.0


def paused(now: float | None = None) -> bool:
    """True while the breaker is open and guarded calls are skipped."""
    return (now or time.time()) < _paused_until


def _pause_for(status: int, resp) -> float:
    """Seconds to pause after this error response (0: don't pause). Google's
    error status and message are read only to classify the error; they are
    never stored or logged."""
    if status in (401, 403):
        return ACCOUNT_PAUSE_S
    try:
        err = resp.json().get("error", {})
        text = f"{err.get('status', '')} {err.get('message', '')}".lower()
    except (ValueError, AttributeError):
        text = ""
    if status == 400 and any(w in text for w in ("api key", "api_key", "failed_precondition", "billing")):
        return ACCOUNT_PAUSE_S
    if status == 429:
        return ACCOUNT_PAUSE_S if "suspend" in text else QUOTA_PAUSE_S
    return 0


def _post(provider: str, url: str, headers: dict, body: dict, timeout: float, guarded: bool = True) -> dict:
    """POST to Gemini. `guarded` calls (everything the live API makes) are
    skipped while the breaker is open and can open it; the offline index
    build isn't guarded, so its own retries and back-off still work."""
    global _paused_until
    if guarded and paused():
        raise LLMError(provider, 0, "paused")
    try:
        resp = requests.post(url, headers=headers, json=body, timeout=timeout)
    except requests.RequestException as exc:
        raise LLMError(provider, 0, type(exc).__name__) from None
    if resp.status_code != 200:
        pause = _pause_for(resp.status_code, resp) if guarded else 0
        if pause:
            _paused_until = max(_paused_until, time.time() + pause)
        raise LLMError(provider, resp.status_code, "non-200 response")
    return resp.json()


def _normalise(vec: list[float]) -> list[float]:
    # Truncated (768-dim) Gemini embeddings are not unit length; normalise so
    # cosine similarity is a plain dot product.
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


# --- Embeddings -------------------------------------------------------------

def embed(texts: list[str], retries: int = 3, timeout: float = 60, guarded: bool = False) -> list[list[float]]:
    """Embed already-formatted texts (see EMBED_*_FORMAT). Batches of 100.
    `guarded` puts the call behind the circuit breaker (see _post)."""
    key = config.gemini_key()
    if not key:
        raise LLMError("gemini-embed", 0, "GEMINI_API_KEY not set")
    model = f"models/{config.GEMINI_EMBED_MODEL}"
    url = f"{config.GEMINI_BASE_URL}/{model}:batchEmbedContents"
    headers = {"x-goog-api-key": key}
    out: list[list[float]] = []
    for start in range(0, len(texts), 100):
        body = {
            "requests": [
                {
                    "model": model,
                    "content": {"parts": [{"text": t}]},
                    "outputDimensionality": config.EMBED_DIMS,
                }
                for t in texts[start : start + 100]
            ]
        }
        for attempt in range(retries + 1):
            try:
                data = _post("gemini-embed", url, headers, body, timeout, guarded=guarded)
                break
            except LLMError as exc:
                # Retry rate limits and server errors (only matters for the
                # offline index build; the API calls this with retries=0).
                if attempt == retries or exc.status not in (0, 429, 500, 503):
                    raise
                time.sleep(2 ** (attempt + 2))
        out.extend(_normalise(e["values"]) for e in data["embeddings"])
    return out


def embed_query(question: str) -> list[float]:
    text = config.EMBED_QUERY_FORMAT.format(text=question)
    return embed([text], retries=0, timeout=config.EMBED_TIMEOUT_S, guarded=True)[0]


# --- Generation -------------------------------------------------------------
# `messages` is a list of {"role": "user" | "assistant", "content": str}.

def gemini_generate(system: str, messages: list[dict], model: str | None = None,
                    timeout: float | None = None) -> str:
    key = config.gemini_key()
    if not key:
        raise LLMError("gemini", 0, "GEMINI_API_KEY not set")
    url = f"{config.GEMINI_BASE_URL}/models/{model or config.GEMINI_GEN_MODEL}:generateContent"
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [
            {"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
            for m in messages
        ],
        "generationConfig": {
            "temperature": config.TEMPERATURE,
            "maxOutputTokens": config.MAX_OUTPUT_TOKENS,
            "thinkingConfig": {"thinkingLevel": config.GEMINI_THINKING_LEVEL},
        },
    }
    data = _post("gemini", url, {"x-goog-api-key": key}, body, timeout or config.GEMINI_TIMEOUT_S)
    try:
        parts = data["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError):
        # Blocked by safety filters, or truncated before any visible text.
        raise LLMError("gemini", 200, "no candidate text") from None
    text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
    if not text:
        raise LLMError("gemini", 200, "empty answer")
    return text
