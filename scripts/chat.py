"""Local command-line chat for checking retrieval and answers before deploying.

    python scripts/chat.py                 # chat
    python scripts/chat.py --debug         # also show retrieved chunks and scores
    python scripts/chat.py --bm25          # force keyword retrieval (no embedding call)
    python scripts/chat.py -q "question"   # one question, then exit

Reads GEMINI_API_KEY from the environment or a local .env.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import assistant, config, llm, prompt, retrieval  # noqa: E402


def show(ans: assistant.Answer, debug: bool) -> None:
    if debug:
        print(f"\n  retrieval={ans.retrieval_method} top={ans.top_score and round(ans.top_score, 3)} "
              f"status={ans.status} provider={ans.provider} latency={ans.latency_ms}ms")
        for c in ans.chunks:
            print(f"    {c['score']:.3f}  {c['id']:<36} {c['section'][:70]}")
    print(f"\n{ans.text}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--debug", action="store_true", help="show retrieved chunks and scores")
    parser.add_argument("--bm25", action="store_true", help="skip embeddings; use keyword retrieval")
    parser.add_argument("-q", "--question", help="ask one question and exit")
    args = parser.parse_args()

    config.load_dotenv()
    if args.bm25:
        def _no_embed(_q):
            raise llm.LLMError("gemini-embed", 0, "disabled by --bm25")
        llm.embed_query = _no_embed

    meta = retrieval.load_index()["meta"]
    print(f"index: {meta['chunk_count']} chunks, built {meta['built_at']} | "
          f"prompt v{prompt.prompt_version()} | gen {config.GEMINI_GEN_MODEL} -> {config.GEMINI_BACKUP_MODEL} (fails closed)")

    if args.question:
        show(assistant.answer(args.question), args.debug)
        return

    print(f"Ask about {config.PERSONA_SHORT}'s work. Ctrl-D or 'exit' to quit.")
    history: list[dict] = []
    while True:
        try:
            q = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if q.lower() in {"exit", "quit"}:
            break
        if not q:
            continue
        ans = assistant.answer(q, history)
        show(ans, args.debug)
        if ans.status == "answered":
            history += [{"role": "user", "content": q}, {"role": "assistant", "content": ans.text}]


if __name__ == "__main__":
    main()
