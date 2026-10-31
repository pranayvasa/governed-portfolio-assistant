"""POST /api/chat — the single serverless endpoint (Vercel Python runtime).

Request:  {"question": str, "history": [{"role", "content"}, ...], "session": str | null}
Response: {"answer": str, "status": str, "sources": [str], "session": str, "remaining": int}

Every response, including failures, carries a friendly `answer` the page can
show as-is. Logs hold one JSON line per request: status code, outcome,
provider, retrieval method and latency. Never the question, the answer, the
history or the caller's IP.
"""

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import assistant, config, guardrails, limits  # noqa: E402

MAX_BODY_BYTES = 16_384

RATE_LIMITED = (
    "You've sent a lot of questions in a short time. Please wait a few minutes and try again, "
    f"or connect with {config.PERSONA_SHORT} on LinkedIn: {config.LINKEDIN_URL}"
)
SESSION_LIMIT = (
    f"You've reached the limit of {limits.SESSION_MAX_QUESTIONS} questions for this session. "
    f"To continue the conversation, connect with {config.PERSONA_SHORT} on LinkedIn: {config.LINKEDIN_URL}"
)
DAILY_LIMIT = (
    "The assistant has answered as many questions as it can for today. Please try again tomorrow, "
    f"or connect with {config.PERSONA_SHORT} on LinkedIn: {config.LINKEDIN_URL}"
)
# Header the live test battery sends to skip the per-IP limit (see limits.battery_bypass).
BATTERY_HEADER = "x-battery-token"

# Optional, comma-separated. Leave unset when the page and API share an origin.
ALLOWED_ORIGINS = {o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()}


def _client_ip(headers) -> str:
    # Vercel puts the real client IP first in x-forwarded-for.
    fwd = headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or headers.get("x-real-ip", "") or "unknown"


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self._send(204, None)

    def do_GET(self):
        # Health check for uptime pings and warm-up. No model calls.
        self._send(200, {"status": "ok"})

    def do_POST(self):
        start = time.monotonic()
        code, payload, meta = 500, {"answer": assistant.UNAVAILABLE, "status": "unavailable"}, {}
        try:
            code, payload, meta = self._handle()
        except Exception as exc:  # never let a traceback (or its locals) reach the logs or the page
            meta = {"error": type(exc).__name__}
        finally:
            self._send(code, payload)
            self._log(code, payload.get("status"), meta, start)

    def _handle(self):
        length = int(self.headers.get("content-length") or 0)
        if length <= 0 or length > MAX_BODY_BYTES:
            return 400, {"answer": "Please type a question.", "status": "invalid"}, {}
        try:
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return 400, {"answer": "Please type a question.", "status": "invalid"}, {}

        try:
            question = guardrails.clean_question(body.get("question"))
        except guardrails.InvalidInput as exc:
            return 400, {"answer": str(exc), "status": "invalid"}, {}

        battery = limits.battery_bypass(self.headers.get(BATTERY_HEADER))
        if not battery and not limits.allow_ip(_client_ip(self.headers)):
            return 429, {"answer": RATE_LIMITED, "status": "rate_limited"}, {}

        asked = limits.read_session(body.get("session"))
        if asked >= limits.SESSION_MAX_QUESTIONS:
            return 429, {
                "answer": SESSION_LIMIT,
                "status": "session_limit",
                "session": body.get("session"),
                "remaining": 0,
            }, {}

        # Counted last, so rejected requests don't use up the day's allowance.
        if not limits.allow_daily():
            return 429, {"answer": DAILY_LIMIT, "status": "daily_limit"}, {}

        history = guardrails.clean_history(body.get("history"))
        ans = assistant.answer(question, history)

        asked += 1
        code = 503 if ans.status in ("busy", "unavailable") else 200
        payload = {
            "answer": ans.text,
            "status": ans.status,
            "sources": ans.sources,
            "session": limits.next_session(asked),
            "remaining": limits.SESSION_MAX_QUESTIONS - asked,
        }
        meta = {"provider": ans.provider, "retrieval": ans.retrieval_method, "battery": battery}
        return code, payload, meta

    # --- plumbing -----------------------------------------------------------

    def _send(self, code: int, payload: dict | None):
        data = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        origin = self.headers.get("origin")
        if origin and origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Vary", "Origin")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _log(self, code: int, outcome, meta: dict, start: float):
        record = {"code": code, "outcome": outcome, "latency_ms": round((time.monotonic() - start) * 1000), **meta}
        print(json.dumps(record), flush=True)

    def log_message(self, format, *args):
        # Silence the default access log; _log writes the only line we keep.
        pass
