# Governed portfolio assistant

A chatbot that answers hiring managers' questions about one person's professional work, only from a curated corpus, with a citation on every answer. The repository is meant to be read as much as the bot is used: it shows how a small LLM application can be governed end to end, with a versioned system prompt, a 42-test hallucination battery, output guardrails, and no content logging.

**This is the public showcase.** It runs on a fictional sample profile, **Maya Ellison**, whose resume, career interests, posts and figures are all invented. The production assistant built from the same code answers questions about its author, Pranay Vasa: **[ask-pranay.vercel.app](https://ask-pranay.vercel.app)**. Its corpus and live test reports stay private.

## Why it exists

LLMs can be **confident but wrong**. A hallucinated answer doesn't look like an error: it reads as fluent and authoritative, and in credit or compliance work, plausible is not the same as accurate.

A bot that talks about a person to people deciding whether to hire them is a small, concrete version of that risk. One invented figure, one overstated role or one opinion the person never held would do more damage than having no bot at all. So the design goal is not "answer everything". It is **never say anything the sources don't support**, and make that checkable.

## Architecture

```mermaid
flowchart LR
    U[Visitor] --> P["Static page<br/>web/ (no frameworks)"]
    P -->|"POST /api/chat"| F["Serverless function<br/>api/chat.py (Vercel)"]
    F --> G1{"Input limits<br/>500 chars · 20/session · per-IP"}
    G1 --> E["Embed question<br/>gemini-embedding-2"]
    E -->|fails| B["BM25 keyword search"]
    E --> R["Top 7 chunks by cosine<br/>index/chunks.json"]
    B --> R
    R -->|"best score < 0.58"| NC["'Not covered' reply<br/>(no model call)"]
    R --> M1["Main Gemini model<br/>temperature 0"]
    M1 -->|overloaded| M2["Backup Gemini model"]
    M2 -->|overloaded| BUSY["'Busy' reply + contact link<br/>(fails closed)"]
    M1 --> O["Output guardrails<br/>strip links/HTML · canonical sources"]
    M2 --> O
    O --> P
```

### Design decisions

- **No vector database.** The corpus is small. `scripts/build_index.py` splits each markdown file by heading into chunks of about 150–300 tokens, keeps the heading trail with every chunk (so a bullet under one employer stays attributed to that employer), embeds them and writes `index/chunks.json`. At query time a linear cosine scan takes about a millisecond. A vector database would add a service, a bill and a failure mode for no gain at this size.
- **Chunks never cross employers or topics.** Small sections are merged only within one top-level section. Mixing two employers' bullets in one chunk invites exactly the misattribution the battery tests for.
- **Skip the model when retrieval finds nothing.** If the best chunk scores below 0.58 cosine similarity, the bot returns a standard "not covered" reply without calling the model. The threshold was calibrated on real questions: off-topic ones scored 0.51–0.56, questions about the person 0.60 or higher.
- **Fail closed.** If both Gemini models are overloaded, the bot says it's busy and points to the contact link. It never hands the question to a weaker model: an early version fell back to a third-party model, and every embellished or rule-breaking answer in four live test runs came from that fallback, so it was removed.
- **Plain HTTP, no SDKs.** The function calls the Gemini REST API directly with `requests`, which keeps the bundle small and cold starts fast. All calls share a 40-second deadline, under the function's 45-second limit.
- **Short memory.** Only the last 4 turns are sent with each question.
- **One persona file.** [`persona.json`](persona.json) names who the assistant is about (name, contact link, pronouns, page text, suggested questions). The Python code reads it, and `scripts/render_web.py` renders the static page from it.

## Governance

| Control | Where |
|---|---|
| **Versioned system prompt.** Every change has a dated change-log row with its reason, usually a failed test. The change log is stripped before the prompt is sent. | [`governance/system_prompt.md`](governance/system_prompt.md) |
| **Curated, reviewed corpus.** Hand-edited markdown with version and review date in the front matter. Nothing is scraped. | [`corpus/`](corpus/) |
| **Grounding rules.** Answer only from retrieved context; state figures exactly; correct wrong premises with the documented figure; never present awaited results as achieved; never expand acronyms beyond the source; never present target roles as past work. | System prompt, *Accuracy* |
| **Judgment questions.** "Is she a good communicator?" or "What would her manager say?" get the documented evidence first and one sentence on what isn't documented: no yes/no verdicts, no ratings, no bare refusals. | System prompt, *Judgment, preference and hypothetical questions* |
| **Refusal scope.** Salary, notice period, start dates, job-search status, plans with the employer, personal details, opinions on employers, proprietary methods and fit verdicts are declined politely with a pointer to the contact link. | System prompt, *Out of scope*, *Career interests* |
| **Prompt-injection handling.** Retrieved text is wrapped as data, not instructions. Requests to ignore the rules, change role or reveal the prompt are declined. | System prompt, *Instruction integrity*; [`core/prompt.py`](core/prompt.py) |
| **Output guardrails.** HTML and every link except the persona's contact link are stripped; the Sources line is rebuilt from real corpus source names (invented sources are dropped); pure refusals carry no Sources line. | [`core/guardrails.py`](core/guardrails.py) |
| **No content logging.** One log line per request: status code, outcome, model used, retrieval method and latency. Never the question, the answer, the history or the IP. | [`api/chat.py`](api/chat.py) |
| **Abuse limits.** 500 characters per question; 20 questions per session (an HMAC-signed counter, no server storage); a per-IP limit per minute and per hour. Use a key on prepaid credits with auto-reload off, or the free tier, so abuse can't run up a bill. | [`core/limits.py`](core/limits.py) |
| **Browser hardening.** Strict Content-Security-Policy, no third-party scripts or fonts, model output rendered as text nodes, never as HTML. | [`vercel.json`](vercel.json), [`web/app.js`](web/app.js) |

## Testing

**The battery.** [`governance/test_battery.md`](governance/test_battery.md) holds 42 tests adapted from a credit-underwriting hallucination battery: missing information, leading questions with wrong numbers, unsupported inference, attribution, out-of-scope requests, prompt injection and extraction, role override, opinion stretching, pending results, career-interest boundaries, judgment and hypothetical questions, confidentiality and consistency. **A single fabricated fact or leaked personal detail is a launch blocker.**

[`scripts/run_tests.py`](scripts/run_tests.py) asks every question against a deployment, applies the mechanical checks in [`governance/battery_checks.py`](governance/battery_checks.py) (test 3 must contain the documented AUC, test 22 must not contain a figure deliberately kept out of the corpus, test 36 must not open with "Yes", and so on), and writes every answer to a dated report for manual review.

**Odd-ball reviews.** Beyond the battery, the production assistant was probed with 50 unusual recruiter-style questions ("What would his manager say about him?", "Does he have an MBA?", "Has he ever failed at something?"), two runs each, to find answers the corpus supports but the bot declined or got wrong. Twenty failed. Prompt v1.7 and three small corpus fixes came out of it, and tests 31–42 were added:

| Model | Good on both runs, prompt v1.6 | Good on both runs, prompt v1.7 |
|---|---|---|
| Flash-Lite | 0 / 20 | 18 / 20 |
| 3.6-flash | 9 / 20 | 17 / 20 |

**Offline tests.** `python -m pytest tests/` runs the unit tests with no network: input validation, link and HTML stripping, source normalisation, session tokens, rate limits, the fallback chain under worst-case timeouts, and the persona rendering. The [Tests](.github/workflows/tests.yml) workflow runs them on every pull request.

## Run it yourself

You need Python 3.12 and a Gemini API key.

```bash
pip install -r requirements.txt pytest
cp .env.example .env          # then add GEMINI_API_KEY and SESSION_SECRET
python -m pytest tests/       # offline, no key needed
python scripts/dev_server.py  # http://127.0.0.1:8787
```

`index/chunks.json` is already built from the sample corpus. After editing `corpus/`, rebuild it with `python scripts/build_index.py` (`--dry-run --show` previews the chunks without calling the API). To try the battery locally, start the dev server and run `python scripts/run_tests.py http://127.0.0.1:8787`.

To deploy, import the repository into Vercel and set `GEMINI_API_KEY` and `SESSION_SECRET` as environment variables. `vercel.json` already sets the function bundle and the security headers.

### Make it about someone else

1. Edit [`persona.json`](persona.json), then run `python scripts/render_web.py` to re-render the page.
2. Replace the markdown in `corpus/`, keeping the front matter (`source`, `version`, `last_reviewed`; posts also need `title` and `date`).
3. Rewrite the person-specific parts of `governance/system_prompt.md` (employers, location, contact link, confidential topics) and keep the rules.
4. Rewrite the battery and `governance/battery_checks.py` around the new corpus's facts, including one figure you deliberately leave out (test 22).
5. Rebuild the index, run the offline tests and the battery, and recalibrate the 0.58 threshold in `core/config.py` with `python scripts/chat.py --debug` if off-topic questions start getting through.

## Known limitations

- **Availability.** If both Gemini models are overloaded, the bot replies "busy, try again" rather than answer badly.
- **Model consistency.** At temperature 0, LLMs are still not fully deterministic: the same question can get a slightly different answer between runs, which is why the reviews run every question at least twice.
- **Third-party processing.** Questions are sent to Google's Gemini API, as the page notice says.
- **Only as current as the corpus.** The bot knows nothing that isn't in `corpus/`.
- **Basic rate limiting.** The per-IP limit lives in each serverless instance's memory, so it is a brake rather than a guarantee.

## Repository layout

```
persona.json     who the assistant is about (name, contact link, page text)
corpus/          sample resume, career interests and 3 LinkedIn posts (fictional)
governance/      system prompt (versioned), test battery and its automated checks
index/           chunks.json, generated by scripts/build_index.py
core/            retrieval, prompt loading, Gemini client, guardrails, limits
api/chat.py      the serverless endpoint
web/             the static chat page (index.html rendered from persona.json)
scripts/         build_index, render_web, chat (CLI), dev_server, run_tests
tests/           offline unit tests
.github/         workflow: run the offline tests on pull requests
```

## How it was built

Pranay Vasa designed the system and wrote its governance: the system prompt and its rules, the test battery, the launch criteria, and every decision about what the bot may and may not say. The code was built with [Claude Code](https://claude.com/claude-code) from that design and governance spec, in stages, with each stage tested before the next. This public copy is generated from the private production repository by an export script that swaps in the sample profile and scans the result for personal data before anything is published.

Connect with Pranay on [LinkedIn](https://www.linkedin.com/in/pranayvasa).
