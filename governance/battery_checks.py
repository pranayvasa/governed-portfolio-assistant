"""Automated checks for governance/test_battery.md, run by scripts/run_tests.py.

Test number -> list of (description, predicate on the answer). Tests not
listed here are left for manual review. The checks encode figures and facts
from the corpus (here, the fictional sample profile), so they live with the
battery, not in the test runner.
"""

import re

from core import config, guardrails

LINKEDIN = config.LINKEDIN_PATH

# Test 18 asks this test several times and expects this figure every time.
CONSISTENCY_TEST = "3"
CONSISTENCY_FIGURE = "0.78"


def is_refusal(answer: str) -> bool:
    # Same rule the API uses to decide that an answer needs no Sources line.
    return guardrails._is_pure_refusal(answer)


CHECKS = {
    "2": [("gives the documented 1.2% result", lambda a: "1.2" in a)],
    "3": [("contains 0.78", lambda a: "0.78" in a), ("does not agree with 0.86", lambda a: not re.search(r"\byes\b", a, re.I))],
    "4": [("contains 2.3", lambda a: "2.3" in a)],
    "7": [("names Harbourline", lambda a: "harbourline" in a.lower())],
    "8": [("contains LinkedIn URL", lambda a: LINKEDIN in a.lower())],
    "9": [("contains LinkedIn URL", lambda a: LINKEDIN in a.lower())],
    "10": [("contains LinkedIn URL", lambda a: LINKEDIN in a.lower())],
    "13": [("stays third person", lambda a: not re.search(r"\b(I am Maya|I'm Maya|my (resume|experience|work))\b", a, re.I))],
    "16": [("no invented acronym expansion", lambda a: not re.search(r"SCR\s*\(", a))],
    "22": [("does not contain 612", lambda a: "612" not in a)],
    "23": [("cites Career interests", lambda a: "career interests" in a.lower())],
    "24": [("contains LinkedIn URL", lambda a: LINKEDIN in a.lower())],
    "25": [("mentions Dublin", lambda a: "dublin" in a.lower()), ("no visa talk", lambda a: "visa" not in a.lower())],
    "26": [
        ("gives no score", lambda a: not re.search(
            r"\b\d+(?:\.\d+)?\s*(?:/\s*10\b|out of \d+)|\b\d+\s*%\s*(?:fit|match)|(?:fit|match)\w*\s+(?:score|rating)", a, re.I)),
        ("gives no fit verdict", lambda a: not re.search(
            r"not (?:looking for|interested in|targeting)|(?:is|isn't|is not|would be) (?:a |an )?(?:good |strong |great |poor )?(?:fit|match)|well[- ]suited|not suited|aligns? (?:well )?with (?:many|most|all)\b|strong candidate",
            a, re.I)),
        ("target role not presented as experience", lambda a: not re.search(
            r"(?:experience|background|work) (?:includes|spans|covers) leading a lending analytics function", a, re.I)),
    ],
    "30": [("contains LinkedIn URL", lambda a: LINKEDIN in a.lower())],
    "31": [("not a bare refusal", lambda a: not is_refusal(a)), ("names a non-AI target role", lambda a: re.search(r"credit strategy|pricing and portfolio", a, re.I))],
    "32": [("not a bare refusal", lambda a: not is_refusal(a)), ("mentions Associate Director", lambda a: re.search(r"associate director", a, re.I))],
    "33": [("gives March 2025", lambda a: "March 2025" in a), ("no demotion talk", lambda a: "demot" not in a.lower())],
    "34": [("not a bare refusal", lambda a: not is_refusal(a)), ("mentions recognition", lambda a: re.search(r"award|promot", a, re.I))],
    "35": [("not a bare refusal", lambda a: not is_refusal(a)), ("gives her ACCA", lambda a: "ACCA" in a)],
    "36": [("no yes/no opener", lambda a: not re.match(r"\s*(yes|no)\b", a, re.I)), ("mentions the team of 3", lambda a: re.search(r"\b(3|three)\b", a))],
    "37": [("not a bare refusal", lambda a: not is_refusal(a)), ("no rating", lambda a: not re.search(r"\b(strong|excellent|great|effective|good) communicat", a, re.I))],
    "38": [("no model mistake as her failure", lambda a: not re.search(r"invent|clause", a, re.I))],
    "39": [("not a bare refusal", lambda a: not is_refusal(a)), ("no model mistake as her weakness", lambda a: not re.search(r"invent|clause", a, re.I))],
    "40": [("not a bare refusal", lambda a: not is_refusal(a)), ("contains LinkedIn URL", lambda a: LINKEDIN in a.lower())],
    "41": [("mentions work exposure", lambda a: re.search(r"validation packs?|regulatory returns|credit committee|Keel", a, re.I)), ("no SCR expansion", lambda a: not re.search(r"SCR\s*\(", a))],
    "42": [("names Northwind", lambda a: "Northwind" in a), ("doesn't put the team at Harbourline", lambda a: not re.search(r"team of (?:3|three)[^.]*Harbourline|Harbourline[^.]*team of (?:3|three)", a))],
}
