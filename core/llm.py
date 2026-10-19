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


def _post(provider: str, url: str, headers: dict, body: dict, timeout: float) -> dict:
    try:
        resp = requests.post(url, headers=headers, json=body, timeout=timeout)
    except requests.RequestException as exc:
        raise LLMError(provider, 0, type(exc).__name__) from None
    if resp.status_code != 200:
        raise LLMError(provider, resp.status_code, "non-200 response")
    return resp.json()


def _normalise(vec: list[float]) -> list[float]:
    # Truncated (768-dim) Gemini embeddings are not unit length; normalise so
    # cosine similarity is a plain dot product.
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


# --- Embeddings -------------------------------------------------------------

def embed(texts: list[str], retries: int = 3, timeout: float = 60) -> list[list[float]]:
    """Embed already-formatted texts (see EMBED_*_FORMAT). Batches of 100."""
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
                data = _post("gemini-embed", url, headers, body, timeout)
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
    return embed([text], retries=0, timeout=config.EMBED_TIMEOUT_S)[0]


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
