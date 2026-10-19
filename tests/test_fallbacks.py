"""Offline tests for generation fallbacks: main Gemini (one quick retry if it
fails fast) -> backup Gemini -> fail closed."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import assistant, config, llm, retrieval  # noqa: E402

CHUNK = {"id": "resume-11", "source": "resume", "source_label": "Resume", "section": "Bank A", "text": "PD model at Bank A."}
BOTH = (config.GEMINI_GEN_MODEL, config.GEMINI_BACKUP_MODEL)


@pytest.fixture
def calls(monkeypatch):
    """Relevant retrieval, no network; records which models were called."""
    log = []
    monkeypatch.setattr(
        retrieval, "retrieve",
        lambda q: retrieval.Retrieval([retrieval.Hit(CHUNK, 0.8)], "embedding", True),
    )
    monkeypatch.setattr(assistant.prompt, "system_prompt", lambda: "system")
    monkeypatch.setattr(assistant.time, "sleep", lambda s: None)
    return log


def fake_gemini(log, fail_models=(), fail_first_n=0, answer="Bank A.\n\nSources: Resume"):
    """Fails instantly (a fast 503) for fail_models, or for the first n calls."""
    def gen(system, messages, model=None, timeout=None):
        log.append((model, timeout))
        if model in fail_models or len(log) <= fail_first_n:
            raise llm.LLMError("gemini", 503, "high demand")
        return answer
    return gen


def test_primary_gemini_answers(calls, monkeypatch):
    monkeypatch.setattr(llm, "gemini_generate", fake_gemini(calls))
    ans = assistant.answer("Where was the PD model built?")
    assert ans.provider == "gemini"
    assert [c[0] for c in calls] == [config.GEMINI_GEN_MODEL]


def test_main_model_retried_once_after_a_fast_failure(calls, monkeypatch):
    monkeypatch.setattr(llm, "gemini_generate", fake_gemini(calls, fail_first_n=1))
    ans = assistant.answer("Where was the PD model built?")
    assert ans.provider == "gemini-retry"
    assert [c[0] for c in calls] == [config.GEMINI_GEN_MODEL, config.GEMINI_GEN_MODEL]


def test_backup_gemini_when_main_model_keeps_failing(calls, monkeypatch):
    monkeypatch.setattr(llm, "gemini_generate", fake_gemini(calls, fail_models=(config.GEMINI_GEN_MODEL,)))
    ans = assistant.answer("Where was the PD model built?")
    assert ans.provider == "gemini-backup"
    assert [c[0] for c in calls] == [config.GEMINI_GEN_MODEL, config.GEMINI_GEN_MODEL, config.GEMINI_BACKUP_MODEL]
    assert calls[-1][1] == config.BACKUP_TIMEOUT_S  # the slower backup gets its longer timeout


def test_fails_closed_when_both_geminis_fail(calls, monkeypatch):
    """No other model answers in Gemini's place: the visitor gets a friendly
    busy message with the LinkedIn link."""
    monkeypatch.setattr(llm, "gemini_generate", fake_gemini(calls, fail_models=BOTH))
    ans = assistant.answer("Where was the PD model built?")
    assert ans.status == "busy" and ans.provider is None
    assert ans.text == assistant.BUSY and config.LINKEDIN_PATH in ans.text
    assert [c[0] for c in calls] == [config.GEMINI_GEN_MODEL, config.GEMINI_GEN_MODEL, config.GEMINI_BACKUP_MODEL]
    assert not hasattr(llm, "groq_generate")


def test_timeouts_fit_the_deadline(calls, monkeypatch):
    """If every call hangs for its full timeout, the request still fits the
    deadline, a slow failure is not retried, and the backup still gets a real
    attempt."""
    clock = [1000.0]
    monkeypatch.setattr(assistant.time, "monotonic", lambda: clock[0])

    def hanging_gemini(system, messages, model=None, timeout=None):
        calls.append((model, timeout))
        clock[0] += timeout
        raise llm.LLMError("gemini", 0, "Timeout")

    def slow_retrieve(q):  # the embedding call also hangs for its full timeout
        clock[0] += config.EMBED_TIMEOUT_S
        return retrieval.Retrieval([retrieval.Hit(CHUNK, 0.8)], "embedding", True)

    monkeypatch.setattr(llm, "gemini_generate", hanging_gemini)
    monkeypatch.setattr(assistant.retrieval, "retrieve", slow_retrieve)

    start = clock[0]
    ans = assistant.answer("Where was the PD model built?")
    assert ans.status == "busy"
    assert [c[0] for c in calls] == list(BOTH)
    assert calls[-1][1] >= config.MIN_ATTEMPT_S
    # Whole request, retrieval included, fits the function limit in vercel.json.
    max_duration = json.loads((config.ROOT / "vercel.json").read_text())["functions"]["api/chat.py"]["maxDuration"]
    assert clock[0] - start <= config.GENERATION_DEADLINE_S < max_duration
    assert calls[-1][1] == config.BACKUP_TIMEOUT_S  # the backup still gets its full timeout
