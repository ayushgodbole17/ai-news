"""Daily AI digest: fetch feeds, rank them against what I actually work on, email it.

    python digest.py                 # fetch, rank, write HTML, email
    python digest.py --no-email      # write the HTML only
    python digest.py --dry-run       # fetch and count, no LLM, no email
    python digest.py --self-check    # parser asserts, no network

Env: GEMINI_API_KEY (required), GEMINI_MODEL, DIGEST_TO, DIGEST_FROM, DIGEST_SMTP_PASS.
"""
import json, os, re, smtplib, sys, time, urllib.error, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from html import escape, unescape
from pathlib import Path

HERE = Path(__file__).parent
SEEN_FILE = HERE / "seen.json"
SENT_FILE = HERE / "last_sent.txt"   # which day a digest last went out, for --once-daily
WINDOW_HOURS = 72          # generous: covers weekends and a missed run; seen.json kills repeats
MAX_PER_SOURCE = 15
KEEP_SHIPS = 7             # releases, models, tools
KEEP_RESEARCH = 6          # papers and writeups

# The relevance lever. Edit this when the work changes -- everything else is plumbing.
PROFILE = """\
I build production AI systems, mostly voice and speech. Specifically:

- Real-time voice agents on Pipecat: Deepgram/Sarvam STT, Cartesia TTS, Gemini as the
  brain, state-machine flows, barge-in, turn-taking, latency budgets.
- Indian-language speech: Hindi, Tamil, Arabic. ASR and TTS quality for accented and
  code-mixed speech is a constant problem. Sarvam, IndicTrans and similar matter a lot.
- Call analytics: transcription, speaker diarization and role assignment (who is the
  agent vs the customer), script adherence scoring, LLM-as-judge rubrics on sales and
  support calls.
- Gemini-heavy pipelines (long context, thinking budgets, structured output), some
  Anthropic and Groq. Cost and latency per call are things I track.
- RAG with Qdrant, embeddings, document ingestion.
- AI governance: a proxy that sits in front of agents adding guardrails, policy
  enforcement, audit trails, jailbreak and PII detection.
- Agentic coding tooling: Claude Code skills, plugins, evals for agents.
- Some computer vision: YOLO for PCB defect detection.
- Stack is Python + FastAPI, Node/TypeScript, MongoDB, Docker on VMs.

I care about: new models and their real benchmarks, things that change speech or
voice-agent quality, evaluation methods, agent reliability, guardrails, and anything
that makes inference cheaper or faster. I do not care about: funding rounds, executive
hires, general AI punditry, doomer or hype takes, enterprise press releases.
"""

FEEDS = [
    ("Hugging Face",    "https://huggingface.co/blog/feed.xml"),
    ("Google Research", "https://research.google/blog/rss/"),
    ("DeepMind",        "https://deepmind.google/blog/rss.xml"),
    ("OpenAI",          "https://openai.com/news/rss.xml"),
    ("Google AI",       "https://blog.google/technology/ai/rss/"),
    ("Simon Willison",  "https://simonwillison.net/atom/everything/"),
    ("Import AI",       "https://jack-clark.net/feed/"),
    ("MarkTechPost",    "https://www.marktechpost.com/feed/"),
    ("MIT News",        "https://news.mit.edu/rss/topic/artificial-intelligence2"),
    ("NVIDIA",          "https://blogs.nvidia.com/blog/category/generative-ai/feed/"),
    ("Qwen",            "https://qwenlm.github.io/blog/index.xml"),
    ("EleutherAI",      "https://blog.eleuther.ai/index.xml"),
    ("Together AI",     "https://www.together.ai/blog/rss.xml"),
    # Tools and IDEs, not research.
    ("Cursor",          "https://cursor.com/changelog/rss.xml"),
    ("VS Code",         "https://code.visualstudio.com/feed.xml"),
    ("GitHub",          "https://github.blog/changelog/feed/"),
]

# Which repos to watch for releases is discovered from these topics, not listed here.
# Whatever is currently big and active in each area gets picked up on its own -- so a
# tool that did not exist last month arrives without this file being edited.
TOPICS = ["llm", "ai-agents", "speech-recognition", "text-to-speech", "voice-assistant",
          "rag", "vector-database", "llmops", "mlops", "code-generation"]
REPOS_PER_TOPIC = 6
REPO_CACHE = HERE / "repos.json"   # last good discovery, used if GitHub rate-limits us

# arXiv, scoped to what I work on rather than the whole of cs.AI.
ARXIV_QUERIES = [
    'abs:"speech recognition" OR abs:"speaker diarization" OR abs:"text to speech"',
    'abs:"voice agent" OR abs:"spoken dialogue" OR abs:"full-duplex"',
    'abs:"LLM agent" AND (abs:evaluation OR abs:reliability OR abs:benchmark)',
    'abs:"guardrail" OR abs:"jailbreak" OR abs:"prompt injection"',
    'abs:"retrieval augmented generation" OR abs:"LLM-as-a-judge"',
    'abs:"low-resource" AND (abs:Hindi OR abs:Tamil OR abs:Indic OR abs:multilingual)',
]

# Hacker News catches the vendors with no RSS (Anthropic, Meta, Mistral) and release news.
HN_QUERIES = ["anthropic", "claude", "llm", "gemini", "open source model",
              "speech recognition", "voice ai", "ai agents"]

UA = {"User-Agent": "Mozilla/5.0 (ai-news daily digest)"}


def get(url, timeout=25, headers=None):
    # Some release feeds run past 500KB and occasionally truncate mid-read. One retry
    # here covers every caller, rather than a guard in each fetcher.
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    for attempt in (1, 2):
        try:
            return urllib.request.urlopen(req, timeout=timeout).read()
        except urllib.error.HTTPError:
            raise          # 403/404 will not fix themselves, and retrying a 403 burns rate limit
        except Exception:  # truncated read, timeout, reset connection -- worth one more go
            if attempt == 2:
                raise


def strip_html(s, limit=400):
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()[:limit]


def parse_date(s):
    """RSS pubDate or Atom ISO timestamp -> aware datetime, or None."""
    if not s:
        return None
    for fn in (parsedate_to_datetime, datetime.fromisoformat):
        try:
            d = fn(s.strip().replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return None


def parse_feed(name, xml, limit=MAX_PER_SOURCE):
    """One parser for both RSS <item> and Atom <entry>."""
    root = ET.fromstring(xml)
    A = "{http://www.w3.org/2005/Atom}"
    items = root.findall(".//item") or root.findall(".//%sentry" % A)
    out = []
    for it in items[:limit]:
        title = it.findtext("title") or it.findtext(A + "title") or ""
        link = it.findtext("link") or ""
        if not link:  # Atom puts the URL in an attribute, not the element text
            el = it.find(A + "link[@rel='alternate']")
            if el is None:
                el = it.find(A + "link")
            link = el.get("href", "") if el is not None else ""
        summary = (it.findtext("description") or it.findtext(A + "summary")
                   or it.findtext(A + "content") or "")
        date = parse_date(it.findtext("pubDate") or it.findtext(A + "updated")
                          or it.findtext(A + "published"))
        if title and link:
            out.append({"source": name, "title": strip_html(title, 300),
                        "link": link.strip(), "summary": strip_html(summary), "date": date})
    return out


def fetch_feed(item):
    name, url = item
    try:
        return parse_feed(name, get(url))
    except Exception as e:
        print("  ! %s: %s: %s" % (name, type(e).__name__, e), file=sys.stderr)
        return []


def fetch_arxiv(query):
    url = ("http://export.arxiv.org/api/query?search_query=" + urllib.parse.quote(query)
           + "&sortBy=submittedDate&sortOrder=descending&max_results=12")
    try:
        return parse_feed("arXiv", get(url))
    except Exception as e:
        print("  ! arXiv: %s: %s" % (type(e).__name__, e), file=sys.stderr)
        return []


def fetch_hn(query):
    cutoff = int((datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)).timestamp())
    url = ("https://hn.algolia.com/api/v1/search?tags=story&hitsPerPage=10&query="
           + urllib.parse.quote(query)
           + "&numericFilters=created_at_i>%d,points>30" % cutoff)
    try:
        hits = json.loads(get(url))["hits"]
    except Exception as e:
        print("  ! HN %s: %s: %s" % (query, type(e).__name__, e), file=sys.stderr)
        return []
    return [{"source": "HN (%dpts)" % h["points"], "title": h.get("title") or "",
             "link": h.get("url") or "https://news.ycombinator.com/item?id=" + h["objectID"],
             "summary": "", "date": parse_date(h.get("created_at"))}
            for h in hits if h.get("title")]


def discover_repos():
    """Ask GitHub which repos matter in each topic right now, rather than hardcoding them.

    Refreshed weekly, not daily: which repos matter moves slowly, and unauthenticated
    GitHub search allows only 10 requests a minute -- roughly one topic sweep. Falls back
    to the last good result, because losing discovery would silently empty the whole
    Shipped half of the digest.
    """
    if REPO_CACHE.exists():
        age_days = (datetime.now().timestamp() - REPO_CACHE.stat().st_mtime) / 86400
        cached = json.loads(REPO_CACHE.read_text())
        if age_days < 7 and cached:
            return cached

    active_since = (datetime.now(timezone.utc) - timedelta(days=45)).strftime("%Y-%m-%d")
    headers = {"Accept": "application/vnd.github+json"}
    if os.environ.get("GITHUB_TOKEN"):     # Actions provides one; it raises the rate limit
        headers["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]

    def search(topic):
        url = ("https://api.github.com/search/repositories?q="
               + urllib.parse.quote("topic:%s stars:>1500 pushed:>%s" % (topic, active_since))
               + "&sort=stars&order=desc&per_page=%d" % REPOS_PER_TOPIC)
        try:
            return [i["full_name"] for i in json.loads(get(url, headers=headers))["items"]]
        except Exception as e:
            print("  ! discover %s: %s: %s" % (topic, type(e).__name__, e), file=sys.stderr)
            return []

    # Sequential and paced. Unauthenticated search allows 10 requests a minute, so firing
    # the topics in parallel trips the limit on the first burst. A token lifts the ceiling,
    # so only pace when we do not have one. This runs weekly, not daily.
    gap = 0 if headers.get("Authorization") else 7
    found = set()
    for i, topic in enumerate(TOPICS):
        if i and gap:
            time.sleep(gap)
        found.update(search(topic))
    found = sorted(found)

    if found:
        REPO_CACHE.write_text(json.dumps(found, indent=0))
        return found
    if REPO_CACHE.exists():
        print("  discovery failed, using cached repo list", file=sys.stderr)
        return json.loads(REPO_CACHE.read_text())
    return []


def fetch_release(repo):
    """A repo's releases.atom. Titles are often bare tags, so prefix the repo name."""
    try:
        items = parse_feed(repo, get("https://github.com/%s/releases.atom" % repo), limit=4)
    except Exception as e:
        print("  ! %s releases: %s: %s" % (repo, type(e).__name__, e), file=sys.stderr)
        return []
    for it in items:
        it["title"] = "%s %s" % (repo.split("/")[-1], it["title"])
        it["summary"] = it["summary"][:300]      # release notes are long and mostly changelog
        it["source"] = "release: " + repo
    return items


def fetch_hf_models():
    """Trending models on the Hub -- how a new open-weights release actually shows up."""
    url = ("https://huggingface.co/api/models?sort=trendingScore&direction=-1&limit=30"
           "&full=false")
    try:
        models = json.loads(get(url))
    except Exception as e:
        print("  ! HF models: %s: %s" % (type(e).__name__, e), file=sys.stderr)
        return []
    out = []
    for m in models:
        mid = m.get("modelId") or m.get("id")
        if not mid:
            continue
        tags = [t for t in m.get("tags", []) if ":" not in t and t != "region:us"][:6]
        out.append({"source": "HF model (%s dl)" % m.get("downloads", 0),
                    "title": "New on the Hub: " + mid,
                    "link": "https://huggingface.co/" + mid,
                    "summary": "pipeline: %s. tags: %s" % (m.get("pipeline_tag", "?"),
                                                           ", ".join(tags)),
                    "date": parse_date(m.get("createdAt") or m.get("lastModified"))})
    return out


def fetch_hf_papers():
    try:
        papers = json.loads(get("https://huggingface.co/api/daily_papers"))
    except Exception as e:
        print("  ! HF papers: %s: %s" % (type(e).__name__, e), file=sys.stderr)
        return []
    out = []
    for p in papers[:25]:
        paper = p.get("paper", {})
        pid = paper.get("id")
        if not pid:
            continue
        out.append({"source": "HF Papers (%s up)" % paper.get("upvotes", 0),
                    "title": strip_html(paper.get("title", ""), 300),
                    "link": "https://huggingface.co/papers/" + pid,
                    "summary": strip_html(paper.get("summary", "")),
                    "date": parse_date(p.get("publishedAt") or paper.get("publishedAt"))})
    return out


def collect():
    repos = discover_repos()
    print("  watching %d repos for releases" % len(repos))
    jobs = ([("feed", f) for f in FEEDS] + [("arxiv", q) for q in ARXIV_QUERIES]
            + [("hn", q) for q in HN_QUERIES] + [("rel", r) for r in repos]
            + [("papers", None), ("models", None)])
    runners = {"feed": fetch_feed, "arxiv": fetch_arxiv, "hn": fetch_hn,
               "rel": fetch_release, "papers": lambda _: fetch_hf_papers(),
               "models": lambda _: fetch_hf_models()}
    with ThreadPoolExecutor(16) as ex:
        results = list(ex.map(lambda j: runners[j[0]](j[1]), jobs))

    cutoff = datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)
    seen = set(json.loads(SEEN_FILE.read_text())) if SEEN_FILE.exists() else set()
    items, dupes = {}, 0
    for batch in results:
        for it in batch:
            if it["date"] and it["date"] < cutoff:
                continue
            if it["link"] in seen:
                dupes += 1
                continue
            items.setdefault(it["link"], it)   # first source to carry a URL wins
    print("  %d new items (%d already sent before)" % (len(items), dupes))
    return list(items.values())


PROMPT = """You are curating a daily AI digest for one specific engineer. Here is who they are:

{profile}

Below are {n} items from the last {hours} hours. Sort the best of them into TWO lists.

SHIPS -- up to {ships} items. Anything in AI that exists NOW and can be downloaded,
installed, called or upgraded today, rather than only written about. Models and weights,
coding agents and IDEs, inference servers, speech and voice libraries, agent frameworks,
vector stores, APIs, pricing and quota changes, developer tools, hosted services. Treat
that as a description of a domain, not a checklist -- if it is a real, usable AI thing
that shipped, it belongs here whatever its category. A version bump only earns a slot if
it changes something worth knowing: a real feature, a breaking change, a meaningful
speedup. Routine patch releases and dependency bumps do not count; drop them.

RESEARCH -- up to {research} items. Papers, benchmarks, evaluations and engineering
writeups. Ideas rather than artifacts.

Judge relevance by their actual work, not general AI newsworthiness. A modest paper on
speaker diarization beats a huge funding announcement. Weight things touching their voice
and speech stack, Indic languages, evals, guardrails, and inference cost most heavily.

Drop funding news, hiring news, opinion pieces, outage chatter and vague enterprise
announcements entirely. Return fewer than the limits if the rest do not clear the bar --
never pad a list to fill it.

Copy "link" and "source" verbatim from the item you picked. Do not invent a URL.

Ground every word of "what" and "why" in the text actually given for that item. Release
notes here are truncated, so you will often not know what changed -- in that case say so
plainly ("release notes not summarised here") or drop the item. Never invent a capability,
a benchmark or a connection to their work that the text does not support: a made-up reason
is worse than a missing item. Only claim something touches speech, voice or turn-taking if
the item itself is about speech, voice or turn-taking. Tag by what the thing IS, not by
which part of their work you are trying to connect it to.

Return JSON only:
{{"ships":    [{{"title": "...", "link": "...", "source": "...", "what": "...",
                "why": "...", "tag": "model|tool|harness|speech|infra"}}],
 "research": [{{"title": "...", "link": "...", "source": "...", "what": "...",
                "why": "...", "tag": "speech|agents|eval|safety|research"}}],
 "skipped_note": "one short line on what else was in the pile and why it did not make it"}}

"what" is 1-2 plain sentences on what the thing actually is.
"why" is one concrete sentence on why it matters to THIS person's work.

ITEMS:
{items}"""


def rank(items):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        sys.exit("GEMINI_API_KEY is not set. Create one at https://aistudio.google.com/apikey")
    # Actions passes an empty string for an unset `vars.X`, so `or` not `get(..., default)`.
    model = os.environ.get("GEMINI_MODEL") or "gemini-3.5-flash"

    listing = "\n".join(
        "[%d] (%s) %s\n    %s\n    %s" % (i, it["source"], it["title"], it["link"],
                                          it["summary"][:280])
        for i, it in enumerate(items))
    body = json.dumps({
        "contents": [{"parts": [{"text": PROMPT.format(
            profile=PROFILE, n=len(items), hours=WINDOW_HOURS, ships=KEEP_SHIPS,
            research=KEEP_RESEARCH, items=listing)}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2},
    }).encode()

    # Auth keys want the header form; ?key= is the legacy standard-key style Google is retiring.
    url = ("https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % model)
    req = urllib.request.Request(url, data=body, headers={
        **UA, "Content-Type": "application/json", "x-goog-api-key": key})
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=180).read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:400]
        if e.code == 404:
            sys.exit("Model '%s' is not available to this key.\nAvailable:\n  %s\n"
                     "Set GEMINI_MODEL to one of those."
                     % (model, "\n  ".join(available_models(key))))
        if e.code in (401, 403):
            sys.exit("Gemini rejected the key (%d). Standard API keys stopped working in\n"
                     "September 2026 -- create a fresh auth key at\n"
                     "https://aistudio.google.com/apikey\n\n%s" % (e.code, detail))
        sys.exit("Gemini call failed (%d): %s" % (e.code, detail))
    return json.loads(resp["candidates"][0]["content"]["parts"][0]["text"])


def available_models(key):
    try:
        d = json.loads(get("https://generativelanguage.googleapis.com/v1beta/models",
                           headers={"x-goog-api-key": key}))
        return sorted(m["name"].replace("models/", "") for m in d.get("models", [])
                      if "generateContent" in m.get("supportedGenerationMethods", []))
    except Exception as e:
        return ["(could not list models: %s)" % e]


TAG_COLOR = {"model": "#7c3aed", "speech": "#0891b2", "agents": "#ea580c",
             "eval": "#16a34a", "infra": "#64748b", "safety": "#dc2626",
             "research": "#4f46e5", "tool": "#0d9488", "harness": "#c2410c"}
SANS = "-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif"


def section(heading, blurb, items):
    if not items:
        return ""
    rows = []
    for i, it in enumerate(items, 1):
        colour = TAG_COLOR.get(it.get("tag", ""), "#64748b")
        rows.append("""
    <div style="margin:0 0 26px;padding:0 0 22px;border-bottom:1px solid #e8e8ea">
      <div style="font:600 11px/1.4 {sans};letter-spacing:.09em;text-transform:uppercase;
                  color:{colour};margin-bottom:7px">
        {n:02d} &nbsp;&middot;&nbsp; {tag} &nbsp;&middot;&nbsp; {source}
      </div>
      <a href="{link}" style="font:600 17px/1.4 {sans};color:#111827;text-decoration:none">{title}</a>
      <p style="font:15px/1.6 {sans};color:#374151;margin:9px 0 0">{what}</p>
      <p style="font:15px/1.6 {sans};color:#6b21a8;margin:7px 0 0">
        <strong style="color:#581c87">Why you care:</strong> {why}</p>
    </div>""".format(sans=SANS, colour=colour, n=i, tag=escape(it.get("tag", "")),
                     source=escape(it.get("source", "")), link=escape(it.get("link", "#")),
                     title=escape(it.get("title", "")), what=escape(it.get("what", "")),
                     why=escape(it.get("why", ""))))

    return """
  <div style="font:700 13px/1.3 {sans};letter-spacing:.1em;text-transform:uppercase;
              color:#111827;margin:34px 0 3px">{heading}</div>
  <div style="font:13px/1.5 {sans};color:#9ca3af;margin:0 0 20px">{blurb}</div>
  {rows}""".format(sans=SANS, heading=escape(heading), blurb=escape(blurb),
                   rows="".join(rows))


def render(digest, date_str):
    ships, research = digest.get("ships", []), digest.get("research", [])
    body = (section("Shipped", "Out now — usable today.", ships)
            + section("Research & writing", "Ideas, benchmarks, writeups.", research))
    if not body:
        body = ('<p style="font:15px %s;color:#6b7280">Nothing cleared the bar today.</p>'
                % SANS)
    return """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI digest &mdash; {date}</title></head>
<body style="margin:0;background:#f6f6f7;padding:26px 14px">
<div style="max-width:660px;margin:0 auto;background:#fff;border-radius:12px;padding:30px">
  <div style="font:700 23px/1.2 {sans};color:#111827">Latest in AI</div>
  <div style="font:14px {sans};color:#9ca3af;margin:5px 0 0">
    {date} &nbsp;&middot;&nbsp; {ships} shipped, {research} to read</div>
  {body}
  <p style="font:13px/1.6 {sans};color:#9ca3af;margin:26px 0 0">{note}</p>
</div></body></html>""".format(date=escape(date_str), sans=SANS, body=body,
                               ships=len(ships), research=len(research),
                               note=escape(digest.get("skipped_note", "")))


def send(html, date_str):
    to, pw = os.environ.get("DIGEST_TO"), os.environ.get("DIGEST_SMTP_PASS")
    if not (to and pw):
        print("  DIGEST_TO / DIGEST_SMTP_PASS not set -- skipping email")
        return
    frm = os.environ.get("DIGEST_FROM", to)
    msg = EmailMessage()
    msg["Subject"] = "AI digest — " + date_str
    msg["From"], msg["To"] = frm, to
    msg.set_content("This digest is HTML. Open it in a client that renders HTML.")
    msg.add_alternative(html, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=45) as s:
        s.login(frm, pw)
        s.send_message(msg)
    print("  emailed to " + to)


def remember(digest):
    """Only remember what was actually sent, so a good item crowded out today can return."""
    sent = {it.get("link") for it in digest.get("ships", []) + digest.get("research", [])}
    sent -= {None}
    prev = json.loads(SEEN_FILE.read_text()) if SEEN_FILE.exists() else []
    SEEN_FILE.write_text(json.dumps((prev + sorted(sent))[-1500:], indent=0))


def self_check():
    rss = """<rss><channel><item><title>Model &amp; Speed</title>
      <link>https://x.test/a</link><description>&lt;p&gt;Body   text&lt;/p&gt;</description>
      <pubDate>Wed, 03 Sep 2026 10:00:00 GMT</pubDate></item></channel></rss>"""
    atom = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Atom Post</title>
      <link rel="alternate" href="https://x.test/b"/><summary>Sum</summary>
      <published>2026-09-03T10:00:00Z</published></entry></feed>"""
    r = parse_feed("R", rss)[0]
    assert r["title"] == "Model & Speed", r["title"]
    assert r["summary"] == "Body text", repr(r["summary"])
    assert r["date"].year == 2026 and r["date"].tzinfo is not None, r["date"]
    b = parse_feed("A", atom)[0]
    assert b["link"] == "https://x.test/b", b["link"]      # Atom link lives in an attribute
    assert b["date"].tzinfo is not None, b["date"]
    assert parse_date("not a date") is None and parse_date("") is None
    assert strip_html("<b>a</b>\n\n b") == "a b"
    html = render({"ships": [{"title": "<script>x</script>", "link": "https://x.test/c",
                             "source": "S", "what": "w", "why": "y", "tag": "model"}],
                   "research": [], "skipped_note": "n"}, "Today")
    assert "<script>x</script>" not in html and "&lt;script&gt;" in html, "title not escaped"
    print("self-check ok")


def load_dotenv():
    """Local convenience. In GitHub Actions the env is already set and there is no file."""
    f = HERE / ".env"
    if not f.exists():
        return
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def sent_today():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return SENT_FILE.exists() and SENT_FILE.read_text().strip() == today


def main():
    args = sys.argv[1:]
    if "--self-check" in args:
        return self_check()
    load_dotenv()

    # GitHub drops scheduled jobs under load, so the workflow fires several times a
    # morning. The first one through sends; the rest see today's date here and stop.
    if "--once-daily" in args and sent_today():
        return print("Already sent today. Nothing to do.")

    date_str = datetime.now().strftime("%A, %d %B %Y")
    print("Collecting for %s..." % date_str)
    items = collect()
    if "--dry-run" in args:
        for it in sorted(items, key=lambda i: i["source"]):
            print("  %-22s %s" % (it["source"], it["title"][:76]))
        return
    if not items:
        return print("Nothing new. No digest sent.")

    digest = rank(items)
    html = render(digest, date_str)
    out = HERE / "digests" / (datetime.now().strftime("%Y-%m-%d") + ".html")
    out.parent.mkdir(exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print("  wrote %s  (%d shipped, %d research)"
          % (out, len(digest.get("ships", [])), len(digest.get("research", []))))

    if "--no-email" not in args:
        send(html, date_str)
        SENT_FILE.write_text(datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    remember(digest)


if __name__ == "__main__":
    main()
