"""Input validation and output sanitising.

Input: question length and shape, history shape.
Output: no HTML, no links except the persona's LinkedIn, and a clean final
"Sources:" line naming only real corpus sources.
"""

import re
from functools import lru_cache

from core import config, retrieval

MAX_QUESTION_CHARS = 500
MAX_ANSWER_CHARS_IN_HISTORY = 2500

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HTML_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]*)[^)]*\)")
_URL_RE = re.compile(r"(?:https?://|www\.)[^\s)>\]]+", re.IGNORECASE)
_SOURCES_RE = re.compile(r"^[\s*_#>-]*sources?[\s*_]*:[\s*_]*(.*?)[\s*_.]*$", re.IGNORECASE)
# "…end of the answer. Sources: Resume" on the same line as the last sentence.
_INLINE_SOURCES_RE = re.compile(r"(?<=[.!?)\]])\s+[*_]*sources?[*_]*\s*:[\s*_]*(.*?)[\s*_.]*$", re.IGNORECASE)
LINKEDIN_PATH = config.LINKEDIN_PATH


class InvalidInput(ValueError):
    pass


# --- Input ------------------------------------------------------------------

def clean_question(raw) -> str:
    if not isinstance(raw, str):
        raise InvalidInput("Please type a question.")
    q = _CONTROL_RE.sub("", raw).strip()
    if not q:
        raise InvalidInput("Please type a question.")
    if len(q) > MAX_QUESTION_CHARS:
        raise InvalidInput(f"Please keep questions under {MAX_QUESTION_CHARS} characters.")
    return q


def clean_history(raw) -> list[dict]:
    """Keep the last MAX_HISTORY_TURNS well-formed user/assistant pairs.
    Anything malformed is dropped rather than rejected: history is a
    convenience, and the client controls it, so it is never trusted for more
    than conversational context."""
    if not isinstance(raw, list):
        return []
    pairs = []
    for i in range(0, len(raw) - 1, 2):
        u, a = raw[i], raw[i + 1]
        if not (isinstance(u, dict) and isinstance(a, dict)):
            continue
        if u.get("role") != "user" or a.get("role") != "assistant":
            continue
        uq, at = u.get("content"), a.get("content")
        if not (isinstance(uq, str) and isinstance(at, str)):
            continue
        pairs.append([
            {"role": "user", "content": _CONTROL_RE.sub("", uq)[:MAX_QUESTION_CHARS]},
            {"role": "assistant", "content": _CONTROL_RE.sub("", at)[:MAX_ANSWER_CHARS_IN_HISTORY]},
        ])
    return [m for pair in pairs[-config.MAX_HISTORY_TURNS :] for m in pair]


# --- Output -----------------------------------------------------------------

def _is_linkedin(url: str) -> bool:
    return LINKEDIN_PATH in url.lower()


def strip_links_and_html(text: str) -> str:
    text = _HTML_TAG_RE.sub("", text)
    text = _MD_IMAGE_RE.sub(r"\1", text)
    # [label](url) -> label, or the bare LinkedIn URL if that's the target.
    text = _MD_LINK_RE.sub(lambda m: config.LINKEDIN_URL if _is_linkedin(m.group(2)) else m.group(1), text)
    text = _URL_RE.sub(lambda m: m.group(0) if _is_linkedin(m.group(0)) else "", text)
    return text


@lru_cache(maxsize=1)
def _known_sources() -> list[tuple[str, list[str]]]:
    """(canonical label, lowercase strings that identify it in a Sources line)."""
    out, seen = [], set()
    for c in retrieval.load_index()["chunks"]:
        label = c["source_label"]
        if label in seen:
            continue
        seen.add(label)
        if c["source"] == "linkedin_post":
            title = label.split('"')[1]
            keys = [title.lower()]
        elif c["source"] == "ai_fluency_handbook":
            topic = label[label.find("(") + 1 : -1]
            keys = [topic.lower()] + ([topic.split(" — ")[0].lower()] if " — " in topic else [])
        elif c["source"] == "career_interests":
            keys = ["career interests", "career interest"]
        else:
            keys = ["resume", "résumé"]
        out.append((label, keys))
    return out


def canonical_sources(line: str) -> list[str]:
    """Map whatever the model wrote on its Sources line to canonical labels,
    in the order the model mentioned them. Unknown names are dropped."""
    low = line.lower()
    found = []
    for label, keys in _known_sources():
        positions = [low.find(k) for k in keys if k in low]
        if positions:
            found.append((min(positions), label))
    # "AI Fluency Handbook" with no recognisable topic -> generic handbook label.
    if "handbook" in low and not any("Handbook" in lbl for _, lbl in found):
        found.append((low.find("handbook"), "AI Fluency Handbook"))
    return [label for _, label in sorted(found)]


_REFUSAL_RE = re.compile(
    r"out of scope|isn't covered|is not covered|not covered in"
    r"|(?:can't|cannot) (?:help|provide|comply|fulfil|reproduce|share)"
    r"|best discussed with " + re.escape(config.PERSONA_SHORT)
    + r"|don't have that information|confidential to (?:his|her|their) employer",
    re.I,
)


def _is_pure_refusal(body: str) -> bool:
    """A decline or not-covered reply with no facts in it."""
    plain = body.replace("’", "'")  # models often write curly apostrophes
    without_link = re.sub(r"\S*" + re.escape(LINKEDIN_PATH) + r"\S*", "", plain, flags=re.I)
    return (
        bool(_REFUSAL_RE.search(plain))
        and not re.search(r"\d", without_link)  # a figure means it's answering something
        and len(body) < 400
    )


def finalise_answer(text: str, retrieved_labels: list[str]) -> tuple[str, list[str]]:
    """Sanitise model output and normalise its Sources line.
    Returns (answer text, list of canonical sources)."""
    text = strip_links_and_html(text).strip()
    lines = text.splitlines()

    sources: list[str] = []
    for i in range(len(lines) - 1, -1, -1):
        m = _SOURCES_RE.match(lines[i])
        if m:
            sources = canonical_sources(m.group(1))
            del lines[i:]  # drop the Sources line and anything after it
            break
    else:
        while lines and not lines[-1].strip():
            lines.pop()
        m = _INLINE_SOURCES_RE.search(lines[-1]) if lines else None
        if m:
            sources = canonical_sources(m.group(1))
            lines[-1] = lines[-1][: m.start()]
    body = "\n".join(lines).rstrip()

    # Every substantive answer ends with sources. If the model omitted them,
    # fall back to what was retrieved. Pure refusals (a LinkedIn redirect with
    # no facts in it) get none: "out of scope. Sources: Resume" reads oddly.
    if _is_pure_refusal(body):
        sources = []
    elif not sources:
        sources = retrieved_labels[:3]
    if sources:
        body += "\n\nSources: " + "; ".join(sources)
    return body, sources
