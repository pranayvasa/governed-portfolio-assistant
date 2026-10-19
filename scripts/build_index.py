"""Build index/chunks.json from corpus/.

Splits each markdown file by heading into chunks of roughly 150-300 tokens,
keeps the source and heading path with every chunk, embeds each chunk with
Gemini, and writes the result to index/chunks.json.

    python scripts/build_index.py            # chunk + embed + write
    python scripts/build_index.py --dry-run  # chunk only, print a report
"""

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import config, llm  # noqa: E402

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def approx_tokens(text: str) -> int:
    # ~1.33 tokens per English word is close enough for sizing chunks.
    return round(len(text.split()) * 1.33)


def parse_frontmatter(raw: str) -> tuple[dict, str]:
    if not raw.startswith("---"):
        return {}, raw
    _, fm, body = raw.split("---", 2)
    meta = {}
    for line in fm.strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip("'\"")
    return meta, body


def source_label(meta: dict, section_path: list[str]) -> str:
    """The citation name the model is asked to use (see system prompt)."""
    src = meta.get("source")
    if src == "linkedin_post":
        return f'LinkedIn post: "{meta.get("title")}" ({meta.get("date")})'
    if src == "ai_fluency_handbook":
        topic = section_path[1] if len(section_path) > 1 else "overview"
        return f"AI Fluency Handbook ({topic})"
    if src == "career_interests":
        return "Career interests"
    return "Resume"


def split_sections(body: str) -> list[tuple[list[str], str]]:
    """Return (heading_path, text) for every heading's own text."""
    sections, path, buf = [], [], []

    def flush():
        text = "\n".join(buf).strip()
        if text:
            sections.append((list(path), text))
        buf.clear()

    for line in body.splitlines():
        m = HEADING_RE.match(line)
        if m:
            flush()
            level = len(m.group(1))
            parent = path[: level - 1]
            parent += [""] * (level - 1 - len(parent))  # tolerate skipped levels
            path[:] = parent + [m.group(2).strip()]
        else:
            buf.append(line)
    flush()
    return [([p for p in hp if p], t) for hp, t in sections]


def split_blocks(text: str) -> list[str]:
    """Paragraphs and individual top-level bullets: the units we never split."""
    blocks = []
    for para in re.split(r"\n\s*\n", text):
        lines = para.strip().splitlines()
        current: list[str] = []
        for line in lines:
            if line.startswith(("- ", "* ")) and current:
                blocks.append("\n".join(current))
                current = []
            current.append(line)
        if current:
            blocks.append("\n".join(current))
    return [b for b in blocks if b.strip()]


def pack(blocks: list[str]) -> list[str]:
    """Pack blocks into chunks of at most CHUNK_MAX_TOKENS, balanced in size
    so a long section doesn't leave a tiny tail chunk."""
    total = sum(approx_tokens(b) for b in blocks)
    n_chunks = -(-total // config.CHUNK_MAX_TOKENS)  # ceil
    target = total / max(n_chunks, 1)
    chunks, current, size = [], [], 0
    for block in blocks:
        t = approx_tokens(block)
        over_max = size + t > config.CHUNK_MAX_TOKENS
        # Close the chunk at the target size if the block would push past it
        # by more than half its own length.
        past_target = size + t / 2 > target and len(chunks) < n_chunks - 1
        if current and (over_max or past_target):
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(block)
        size += t
    if current:
        chunks.append("\n".join(current))
    return chunks


def chunk_file(path: Path) -> list[dict]:
    meta, body = parse_frontmatter(path.read_text())
    pieces = []  # (heading_path, text)
    for heading_path, text in split_sections(body):
        for piece in pack(split_blocks(text)):
            pieces.append((heading_path, piece))

    # Merge undersized pieces with a neighbour while the result stays within
    # the max, but never across top-level sections (employers, handbook
    # topics): a chunk mixing bullets from two employers invites misattribution, and
    # a chunk spanning two topics would be cited under the wrong one.
    # Headings stay in the text so a merged chunk says where each part came from.
    def top(hp: list[str]) -> list[str]:
        return hp[:2]

    def size(parts) -> int:
        return sum(approx_tokens(t) for _, t in parts)

    merged: list[tuple[list[str], list[tuple[list[str], str]]]] = []
    for hp, text in pieces:
        if merged and top(merged[-1][0]) == top(hp):
            parts = merged[-1][1]
            small = size(parts) < config.CHUNK_MIN_TOKENS or approx_tokens(text) < config.CHUNK_MIN_TOKENS
            if small and size(parts) + approx_tokens(text) <= config.CHUNK_MAX_TOKENS:
                parts.append((hp, text))
                continue
        merged.append((hp, [(hp, text)]))

    chunks = []
    for i, (first_hp, parts) in enumerate(merged, 1):
        doc_title = meta.get("title") or (first_hp[0] if first_hp else path.stem)
        lines, last_hp = [], None
        for hp, text in parts:
            if hp != last_hp:
                # Heading path shown inline, e.g. "Current role — Employer ... > Project ...",
                # so facts under a sub-heading stay attributed to the right employer.
                trail = " > ".join(hp[1:]) if len(hp) > 1 else (hp[0] if hp else doc_title)
                lines.append(f"[{trail}]")
                last_hp = hp
            lines.append(text)
        body_text = "\n".join(lines)
        sections = []
        for hp, _ in parts:
            name = " > ".join(hp[1:]) or (hp[0] if hp else doc_title)
            if name not in sections:
                sections.append(name)
        chunks.append(
            {
                "id": f"{path.stem}-{i:02d}",
                "file": str(path.relative_to(config.ROOT)),
                "source": meta.get("source", path.stem),
                "source_label": source_label(meta, first_hp),
                "section": "; ".join(sections),
                "text": body_text,
                "tokens": approx_tokens(body_text),
            }
        )
    return chunks


def corpus_files() -> list[Path]:
    return sorted(
        [*config.CORPUS_DIR.glob("*.md"), *(config.CORPUS_DIR / "posts").glob("*.md")]
    )


def corpus_hash(files: list[Path]) -> str:
    h = hashlib.sha256()
    for f in files:
        h.update(str(f.relative_to(config.ROOT)).encode())
        h.update(f.read_bytes())
    return h.hexdigest()[:16]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="chunk only; don't embed or write")
    parser.add_argument("--show", action="store_true", help="print every chunk's text")
    args = parser.parse_args()

    files = corpus_files()
    chunks = [c for f in files for c in chunk_file(f)]

    print(f"{len(files)} files -> {len(chunks)} chunks")
    for c in chunks:
        flag = "" if config.CHUNK_MIN_TOKENS <= c["tokens"] <= config.CHUNK_MAX_TOKENS else "  (outside 150-300)"
        print(f"  {c['id']:<36} {c['tokens']:>4} tok  {c['source_label'][:40]:<40} | {c['section'][:60]}{flag}")
        if args.show:
            print("    " + c["text"].replace("\n", "\n    ") + "\n")

    if args.dry_run:
        return

    config.load_dotenv()
    titles = [f"{c['source_label']} — {c['section']}" for c in chunks]
    texts = [config.EMBED_DOC_FORMAT.format(title=t, text=c["text"]) for t, c in zip(titles, chunks)]
    vectors = llm.embed(texts)
    for c, v in zip(chunks, vectors):
        c["embedding"] = [round(x, 5) for x in v]

    index = {
        "meta": {
            "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "corpus_hash": corpus_hash(files),
            "embed_model": config.GEMINI_EMBED_MODEL,
            "embed_dims": config.EMBED_DIMS,
            "doc_format": config.EMBED_DOC_FORMAT,
            "query_format": config.EMBED_QUERY_FORMAT,
            "chunk_count": len(chunks),
        },
        "chunks": chunks,
    }
    config.INDEX_PATH.parent.mkdir(exist_ok=True)
    config.INDEX_PATH.write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")))
    size_kb = config.INDEX_PATH.stat().st_size / 1024
    print(f"wrote {config.INDEX_PATH.relative_to(config.ROOT)} ({size_kb:.0f} KB)")


if __name__ == "__main__":
    main()
