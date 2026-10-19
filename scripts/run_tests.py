"""Run the hallucination and safety battery against the live endpoint.

    python scripts/run_tests.py https://<your-app>.vercel.app
    python scripts/run_tests.py http://127.0.0.1:8787        # local dev server

Reads the questions from governance/test_battery.md, asks each one with a
fresh conversation, applies the automated checks below, and saves every
answer to governance/test_runs/<date>-<time>.md for manual review.
Tests without an automated check are marked REVIEW: the corpus owner judges
those by hand against the battery's pass/fail criteria and fills in the results log.

Paced to stay under the endpoint's per-IP rate limit (8/minute, 60/hour),
so a full run takes a few minutes and back-to-back runs may be throttled.
"""

import argparse
import importlib.util
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import guardrails  # noqa: E402
BATTERY = ROOT / "governance" / "test_battery.md"
OUT_DIR = ROOT / "governance" / "test_runs"
PACE_S = 10
BUSY_RETRY_S = 15
CONSISTENCY_RUNS = 3  # test 18: ask test 3 this many times

# Automated checks and the test 18 consistency figure live with the battery.
_spec = importlib.util.spec_from_file_location("battery_checks", ROOT / "governance" / "battery_checks.py")
battery_checks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(battery_checks)
CHECKS = battery_checks.CHECKS
FIGURE = battery_checks.CONSISTENCY_FIGURE


def load_battery() -> list[dict]:
    """Rows of the battery table: number, category, questions, pass/fail criteria."""
    tests = []
    for line in BATTERY.read_text().splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 5 or not cells[0].isdigit():
            continue
        num, category, question_cell, pass_if, fail_if = cells
        # A cell can hold several quoted questions: "How old is he?" / "What's his phone number?"
        questions = re.findall(r'"([^"]+)"', question_cell)
        tests.append({
            "num": num, "category": category, "questions": questions,
            "raw_question": question_cell, "pass_if": pass_if, "fail_if": fail_if,
        })
    return tests


def ask(base_url: str, question: str) -> dict:
    req = urllib.request.Request(
        base_url.rstrip("/") + "/api/chat",
        data=json.dumps({"question": question}).encode(),
        headers={"Content-Type": "application/json"},
    )
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            code, data = resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        code = exc.code
        try:
            data = json.load(exc)
        except ValueError:
            data = {}
    except (urllib.error.URLError, TimeoutError) as exc:
        code, data = 0, {"answer": f"(request failed: {type(exc).__name__})", "status": "error"}
    data["code"] = code
    data["latency_ms"] = round((time.monotonic() - start) * 1000)
    return data


def ask_with_retry(base_url: str, question: str) -> dict:
    """A "busy" reply means both Gemini models were overloaded (the bot fails
    closed). That is availability, not answer quality, so retry it once."""
    resp = ask(base_url, question)
    if resp.get("status") == "busy":
        time.sleep(BUSY_RETRY_S)
        resp = ask(base_url, question)
        resp["retried"] = True
    return resp


def run_checks(num: str, answer: str) -> list[tuple[str, bool]]:
    return [(desc, bool(pred(answer))) for desc, pred in CHECKS.get(num, [])]


def has_sources(resp: dict) -> bool:
    return bool(resp.get("sources")) or "Sources:" in resp.get("answer", "")


def is_refusal(answer: str) -> bool:
    # Same rule the API uses to decide that an answer needs no Sources line.
    return guardrails._is_pure_refusal(answer)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("base_url", help="deployment URL, e.g. https://example.vercel.app")
    parser.add_argument("--only", help="comma-separated test numbers to run, e.g. 3,4,22")
    args = parser.parse_args()

    tests = load_battery()
    if args.only:
        wanted = set(args.only.split(","))
        tests = [t for t in tests if t["num"] in wanted]

    results, first = [], True
    for t in tests:
        if not t["questions"]:
            continue  # 17 (citations) and 18 (consistency) are derived below
        for q in t["questions"]:
            if not first:
                time.sleep(PACE_S)
            first = False
            resp = ask_with_retry(args.base_url, q)
            checks = run_checks(t["num"], resp.get("answer", ""))
            results.append({"test": t, "question": q, "resp": resp, "checks": checks})
            mark = "FAIL" if any(not ok for _, ok in checks) else ("PASS" if checks else "REVIEW")
            retried = " (retried)" if resp.get("retried") else ""
            print(f"[{t['num']:>2}] {mark:<6} {resp['code']} {resp.get('status', '?'):<12} {resp['latency_ms']:>5}ms  {q[:60]}{retried}")

    # Test 18: consistency of test 3's figures across repeated runs.
    consistency = []
    if not args.only or "18" in args.only.split(","):
        test3 = next((t for t in load_battery() if t["num"] == battery_checks.CONSISTENCY_TEST), None)
        if test3:
            for _ in range(CONSISTENCY_RUNS):
                time.sleep(PACE_S)
                consistency.append(ask_with_retry(args.base_url, test3["questions"][0]))
            ok = all(FIGURE in r.get("answer", "") for r in consistency)
            print(f"[18] {'PASS' if ok else 'FAIL':<6} {FIGURE} present in {sum(FIGURE in r.get('answer', '') for r in consistency)}/{CONSISTENCY_RUNS} repeats of test {battery_checks.CONSISTENCY_TEST}")

    # Test 17: every substantive answer cites sources (refusals excepted).
    uncited = [
        r["test"]["num"] for r in results
        if r["resp"].get("status") == "answered" and not is_refusal(r["resp"]["answer"]) and not has_sources(r["resp"])
    ]
    print(f"[17] {'PASS' if not uncited else 'FAIL':<6} substantive answers without sources: {uncited or 'none'}")

    write_report(args.base_url, results, consistency, uncited)


def write_report(base_url: str, results: list, consistency: list, uncited: list) -> None:
    OUT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    path = OUT_DIR / f"{stamp}.md"
    auto_fail = sum(1 for r in results if any(not ok for _, ok in r["checks"]))
    lines = [
        f"# Test run {stamp}",
        "",
        f"- Endpoint: {base_url}",
        f"- Automated check failures: {auto_fail}",
        f"- Test 17 (citations): {'pass' if not uncited else 'fail, uncited: ' + ', '.join(uncited)}",
        "- Provider per answer is not exposed by the API; check the Vercel logs for the gemini/gemini-backup split. Status `busy` means both Gemini models were overloaded (the bot fails closed).",
        "",
        "Fill in **Verdict** for every test, then copy the totals into the results log in `test_battery.md`.",
        "",
    ]
    for r in results:
        t, resp = r["test"], r["resp"]
        lines += [
            f"## Test {t['num']} — {t['category']}",
            "",
            f"**Question:** {r['question']}",
            "",
            f"**Pass if:** {t['pass_if']}  ",
            f"**Fails if:** {t['fail_if']}",
            "",
            f"**HTTP {resp['code']} · {resp.get('status', '?')} · {resp['latency_ms']} ms{' · retried after busy' if resp.get('retried') else ''}**",
            "",
            "> " + resp.get("answer", "(no answer)").replace("\n", "\n> "),
            "",
        ]
        if r["checks"]:
            lines += [f"- Automated: {desc} — {'PASS' if ok else 'FAIL'}" for desc, ok in r["checks"]] + [""]
        lines += ["**Verdict:** ", ""]
    if consistency:
        lines += [f"## Test 18 — Consistency (test {battery_checks.CONSISTENCY_TEST} repeated)", ""]
        for i, resp in enumerate(consistency, 1):
            lines += [f"**Run {i}** (HTTP {resp['code']}, {FIGURE + (' present' if FIGURE in resp.get('answer', '') else ' MISSING')})", "",
                      "> " + resp.get("answer", "").replace("\n", "\n> "), ""]
        lines += ["**Verdict:** ", ""]
    path.write_text("\n".join(lines))
    print(f"\nsaved {path.relative_to(ROOT)}")


if __name__ == "__main__":
    sys.exit(main())
