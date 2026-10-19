"""Central configuration: model names, limits and paths.

Model choices were checked against the providers' docs on 2026-10-16.
Change them here only; every script and the API read from this file.
"""

import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORPUS_DIR = ROOT / "corpus"
INDEX_PATH = ROOT / "index" / "chunks.json"
SYSTEM_PROMPT_PATH = ROOT / "governance" / "system_prompt.md"
PERSONA_PATH = ROOT / "persona.json"

# --- Persona ----------------------------------------------------------------
# Who the assistant is about. persona.json is the only place that names them;
# the static page is rendered from it by scripts/render_web.py.
PERSONA = json.loads(PERSONA_PATH.read_text())
PERSONA_SHORT = PERSONA["short_name"]
PRONOUNS = PERSONA["pronouns"]  # subject, object, possessive
LINKEDIN_URL = PERSONA["linkedin_url"]
# The URL without scheme or "www.", as it is matched in answers.
LINKEDIN_PATH = re.sub(r"^https?://(www\.)?", "", LINKEDIN_URL, flags=re.I).rstrip("/").lower()

# --- Models -----------------------------------------------------------------
# Generation runs on prepaid Gemini credits (paid tier, auto-reload off, so the
# prepaid balance is a hard spending cap). Checked 2026-10-16.
# Main model: gemini-3.6-flash, the model that followed the governance rules
# most consistently in testing; on the free tier it was capped at 20 requests
# a day, which is why the bot ran on Flash-Lite until credits were added.
# "minimal" thinking keeps thinking tokens, which count against the output
# cap, to a minimum.
#
# Temporarily swapped 2026-10-16: probes of real requests showed
# gemini-3.6-flash answering 1 of 6 (503 "high demand") and gemini-3.8-flash
# 0 of 6, while Flash-Lite answered most calls. So Flash-Lite is the main
# model and 3.6-flash the backup (it fails fast, so trying it is cheap).
# Swap back once 3.6-flash is answering reliably.
GEMINI_GEN_MODEL = "gemini-3.5-flash-lite"
# Backup, tried when the main model is overloaded or errors.
#
# The bot fails closed: if both Gemini models fail, it says it is busy rather
# than answer with a weaker model. A Groq fallback (gpt-oss-120b) was removed
# on 2026-10-16 after four live battery runs in which every embellished or
# rule-breaking answer came from it (e.g. tests 12, 16, 26).
# gemini-3.1-flash-lite was dropped as the backup the same day: it returned
# 503 "high demand" far more often than it answered.
GEMINI_BACKUP_MODEL = "gemini-3.6-flash"
GEMINI_THINKING_LEVEL = "minimal"
# Embeddings: stable. Truncated to 768 dims to keep chunks.json small.
GEMINI_EMBED_MODEL = "gemini-embedding-2"
EMBED_DIMS = 768
# gemini-embedding-2 takes no task_type; the task goes in the text instead.
EMBED_DOC_FORMAT = "title: {title} | text: {text}"
EMBED_QUERY_FORMAT = "task: search result | query: {text}"

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

# --- Generation -------------------------------------------------------------
TEMPERATURE = 0
# Visible answer target is ~400 tokens (the system prompt keeps answers short).
# The cap is higher because Gemini counts thinking tokens against it.
MAX_OUTPUT_TOKENS = 800
# Per-call timeouts. Worst case (embed + main model + backup) must stay under
# the 45 s Vercel function limit set in vercel.json.
EMBED_TIMEOUT_S = 6
# Measured 2026-10-16: gemini-3.6-flash answers in 1-2 s but about 1 call in
# 5 fails fast with 503 "high demand"; gemini-3.5-flash-lite fails less often
# but takes 5-11 s. So the main model gets one quick retry when it fails fast,
# and the backup gets the longer timeout.
GEMINI_TIMEOUT_S = 15  # main model (Flash-Lite, 5-11 s)
BACKUP_TIMEOUT_S = 15  # backup (3.6-flash: 1-2 s when it has capacity, 12 s seen)
FAST_FAIL_S = 3  # a failure quicker than this is worth retrying once
RETRY_PAUSE_S = 0.5
# All attempts share one deadline, counted from the start of the request.
# Main-model attempts leave BACKUP_RESERVE_S for the backup; no attempt starts
# with under MIN_ATTEMPT_S left.
GENERATION_DEADLINE_S = 40
BACKUP_RESERVE_S = 15
MIN_ATTEMPT_S = 3

# --- Retrieval --------------------------------------------------------------
CHUNK_MIN_TOKENS = 150
CHUNK_MAX_TOKENS = 300
TOP_K = 7  # broad questions ("which teams…") need facts from many sections
# Below this cosine similarity for the best chunk, the question is treated as
# not covered and the model is not called. Calibrate with `scripts/chat.py --debug`.
# Calibrated 2026-10-16: off-topic questions scored 0.51-0.56, questions about
# the person (including out-of-scope personal ones, which the model declines) >= 0.60.
MIN_SIMILARITY = 0.58
# Same idea for the BM25 fallback (score of the best chunk).
MIN_BM25_SCORE = 2.0

# --- Conversation -----------------------------------------------------------
MAX_HISTORY_TURNS = 4  # user+assistant pairs sent with each question



def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Minimal .env loader for local runs (Vercel injects env vars itself)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def gemini_key() -> str | None:
    return os.environ.get("GEMINI_API_KEY")

