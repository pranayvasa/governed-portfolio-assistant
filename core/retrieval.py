"""Retrieval over index/chunks.json: cosine similarity, with BM25 as fallback.

The corpus is ~45 chunks, so a linear scan in pure Python takes about a
millisecond. A vector database would add a service, a bill and a failure mode
for no gain at this size.
"""

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache

from core import config, llm

_WORD_RE = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")
_STOPWORDS = set(
    "a an the and or of to in on for with at by from is are was were be been his he him her she they their them "
    "what which who how did does do has have had that this it as about me tell".split()
)


@dataclass
class Hit:
    chunk: dict
    score: float


@dataclass
class Retrieval:
    hits: list[Hit]
    method: str  # "embedding" or "bm25"
    relevant: bool  # best score cleared the threshold


@lru_cache(maxsize=1)
def load_index() -> dict:
    index = json.loads(config.INDEX_PATH.read_text())
    if index["meta"]["embed_model"] != config.GEMINI_EMBED_MODEL:
        raise RuntimeError("index was built with a different embedding model; rebuild it")
    return index


def _tokenise(text: str) -> list[str]:
    return [w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS]


@lru_cache(maxsize=1)
def _bm25_stats() -> tuple[list[Counter], list[int], dict[str, float], float]:
    chunks = load_index()["chunks"]
    docs = [Counter(_tokenise(f"{c['section']} {c['text']}")) for c in chunks]
    lengths = [sum(d.values()) for d in docs]
    n = len(docs)
    df = Counter(term for d in docs for term in d)
    idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
    return docs, lengths, idf, sum(lengths) / n


def bm25(question: str, k: int = config.TOP_K) -> list[Hit]:
    docs, lengths, idf, avg_len = _bm25_stats()
    chunks = load_index()["chunks"]
    k1, b = 1.5, 0.75
    terms = _tokenise(question)
    scored = []
    for chunk, doc, length in zip(chunks, docs, lengths):
        s = 0.0
        for t in terms:
            if t in doc:
                tf = doc[t]
                s += idf[t] * tf * (k1 + 1) / (tf + k1 * (1 - b + b * length / avg_len))
        scored.append(Hit(chunk, s))
    scored.sort(key=lambda h: h.score, reverse=True)
    return scored[:k]


def cosine(query_vec: list[float], k: int = config.TOP_K) -> list[Hit]:
    # Stored vectors and the query vector are unit length, so dot = cosine.
    scored = [
        Hit(c, sum(a * b for a, b in zip(query_vec, c["embedding"])))
        for c in load_index()["chunks"]
    ]
    scored.sort(key=lambda h: h.score, reverse=True)
    return scored[:k]


def retrieve(question: str) -> Retrieval:
    """Embedding search, falling back to BM25 if the embedding call fails."""
    try:
        hits = cosine(llm.embed_query(question))
        return Retrieval(hits, "embedding", bool(hits) and hits[0].score >= config.MIN_SIMILARITY)
    except llm.LLMError:
        hits = bm25(question)
        return Retrieval(hits, "bm25", bool(hits) and hits[0].score >= config.MIN_BM25_SCORE)
