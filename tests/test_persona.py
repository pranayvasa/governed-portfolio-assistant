"""Offline tests: persona.json is complete, the page is rendered from it, and
the serverless function bundles it."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import render_web  # noqa: E402
from core import config  # noqa: E402


def test_persona_has_every_field():
    p = config.PERSONA
    for key in ("name", "short_name", "initials", "linkedin_url", "page", "starters", "related"):
        assert p[key], key
    assert set(p["pronouns"]) == {"subject", "object", "possessive"}
    assert set(p["page"]) == {"title", "description", "tagline"}
    assert all(s["icon"] in render_web.ICONS for s in p["starters"])
    assert config.LINKEDIN_PATH in config.LINKEDIN_URL.lower()
    if "credit" in p:  # optional footer credit
        assert set(p["credit"]) == {"text", "url"}


def test_web_page_is_rendered_from_persona():
    for path, text in render_web.render(ROOT).items():
        assert path.read_text() == text, f"{path.name} is stale: run python scripts/render_web.py"


def test_function_bundle_includes_persona():
    vercel = json.loads((ROOT / "vercel.json").read_text())
    assert "persona.json" in vercel["functions"]["api/chat.py"]["includeFiles"]
