"""Offline tests for abuse protection: per-IP and daily limits, the battery
bypass, and the Gemini circuit breaker. No network: requests.post is faked."""

import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api import chat  # noqa: E402
from core import assistant, config, limits, llm, retrieval  # noqa: E402

CHUNK = {"id": "resume-01", "source": "resume", "source_label": "Resume", "section": "Bank A", "text": "PD model at Bank A."}


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    limits._hits.clear()
    limits._day.clear()
    monkeypatch.setattr(llm, "_paused_until", 0.0)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("BATTERY_TOKEN", raising=False)


class FakeResponse:
    def __init__(self, status, error=None, body=None):
        self.status_code = status
        self._body = body if body is not None else {"error": error or {}}

    def json(self):
        return self._body


def fake_post(responses, log):
    """requests.post stand-in: returns the queued responses in order."""
    def post(url, headers=None, json=None, timeout=None):
        log.append(url)
        return responses.pop(0)
    return post


OK = FakeResponse(200, body={"candidates": [{"content": {"parts": [{"text": "Bank A.\n\nSources: Resume"}]}}]})


# --- per-IP and daily limits ------------------------------------------------

def test_ip_limit_is_10_per_10_minutes_and_30_per_hour():
    ip, now = "203.0.113.7", 1_000_000.0
    assert all(limits.allow_ip(ip, now + i) for i in range(10))
    assert not limits.allow_ip(ip, now + 10)
    # A new 10-minute window opens, but the hourly limit still counts everything.
    t = now + 601
    allowed = sum(limits.allow_ip(ip, t + i * 61) for i in range(40))
    assert allowed == 20  # 30 per hour in total
    assert limits.allow_ip("198.51.100.1", now)  # other visitors are unaffected


def test_daily_cap_per_instance():
    now = 2_000_000.0
    assert all(limits.allow_daily(now + i) for i in range(limits.DAILY_MAX_QUESTIONS))
    assert not limits.allow_daily(now + limits.DAILY_MAX_QUESTIONS)
    assert limits.allow_daily(now + limits.DAY_S + 1)  # the oldest question has aged out


def test_battery_bypass_needs_the_exact_secret(monkeypatch):
    assert not limits.battery_bypass("anything")  # off unless BATTERY_TOKEN is set
    monkeypatch.setenv("BATTERY_TOKEN", "s3cret-token")
    assert limits.battery_bypass("s3cret-token")
    assert not limits.battery_bypass("s3cret-toke")
    assert not limits.battery_bypass("")
    assert not limits.battery_bypass(None)


# --- the endpoint -----------------------------------------------------------

def call_api(question="Where was the PD model built?", ip="203.0.113.9", token=None):
    body = json.dumps({"question": question}).encode()
    h = chat.handler.__new__(chat.handler)
    h.headers = {"content-length": str(len(body)), "x-forwarded-for": ip}
    if token:
        h.headers[chat.BATTERY_HEADER] = token
    h.rfile = io.BytesIO(body)
    return h._handle()


@pytest.fixture
def stub_answer(monkeypatch):
    monkeypatch.setattr(assistant, "answer", lambda q, h=None: assistant.Answer("ok", "answered"))


def test_api_rate_limits_an_ip_and_battery_token_skips_it(stub_answer, monkeypatch):
    codes = [call_api()[0] for _ in range(11)]
    assert codes[:10] == [200] * 10 and codes[10] == 429
    assert call_api()[1]["status"] == "rate_limited"
    monkeypatch.setenv("BATTERY_TOKEN", "s3cret-token")
    assert call_api(token="s3cret-token")[0] == 200
    assert call_api(token="wrong")[0] == 429


def test_api_daily_cap(stub_answer, monkeypatch):
    monkeypatch.setattr(limits, "DAILY_MAX_QUESTIONS", 3)
    results = [call_api(ip=f"198.51.100.{i}") for i in range(4)]
    assert [r[0] for r in results] == [200, 200, 200, 429]
    assert results[3][1]["status"] == "daily_limit"
    assert config.LINKEDIN_PATH in results[3][1]["answer"]


# --- circuit breaker --------------------------------------------------------

@pytest.mark.parametrize("response, pause", [
    (FakeResponse(403, {"status": "PERMISSION_DENIED", "message": "billing account suspended"}), llm.ACCOUNT_PAUSE_S),
    (FakeResponse(401, {"status": "UNAUTHENTICATED"}), llm.ACCOUNT_PAUSE_S),
    (FakeResponse(400, {"status": "INVALID_ARGUMENT", "message": "API key not valid."}), llm.ACCOUNT_PAUSE_S),
    (FakeResponse(400, {"status": "FAILED_PRECONDITION", "message": "..."}), llm.ACCOUNT_PAUSE_S),
    (FakeResponse(429, {"status": "RESOURCE_EXHAUSTED", "message": "You exceeded your current quota"}), llm.QUOTA_PAUSE_S),
    (FakeResponse(429, {"status": "RESOURCE_EXHAUSTED", "message": "Service tier suspended"}), llm.ACCOUNT_PAUSE_S),
])
def test_breaker_opens_on_account_and_quota_errors(monkeypatch, response, pause):
    log = []
    monkeypatch.setattr(llm.requests, "post", fake_post([response], log))
    with pytest.raises(llm.LLMError):
        llm.gemini_generate("system", [{"role": "user", "content": "q"}])
    assert llm.paused()
    assert llm._paused_until == pytest.approx(llm.time.time() + pause, abs=5)
    # While open, guarded calls fail at once without touching the network.
    with pytest.raises(llm.LLMError):
        llm.embed_query("q")
    assert len(log) == 1


@pytest.mark.parametrize("status", [500, 503])
def test_overload_does_not_open_the_breaker(monkeypatch, status):
    monkeypatch.setattr(llm.requests, "post", fake_post([FakeResponse(status, {"status": "UNAVAILABLE"})], []))
    with pytest.raises(llm.LLMError):
        llm.gemini_generate("system", [{"role": "user", "content": "q"}])
    assert not llm.paused()


def test_index_build_is_not_guarded(monkeypatch):
    """The offline build keeps its own retries: a 429 doesn't pause it."""
    log = []
    responses = [FakeResponse(429, {"message": "quota"}), FakeResponse(200, body={"embeddings": [{"values": [1.0, 0.0]}]})]
    monkeypatch.setattr(llm.requests, "post", fake_post(responses, log))
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    assert llm.embed(["text"], retries=1) == [[1.0, 0.0]]
    assert not llm.paused() and len(log) == 2


@pytest.fixture
def relevant(monkeypatch):
    monkeypatch.setattr(retrieval, "retrieve", lambda q: retrieval.Retrieval([retrieval.Hit(CHUNK, 0.8)], "embedding", True))
    monkeypatch.setattr(assistant.prompt, "system_prompt", lambda: "system")
    monkeypatch.setattr(assistant.time, "sleep", lambda s: None)


def test_suspended_account_gets_one_call_then_unavailable(monkeypatch, relevant):
    """A 403 from Gemini: no retry, no backup, a friendly 'temporarily
    unavailable' reply, and no further calls while the breaker is open."""
    log = []
    monkeypatch.setattr(llm.requests, "post", fake_post([FakeResponse(403, {"status": "PERMISSION_DENIED"})], log))
    first = assistant.answer("Where was the PD model built?")
    assert first.status == "unavailable" and first.text == assistant.UNAVAILABLE
    assert "temporarily unavailable" in first.text and config.LINKEDIN_PATH in first.text
    assert len(log) == 1
    second = assistant.answer("And the second question?")
    assert second.status == "unavailable" and len(log) == 1


def test_breaker_closes_after_the_pause(monkeypatch, relevant):
    monkeypatch.setattr(llm, "_paused_until", llm.time.time() - 1)
    monkeypatch.setattr(llm.requests, "post", fake_post([OK], []))
    assert assistant.answer("Where was the PD model built?").status == "answered"
