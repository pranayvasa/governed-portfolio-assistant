"""Abuse limits for the public endpoint: per-session and per-IP.

Session cap (20 questions): the server issues an HMAC-signed token holding a
counter; the client sends it back with each question. No server storage. A
client can drop the token to start over, which is what the per-IP limit is for.

Per-IP limit: an in-memory sliding window. Each serverless instance keeps its
own window, so this is a basic brake, not a hard guarantee. The hard ceiling
is the free-tier quota itself: with no billing account attached, abuse can
only exhaust quota, never cost money.

IPs are held in memory only for the window and are never logged.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from collections import defaultdict, deque

SESSION_MAX_QUESTIONS = 20
SESSION_TTL_S = 24 * 3600
IP_LIMITS = ((60, 8), (3600, 60))  # (window seconds, max requests)


# --- Session token ----------------------------------------------------------

# Used when SESSION_SECRET isn't set (local runs). Tokens then don't survive a
# restart, and on Vercel they wouldn't survive across instances, so production
# must set SESSION_SECRET.
_DEV_SECRET = secrets.token_bytes(32)


def _secret() -> bytes:
    secret = os.environ.get("SESSION_SECRET")
    return secret.encode() if secret else _DEV_SECRET


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(payload: dict) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    mac = _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{mac}"


def read_session(token) -> int:
    """Questions already asked in this session (0 for a missing, invalid or
    expired token)."""
    if not isinstance(token, str) or "." not in token or len(token) > 512:
        return 0
    body, mac = token.rsplit(".", 1)
    expected = _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(mac, expected):
        return 0
    try:
        payload = json.loads(_unb64(body))
        if time.time() - payload["iat"] > SESSION_TTL_S:
            return 0
        return int(payload["n"])
    except (ValueError, KeyError, TypeError):
        return 0


def next_session(count: int) -> str:
    return _sign({"n": count, "iat": int(time.time())})


# --- Per-IP sliding window --------------------------------------------------

_hits: dict[str, deque] = defaultdict(deque)
_lock = threading.Lock()


def allow_ip(ip: str, now: float | None = None) -> bool:
    now = now or time.time()
    longest = max(w for w, _ in IP_LIMITS)
    with _lock:
        q = _hits[ip]
        while q and now - q[0] > longest:
            q.popleft()
        for window, limit in IP_LIMITS:
            if sum(1 for t in q if now - t <= window) >= limit:
                return False
        q.append(now)
        # Bound memory on a long-lived instance.
        if len(_hits) > 10_000:
            for k in [k for k, v in _hits.items() if not v or now - v[-1] > longest]:
                del _hits[k]
        return True
