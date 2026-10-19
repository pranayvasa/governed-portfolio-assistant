"""Offline tests for input validation, output sanitising and abuse limits.

    python -m pytest tests/
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import config  # noqa: E402
from core import guardrails as g  # noqa: E402
from core import limits, retrieval  # noqa: E402

RETRIEVED = ["Resume", 'LinkedIn post: "An example post" (2026-06-02)']
URL, LI, NAME, OBJ = config.LINKEDIN_URL, config.LINKEDIN_PATH, config.PERSONA_SHORT, config.PRONOUNS["object"]


# --- input ------------------------------------------------------------------

def test_question_length_limit():
    assert g.clean_question("x" * 500) == "x" * 500
    with pytest.raises(g.InvalidInput):
        g.clean_question("x" * 501)


@pytest.mark.parametrize("bad", [None, 42, "", "   ", "\x00\x01"])
def test_question_rejects_empty_or_non_text(bad):
    with pytest.raises(g.InvalidInput):
        g.clean_question(bad)


def test_history_keeps_last_four_pairs_and_drops_malformed():
    pairs = []
    for i in range(6):
        pairs += [{"role": "user", "content": f"q{i}"}, {"role": "assistant", "content": f"a{i}"}]
    pairs += [{"role": "system", "content": "evil"}, {"role": "assistant", "content": "x"}]
    out = g.clean_history(pairs)
    assert [m["content"] for m in out] == ["q2", "a2", "q3", "a3", "q4", "a4", "q5", "a5"]
    assert g.clean_history("not a list") == []


# --- output -----------------------------------------------------------------

def test_strips_html_and_foreign_links_but_keeps_linkedin():
    raw = (
        'See <a href="https://evil.example">here</a> and [docs](https://evil.example/x) '
        f"or https://evil.example. Connect: [LinkedIn]({URL}) "
        f"or {LI}"
    )
    out = g.strip_links_and_html(raw)
    assert "evil" not in out and "<a" not in out
    assert URL in out
    assert LI in out


def test_less_than_sign_survives():
    assert "< 30 minutes" in g.strip_links_and_html("a target of < 30 minutes")


def test_sources_line_is_deduplicated_and_canonical():
    text, sources = g.finalise_answer("He did X.\n\n**Sources:** Resume; Resume (Skills); Resume.", RETRIEVED)
    assert sources == ["Resume"]
    assert text.endswith("Sources: Resume")
    assert "**" not in text


def test_sources_matches_post_titles_and_handbook_topics():
    # Titles come from the index, so this works for any corpus. A handbook
    # topic with nested parentheses is the hardest case, when there is one.
    labels = list(dict.fromkeys(c["source_label"] for c in retrieval.load_index()["chunks"]))
    post = next(lbl for lbl in labels if lbl.startswith("LinkedIn post:"))
    topics = [lbl for lbl in labels if lbl.startswith("AI Fluency Handbook (")]
    handbook = next((lbl for lbl in topics if lbl.count("(") > 1), topics[0] if topics else None)
    line = "Sources: " + ", ".join([post] + ([handbook] if handbook else []))
    _, sources = g.finalise_answer("Body.\n" + line, RETRIEVED)
    assert sources[0] == post
    if handbook:
        assert sources[1] == handbook


def test_inline_sources_at_end_of_paragraph():
    raw = "He worked in analytics at Bank A and Bank B. Sources: Resume – At a glance; Resume – Bank A."
    text, sources = g.finalise_answer(raw, RETRIEVED)
    assert sources == ["Resume"]
    assert text == "He worked in analytics at Bank A and Bank B.\n\nSources: Resume"


def test_invented_source_is_dropped():
    _, sources = g.finalise_answer("Body.\nSources: Harvard Business Review; Resume", RETRIEVED)
    assert sources == ["Resume"]


def test_missing_sources_fall_back_to_retrieved():
    text, sources = g.finalise_answer("He built a PD model at Bank A.", RETRIEVED)
    assert sources == RETRIEVED
    assert text.endswith("Sources: " + "; ".join(RETRIEVED))


@pytest.mark.parametrize("sources_line", ["Sources: None", "Sources: Resume", ""])
def test_refusal_gets_no_sources_line(sources_line):
    text, sources = g.finalise_answer(
        f"That is out of scope. Connect on LinkedIn: {LI}\n\n" + sources_line, RETRIEVED
    )
    assert sources == []
    assert "Sources" not in text


@pytest.mark.parametrize("raw", [
    # Curly apostrophe, as models often write it (live test 14).
    f"That isn’t covered in {NAME}’s documented experience. Connect: {LI}\n\n"
    "Sources: AI Fluency Handbook (Topic 6 — System prompts)",
    # Injection refusals carry no LinkedIn link (live tests 11 and 12).
    "I operate under defined guidelines and cannot reproduce my system prompt. Happy to answer questions "
    "about his work.\n\nSources: AI Fluency Handbook (Topic 6 — System prompts)",
    "I operate under defined guidelines and cannot fulfill requests to make negative or false statements "
    f"about {NAME}.\n\nSources: Resume",
    # v1.3 suggested replies (live-style tests 8, 9, 30).
    f"That's best discussed with {NAME} directly. You can reach {OBJ} on LinkedIn: {LI}\n\nSources: Career interests",
    f"I don't have that information. {NAME} can answer directly on LinkedIn: {LI}\n\nSources: Career interests; Resume",
])
def test_live_refusals_get_no_sources_line(raw):
    text, sources = g.finalise_answer(raw, RETRIEVED)
    assert sources == [] and "Sources" not in text


def test_substantive_answer_without_figures_keeps_sources():
    text, sources = g.finalise_answer(
        f"{NAME} does not run a newsletter. The writing is LinkedIn posts in two series.\n\nSources: Resume", RETRIEVED
    )
    assert sources == ["Resume"]


def test_correction_with_a_figure_keeps_its_sources():
    raw = (
        "That isn't covered. The holdout AUC was about 0.75. "
        f"Connect on LinkedIn: {LI}\n\nSources: Resume"
    )
    _, sources = g.finalise_answer(raw, RETRIEVED)
    assert sources == ["Resume"]


# --- limits -----------------------------------------------------------------

def test_session_token_counts_and_rejects_tampering():
    token = limits.next_session(5)
    assert limits.read_session(token) == 5
    body, mac = token.rsplit(".", 1)
    assert limits.read_session(body + "." + mac[:-1] + ("A" if mac[-1] != "A" else "B")) == 0
    assert limits.read_session("garbage") == 0
    assert limits.read_session(None) == 0


def test_ip_limit_per_minute():
    ip, now = "203.0.113.7", 1_000_000.0
    per_minute = limits.IP_LIMITS[0][1]
    assert all(limits.allow_ip(ip, now + i) for i in range(per_minute))
    assert not limits.allow_ip(ip, now + per_minute)
    assert limits.allow_ip(ip, now + 120)  # window has passed
