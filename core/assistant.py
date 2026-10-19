"""Question in, grounded answer out. Shared by the CLI and the serverless API.

Nothing here logs question or answer text. Callers get back the answer plus
metadata (retrieval method, provider, scores) that is safe to log.
"""

import time
from dataclasses import dataclass, field

from core import config, guardrails, llm, prompt, retrieval

_NAME, _P = config.PERSONA_SHORT, config.PRONOUNS
NOT_COVERED = (
    f"That isn't covered in {_NAME}'s documented experience. "
    f"For anything beyond {_P['possessive']} resume and writing, "
    f"connect with {_P['object']} on LinkedIn: {config.LINKEDIN_URL}"
)
UNAVAILABLE = (
    "The assistant is unavailable right now. "
    f"Please try again later, or connect with {_NAME} on LinkedIn: {config.LINKEDIN_URL}"
)
# Both Gemini models failed or were overloaded. The bot fails closed: no
# weaker model answers in their place.
BUSY = (
    "The assistant is busy right now. Please try again in a moment, "
    f"or connect with {_NAME} on LinkedIn: {config.LINKEDIN_URL}"
)


@dataclass
class Answer:
    text: str
    status: str  # "answered" | "not_covered" | "busy" | "unavailable"
    provider: str | None = None  # "gemini" | "gemini-retry" | "gemini-backup"
    retrieval_method: str | None = None  # "embedding" | "bm25"
    top_score: float | None = None
    sources: list[str] = field(default_factory=list)
    chunks: list[dict] = field(default_factory=list)  # for --debug only
    latency_ms: int = 0


def _trim_history(history: list[dict]) -> list[dict]:
    return history[-2 * config.MAX_HISTORY_TURNS :]


def answer(question: str, history: list[dict] | None = None) -> Answer:
    """`history` is prior turns as {"role": "user"|"assistant", "content": str},
    with user turns holding only the bare question (no retrieved context)."""
    start = time.monotonic()
    history = _trim_history(history or [])

    try:
        result = retrieval.retrieve(question)
        # A follow-up like "and at his previous employer?" may not stand on its own: retry with
        # the previous question for context before declaring it not covered.
        if not result.relevant:
            prev = next((m["content"] for m in reversed(history) if m["role"] == "user"), None)
            if prev:
                retry = retrieval.retrieve(f"{prev}\n{question}")
                if retry.relevant:
                    result = retry
    except Exception:
        # Index missing or corrupt: nothing to ground on.
        return Answer(UNAVAILABLE, "unavailable", latency_ms=_ms(start))

    top = result.hits[0].score if result.hits else None
    meta = dict(
        retrieval_method=result.method,
        top_score=top,
        chunks=[h.chunk | {"score": h.score} for h in result.hits],
    )

    if not result.relevant:
        return Answer(NOT_COVERED, "not_covered", **meta, latency_ms=_ms(start))

    chunks = [h.chunk for h in result.hits]
    messages = [*history, {"role": "user", "content": prompt.user_message(question, chunks)}]
    system = prompt.system_prompt()
    sources = list(dict.fromkeys(c["source_label"] for c in chunks))

    deadline = start + config.GENERATION_DEADLINE_S
    last_failed_fast = False
    for provider, model, cap, reserve, only_after_fast_fail in _attempts():
        if only_after_fast_fail:
            if not last_failed_fast:
                continue
            time.sleep(config.RETRY_PAUSE_S)
        left = deadline - time.monotonic() - reserve
        if left < config.MIN_ATTEMPT_S:
            continue
        began = time.monotonic()
        try:
            raw = llm.gemini_generate(system, messages, model=model, timeout=min(cap, left))
        except llm.LLMError:
            last_failed_fast = time.monotonic() - began < config.FAST_FAIL_S
            continue
        text, cited = guardrails.finalise_answer(raw, sources)
        return Answer(text, "answered", provider=provider, sources=cited, **meta, latency_ms=_ms(start))
    return Answer(BUSY, "busy", **meta, latency_ms=_ms(start))


def _attempts():
    """(provider label, model, timeout cap, seconds to keep in reserve for the
    backup, only run if the previous attempt failed fast)."""
    main, backup = config.GEMINI_GEN_MODEL, config.GEMINI_BACKUP_MODEL
    return [
        ("gemini", main, config.GEMINI_TIMEOUT_S, config.BACKUP_RESERVE_S, False),
        ("gemini-retry", main, config.GEMINI_TIMEOUT_S, config.BACKUP_RESERVE_S, True),
        ("gemini-backup", backup, config.BACKUP_TIMEOUT_S, 0, False),
    ]


def _ms(start: float) -> int:
    return round((time.monotonic() - start) * 1000)
