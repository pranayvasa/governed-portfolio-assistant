// Chat client for the portfolio assistant. No dependencies.
// Each question becomes a "turn": the question as a heading, source cards,
// the answer, and related follow-ups. The page must never show an error:
// every failure becomes a friendly message with the LinkedIn link.

(function () {
  "use strict";

  var API_URL = document.body.getAttribute("data-api") || "/api/chat";
  // Who the assistant is about: name, LinkedIn, pronouns and suggested
  // questions, from web/persona.js (rendered from persona.json).
  var PERSONA = window.PERSONA;
  var LINKEDIN = PERSONA.linkedin_url;
  var PRONOUNS = PERSONA.pronouns;
  var MAX_CHARS = 500;
  var MAX_HISTORY_MESSAGES = 8; // 4 turns; the server trims too
  var TIMEOUT_MS = 50000; // above the API's 45 s limit
  var FALLBACK =
    "Sorry, the assistant couldn't answer just now. Please try again in a moment, " +
    "or connect with " + PERSONA.short_name + " on LinkedIn: " + LINKEDIN;

  // Follow-ups offered under an answer, keyed by the kind of source it used.
  // Every one is answerable from the corpus.
  var RELATED = PERSONA.related;

  var $ = function (id) { return document.getElementById(id); };
  var body = document.body;
  var thread = $("thread");
  var form = $("ask");
  var input = $("question");
  var send = $("send");
  var count = $("count");
  var newThread = $("new-thread");

  var history = [];
  var asked = {};
  var busy = false;
  var locked = false;
  var session = load("session");

  // sessionStorage can throw (private mode, blocked storage); it's optional.
  function load(key) {
    try { return window.sessionStorage.getItem(key); } catch (e) { return null; }
  }
  function save(key, value) {
    try { window.sessionStorage.setItem(key, value); } catch (e) { /* optional */ }
  }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function icon(path) {
    var ns = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    path.split("|").forEach(function (d) {
      var p = document.createElementNS(ns, "path");
      p.setAttribute("d", d);
      svg.appendChild(p);
    });
    return svg;
  }
  var ICONS = {
    sources: "M4 6h16M4 12h16M4 18h10",
    answer: "M12 3l2.5 5.5L20 9l-4 4 1 6-5-3-5 3 1-6-4-4 5.5-.5z",
    related: "M4 12h12M12 6l6 6-6 6",
    arrow: "M5 12h14M13 6l6 6-6 6",
    copy: "M9 9h10v10H9z|M5 15V5h10",
    check: "M5 12l5 5 9-10",
  };

  // --- Rendering (DOM only, never innerHTML with model text) --------------

  var LINKEDIN_PATH = LINKEDIN.replace(/^https?:\/\/(www\.)?/i, "").replace(/\/$/, "");
  var INLINE_RE = new RegExp(
    "((?:https?:\\/\\/)?(?:www\\.)?" + LINKEDIN_PATH.replace(/[.*+?^${}()|[\]\\\/]/g, "\\$&") + "\\/?)|\\*\\*([^*]+)\\*\\*",
    "gi"
  );
  var LIST_RE = /^\s*([*\-•]|\d+\.)\s+/;

  function appendInline(parent, text) {
    var last = 0, m;
    INLINE_RE.lastIndex = 0;
    while ((m = INLINE_RE.exec(text))) {
      if (m.index > last) parent.appendChild(document.createTextNode(text.slice(last, m.index)));
      if (m[1]) {
        var a = el("a", null, m[1].replace(/\/$/, ""));
        a.href = LINKEDIN;
        a.target = "_blank";
        a.rel = "noopener";
        parent.appendChild(a);
      } else {
        parent.appendChild(el("strong", null, m[2]));
      }
      last = INLINE_RE.lastIndex;
    }
    if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
  }

  function renderAnswer(container, text) {
    text.split(/\n\s*\n/).forEach(function (block) {
      var lines = block.split("\n").filter(function (l) { return l.trim(); });
      if (!lines.length) return;
      // A block may open with a sentence and continue as a list.
      var lead = [];
      while (lines.length && !LIST_RE.test(lines[0])) lead.push(lines.shift());
      if (lead.length) {
        var p = el("p");
        lead.forEach(function (l, i) {
          if (i) p.appendChild(el("br"));
          appendInline(p, l);
        });
        container.appendChild(p);
      }
      if (lines.length) {
        var ordered = /^\s*\d+\./.test(lines[0]);
        var list = el(ordered ? "ol" : "ul");
        lines.forEach(function (l) {
          var li = el("li");
          appendInline(li, l.replace(LIST_RE, ""));
          list.appendChild(li);
        });
        container.appendChild(list);
      }
    });
  }

  function splitSources(text) {
    var m = text.match(/\n\s*Sources:\s*(.+)\s*$/);
    return m ? text.slice(0, m.index).trim() : text.trim();
  }

  function describeSource(label) {
    var m = label.match(/^LinkedIn post: "(.+)" \((.+)\)$/);
    if (m) return { kind: "post", kicker: "LinkedIn post · " + m[2], title: m[1] };
    m = label.match(/^AI Fluency Handbook \((.+)\)$/);
    if (m) {
      var topic = m[1].replace(/^Topic \d+ — /, "");
      return { kind: "handbook", kicker: "AI Fluency Handbook", title: topic === "overview" ? "Curriculum overview" : topic };
    }
    if (label === "AI Fluency Handbook") return { kind: "handbook", kicker: "AI Fluency Handbook", title: "Self-study notes" };
    if (label === "Career interests") return { kind: "career", kicker: "Career interests", title: "Roles " + PRONOUNS.subject + (PRONOUNS.subject === "they" ? "'re" : "'s") + " interested in" };
    return { kind: "resume", kicker: "Resume", title: "Professional background" };
  }

  function sectionLabel(text, iconKey) {
    var h = el("h3", "label");
    h.appendChild(icon(ICONS[iconKey]));
    h.appendChild(document.createTextNode(text));
    return h;
  }

  function renderSources(turn, sources) {
    var section = el("section", "sources");
    section.appendChild(sectionLabel("Sources", "sources"));
    var cards = el("div", "cards");
    sources.forEach(function (label, i) {
      var s = describeSource(label);
      var card = el("div", "card");
      var kicker = el("div", "card-kicker");
      kicker.appendChild(el("span", "n", String(i + 1)));
      kicker.appendChild(document.createTextNode(s.kicker));
      card.appendChild(kicker);
      card.appendChild(el("div", "card-title", s.title));
      cards.appendChild(card);
    });
    section.appendChild(cards);
    turn.appendChild(section);
  }

  function pickRelated(sources) {
    var kinds = sources.map(function (l) { return describeSource(l).kind; });
    if (!kinds.length) kinds = ["resume", "career"];
    // Answers drawn only from the resume also suggest the career questions.
    if (kinds.indexOf("career") < 0) kinds.push("career");
    var seen = {}, out = [];
    for (var round = 0; out.length < 3 && round < 6; round++) {
      kinds.forEach(function (k) {
        var pool = RELATED[k] || [];
        for (var i = 0; i < pool.length && out.length < 3; i++) {
          var q = pool[(i + round) % pool.length];
          if (!asked[q.toLowerCase()] && !seen[q]) { seen[q] = 1; out.push(q); break; }
        }
      });
    }
    return out.slice(0, 3);
  }

  function renderRelated(turn, sources) {
    var qs = pickRelated(sources);
    if (!qs.length) return;
    var section = el("section", "related");
    section.appendChild(sectionLabel("Related", "related"));
    var list = el("ul", "related-list");
    qs.forEach(function (q) {
      var li = el("li");
      var btn = el("button", "related-btn");
      btn.type = "button";
      btn.appendChild(el("span", null, q));
      btn.appendChild(icon(ICONS.arrow));
      btn.addEventListener("click", function () { ask(q); });
      li.appendChild(btn);
      list.appendChild(li);
    });
    section.appendChild(list);
    turn.appendChild(section);
  }

  function renderTools(turn, text) {
    if (!navigator.clipboard) return;
    var tools = el("div", "tools");
    var copy = el("button", "tool");
    copy.type = "button";
    copy.appendChild(icon(ICONS.copy));
    copy.appendChild(el("span", null, "Copy"));
    copy.addEventListener("click", function () {
      navigator.clipboard.writeText(text).then(function () {
        copy.replaceChildren(icon(ICONS.check), el("span", null, "Copied"));
        setTimeout(function () { copy.replaceChildren(icon(ICONS.copy), el("span", null, "Copy")); }, 1600);
      }, function () { /* clipboard blocked: nothing to do */ });
    });
    tools.appendChild(copy);
    turn.appendChild(tools);
  }

  function startTurn(question) {
    var turn = el("article", "turn");
    turn.appendChild(el("h2", "q", question));
    var skeleton = el("div", "skeleton");
    skeleton.setAttribute("aria-hidden", "true");
    skeleton.append(el("span"), el("span"), el("span"));
    var thinking = el("div", "thinking");
    thinking.append(el("span", "orb"), el("span", null, "Searching " + PRONOUNS.possessive + " resume and writing…"));
    turn.append(skeleton, thinking);
    thread.appendChild(turn);
    turn.scrollIntoView({ behavior: "smooth", block: "start" });
    return {
      finish: function (data) {
        skeleton.remove();
        thinking.remove();
        var text = splitSources(data.answer);
        var sources = Array.isArray(data.sources) ? data.sources : [];
        if (sources.length) renderSources(turn, sources);
        var answer = el("section", "answer-wrap");
        answer.appendChild(sectionLabel("Answer", "answer"));
        var content = el("div", "answer");
        renderAnswer(content, text);
        answer.appendChild(content);
        turn.appendChild(answer);
        if (data.status === "answered" || data.status === "not_covered") {
          renderTools(turn, text);
          renderRelated(turn, sources);
        }
        // Keep the question at the top while its answer is read.
        turn.scrollIntoView({ behavior: "smooth", block: "start" });
      },
    };
  }

  // --- Network ------------------------------------------------------------

  function post(question) {
    var controller = "AbortController" in window ? new AbortController() : null;
    var timer = controller ? setTimeout(function () { controller.abort(); }, TIMEOUT_MS) : null;
    return fetch(API_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: question, history: history, session: session }),
      signal: controller ? controller.signal : undefined,
    })
      .then(function (res) { return res.json().catch(function () { return null; }); })
      .catch(function () { return null; })
      .then(function (data) {
        if (timer) clearTimeout(timer);
        if (!data || typeof data.answer !== "string") return { answer: FALLBACK, status: "unavailable" };
        return data;
      });
  }

  // --- Flow ---------------------------------------------------------------

  function ask(question) {
    question = (question || "").trim().slice(0, MAX_CHARS);
    if (!question || busy || locked) return;
    busy = true;
    asked[question.toLowerCase()] = 1;
    body.classList.remove("is-landing");
    body.classList.add("is-thread");
    newThread.hidden = false;
    input.value = "";
    sync();

    var turn = startTurn(question);
    post(question).then(function (data) {
      turn.finish(data);
      if (data.session) { session = data.session; save("session", session); }
      if (data.status === "answered") {
        history.push({ role: "user", content: question }, { role: "assistant", content: data.answer });
        history = history.slice(-MAX_HISTORY_MESSAGES);
      }
      if (data.status === "session_limit") {
        locked = true;
        input.disabled = true;
        input.placeholder = "Question limit reached for this session";
      }
      busy = false;
      sync();
      if (!locked) input.focus({ preventScroll: true });
    });
  }

  function reset() {
    if (busy) return;
    history = [];
    asked = {};
    thread.replaceChildren();
    body.classList.remove("is-thread");
    body.classList.add("is-landing");
    newThread.hidden = true;
    window.scrollTo({ top: 0 });
    input.focus();
  }

  function sync() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 180) + "px";
    var n = input.value.length;
    count.textContent = n + "/" + MAX_CHARS;
    count.classList.toggle("over", n >= MAX_CHARS);
    send.disabled = busy || locked || !input.value.trim();
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    ask(input.value);
  });
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      ask(input.value);
    }
  });
  input.addEventListener("input", sync);
  newThread.addEventListener("click", reset);
  $("home").addEventListener("click", reset);
  Array.prototype.forEach.call(document.querySelectorAll(".starter"), function (btn) {
    btn.addEventListener("click", function () { ask(btn.getAttribute("data-q")); });
  });
  sync();
})();
