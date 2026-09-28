"""Daily AI digest: fetch feeds, rank them against what I actually work on, email it.

    python digest.py                 # fetch, rank, write HTML, email
    python digest.py --no-email      # write the HTML only
    python digest.py --dry-run       # fetch and count, no LLM, no email
    python digest.py --once-daily    # skip if a digest already went out today
    python digest.py --self-check    # parser asserts, no network
    python digest.py --send-file F   # email an already-built digest (used by send.yml)
    python digest.py --collect-json F  # fetch only, items as JSON (used by collect.yml)

Env: GEMINI_API_KEY (required), GEMINI_MODEL, DIGEST_TO, DIGEST_FROM, DIGEST_SMTP_PASS,
     DIGEST_PROFILE, DIGEST_KEEP_SHIPS, DIGEST_KEEP_INDUSTRY, DIGEST_KEEP_RESEARCH,
     GITHUB_TOKEN (all optional --
     see load_config() and config.example.json).
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
CONFIG_FILE = HERE / "config.json"   # per-person overrides; see config.example.json
WINDOW_HOURS = 72          # generous: covers weekends and a missed run; seen.json kills repeats
MAX_PER_SOURCE = 15

# Everything below is what makes this MY digest rather than anyone else's. It lives in
# config.json (gitignored, one per person) if that file exists, else these defaults.
# Copy config.example.json to config.json to run this against your own work instead of
# editing the script -- that is the only file a colleague needs to touch.
KEEP_SHIPS = 15            # releases, models, tools
KEEP_INDUSTRY = 10         # funding, people, deals, policy, commentary
KEEP_RESEARCH = 5          # papers and writeups

# When the mail should land, on the reader's clock. GitHub fires this repo's scheduled
# runs 4-6 hours late, by an amount that varies day to day, so the workflow fires a cron
# every half hour through the night and morning and THIS decides which of those runs
# actually sends: the first one to land at or after SEND_AFTER. Timing lives here because
# the scheduler demonstrably does not keep time.
SEND_AFTER = "09:45"
# A fixed offset, not a zoneinfo name: India has no DST so this is exact, and zoneinfo
# needs the tzdata package on Windows, which would break "standard library only".
UTC_OFFSET = "+05:30"
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
voice-agent quality, evaluation methods, agent reliability, guardrails, anything
that makes inference cheaper or faster, and how to develop with AI better -- new
Claude Code plugins, skills, harnesses, MCP servers, and agentic coding tooling.
I do not care about: funding rounds, executive hires, general AI punditry, doomer or
hype takes, enterprise press releases, and routine version-bump releases of libraries
I don't actively use.
"""


def load_config():
    """Pull PROFILE / KEEP_SHIPS / KEEP_RESEARCH from config.json, then let
    DIGEST_PROFILE / DIGEST_KEEP_SHIPS / DIGEST_KEEP_RESEARCH env vars override that.

    config.json is gitignored on purpose (nobody's profile should land in the shared
    repo), which means a checkout under GitHub Actions never has one -- so a scheduled
    run needs its own way in. The env vars are that way in: same repo-secrets pattern
    already used for GEMINI_API_KEY. Local runs can use either; config.json is the
    easy path by hand, the env vars are what Actions actually needs.
    """
    global PROFILE
    g = globals()
    keeps = ("KEEP_SHIPS", "KEEP_INDUSTRY", "KEEP_RESEARCH")
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            PROFILE = cfg.get("profile", PROFILE)
            for name in keeps:
                g[name] = cfg.get(name.lower(), g[name])
        except Exception as e:
            print("  ! config.json is invalid (%s), using defaults" % e, file=sys.stderr)

    # Actions substitutes an empty string for an unset secret/var, so `or` a truthy
    # check here rather than trusting presence -- same trap as the GEMINI_MODEL fix.
    if os.environ.get("DIGEST_PROFILE"):
        PROFILE = os.environ["DIGEST_PROFILE"]
    for name in keeps:
        raw = os.environ.get("DIGEST_" + name)
        if raw:
            try:
                g[name] = int(raw)
            except ValueError:
                print("  ! DIGEST_%s=%r is not a number, keeping %d" % (name, raw, g[name]),
                      file=sys.stderr)

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
    ("Together AI",     "https://www.together.ai/blog/rss.xml"),
    # Industry: funding, people, deals, policy.
    ("TechCrunch AI",   "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("The Verge AI",    "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    # Tools and IDEs, not research.
    ("Cursor",          "https://cursor.com/changelog/rss.xml"),
    ("VS Code",         "https://code.visualstudio.com/feed.xml"),
    ("GitHub",          "https://github.blog/changelog/feed/"),
]

# Which repos to watch for releases is discovered from these topics, not listed here.
# Whatever is currently big and active in each area gets picked up on its own -- so a
# tool that did not exist last month arrives without this file being edited.
TOPICS = ["llm", "ai-agents", "speech-recognition", "text-to-speech", "voice-assistant",
          "rag", "vector-database", "llmops", "mlops", "code-generation",
          "claude-code", "mcp-server", "model-context-protocol"]
REPOS_PER_TOPIC = 6
# Big in their topics, so discovery keeps finding them, but they ship near-daily and I
# don't use them. Lowercase owner/name.
MUTED_REPOS = {"promptfoo/promptfoo", "ollama/ollama"}
# Anthropic's own dev-tooling repos carry no GitHub topics at all (verified Sept 2026),
# so topic discovery above structurally never finds them. Watched directly instead.
ALWAYS_WATCH_REPOS = {"anthropics/claude-code", "anthropics/skills",
                       "anthropics/claude-agent-sdk-python",
                       "anthropics/claude-agent-sdk-typescript",
                       "modelcontextprotocol/servers"}
REPO_CACHE = HERE / "repos.json"   # last good discovery, used if GitHub rate-limits us

# arXiv, scoped to what I work on rather than the whole of cs.AI: today's listing for
# these categories, kept only if the title or abstract matches one of the topics below.
# (?=.*a)(?=.*b) is "a AND b" in either order.
ARXIV_CATEGORIES = "cs.CL+cs.SD+eess.AS+cs.AI+cs.CR+cs.IR+cs.LG"
ARXIV_TOPICS = [
    r"speech recognition|speaker diarization|text[- ]to[- ]speech",
    r"voice agent|spoken dialogue|full[- ]duplex",
    r"(?=.*LLM[- ]agent)(?=.*(evaluation|reliability|benchmark))",
    r"guardrail|jailbreak|prompt injection",
    r"retrieval[- ]augmented generation|LLM[- ]as[- ]a[- ]judge",
    r"(?=.*low[- ]resource)(?=.*(Hindi|Tamil|Indic|multilingual))",
]
ARXIV_PER_TOPIC = 12

# Hacker News catches the vendors with no RSS (Anthropic, Meta, Mistral) and release news.
HN_QUERIES = ["anthropic", "claude", "llm", "gemini", "open source model",
              "speech recognition", "voice ai", "ai agents",
              "claude code", "mcp server"]

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


def parse_feed(name, xml, limit=MAX_PER_SOURCE, summary_chars=400):
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
                        "link": link.strip(), "summary": strip_html(summary, summary_chars), "date": date})
    return out


def fetch_feed(item):
    name, url = item
    try:
        return parse_feed(name, get(url))
    except Exception as e:
        print("  ! %s: %s: %s" % (name, type(e).__name__, e), file=sys.stderr)
        return []


def fetch_arxiv():
    """Today's arXiv listing, filtered here by ARXIV_TOPICS.

    Not the search API: since Sept 2026 export.arxiv.org/api answers Python's HTTP client
    with 406 on every query that isn't already cached, while curl with identical headers
    gets 200 -- so the block is below the header level and no header change fixes it.
    The RSS listings serve Python fine, and a daily listing suits a daily digest anyway.
    """
    try:
        papers = parse_feed("arXiv", get("https://rss.arxiv.org/rss/" + ARXIV_CATEGORIES,
                                         timeout=60), limit=None, summary_chars=3000)
    except Exception as e:
        print("  ! arXiv: %s: %s" % (type(e).__name__, e), file=sys.stderr)
        return []
    # "replace" entries are revisions of old papers, not new work.
    papers = [p for p in papers if "Announce Type: replace" not in p["summary"][:80]]
    out = []
    for pattern in ARXIV_TOPICS:
        hits = [p for p in papers
                if re.search(pattern, p["title"] + " " + p["summary"], re.I | re.S)]
        out += hits[:ARXIV_PER_TOPIC]
    for p in out:
        p["summary"] = re.sub(r"^.*?Abstract:\s*", "", p["summary"])[:400]
    return out


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
    repos = [r for r in discover_repos() if r.lower() not in MUTED_REPOS]
    repos = sorted(set(repos) | ALWAYS_WATCH_REPOS)
    print("  watching %d repos for releases" % len(repos))
    jobs = ([("feed", f) for f in FEEDS] + [("hn", q) for q in HN_QUERIES]
            + [("rel", r) for r in repos]
            + [("arxiv", None), ("papers", None), ("models", None)])
    runners = {"feed": fetch_feed, "arxiv": lambda _: fetch_arxiv(), "hn": fetch_hn,
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

Below are {n} items from the last {hours} hours. Sort the best of them into THREE lists.

SHIPS -- up to {ships} items. Anything in AI that exists NOW and can be downloaded,
installed, called or upgraded today, rather than only written about. Models and weights,
coding agents and IDEs, inference servers, speech and voice libraries, agent frameworks,
vector stores, APIs, pricing and quota changes, developer tools, hosted services. Treat
that as a description of a domain, not a checklist -- if it is a real, usable AI thing
that shipped, it belongs here whatever its category. A version bump only earns a slot if
it changes something worth knowing: a real feature, a breaking change, a meaningful
speedup. Routine patch releases and dependency bumps do not count; drop them. If the
title is just a name and a version number and the notes don't spell out a real change
("bug fixes", "misc improvements", or notes truncated with nothing substantive left),
that is noise -- drop it even for a repo this person watches closely. Being watched does
not make every tag newsworthy.

INDUSTRY -- up to {industry} items. The business and people side of AI: funding rounds,
acquisitions, executive moves, company strategy, partnerships, enterprise deals, policy
and regulation, and commentary or opinion worth reading. Anything that shipped a usable
thing goes in SHIPS instead. Pick by how much it moves the AI industry, and prefer ones
that touch the labs, vendors and tools in their stack.

RESEARCH -- up to {research} items. Papers, benchmarks, evaluations and engineering
writeups. Ideas rather than artifacts.

For SHIPS and RESEARCH, judge relevance by their actual work, not general AI
newsworthiness. Weight things touching their voice and speech stack, Indic languages,
evals, guardrails, and inference cost most heavily. A new Indic speech model or a
voice-agent framework release beats any coding-tool update.

AI coding tools (Claude Code, agent SDKs, MCP servers, plugins, coding agents) are a
secondary interest: at most 4 SHIPS slots between them, and only for a genuinely new
thing or a real feature, never a routine point release. Their core work never gets
pushed out to make room for these.

Drop outage chatter entirely. Each item goes in at most one list. Return fewer than the
limits if the rest do not clear the bar -- never pad a list to fill it.

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
 "industry": [{{"title": "...", "link": "...", "source": "...", "what": "...",
                "why": "...", "tag": "funding|people|business|policy|opinion"}}],
 "research": [{{"title": "...", "link": "...", "source": "...", "what": "...",
                "why": "...", "tag": "speech|agents|eval|safety|research"}}],
 "skipped_note": "one short line on what else was in the pile and why it did not make it"}}

"what" is 1-2 plain sentences on what the thing actually is.
"why" is one concrete sentence on why it matters to THIS person's work. For an INDUSTRY
item with no real link to their work, say why it matters to the field instead.

ITEMS:
{items}"""

SECTIONS = ("ships", "industry", "research")   # the model's output keys, in email order


def _extract_text(resp):
    """Pull the model's text out of a Gemini response, with a clear error instead of a
    bare KeyError/IndexError if it came back blocked or empty -- a real possibility here
    since PROFILE and the items themselves talk about jailbreaks and prompt injection."""
    candidates = resp.get("candidates") or []
    if not candidates:
        reason = resp.get("promptFeedback", {}).get("blockReason", "no reason given")
        sys.exit("Gemini returned no candidates (blockReason: %s). Nothing to send today." % reason)
    parts = candidates[0].get("content", {}).get("parts") or []
    if not parts:
        sys.exit("Gemini returned an empty response (finishReason: %s)."
                  % candidates[0].get("finishReason", "unknown"))
    return parts[0]["text"]


def _keep_known_links(result, valid_links):
    """Drop any ships/research entry whose link wasn't in the items we actually fetched.

    The prompt already tells the model not to invent a URL, but that's a request, not a
    guarantee -- this is the structural backstop for the same hallucination problem.
    """
    for key in SECTIONS:
        picked = result.get(key, [])
        kept = [e for e in picked if e.get("link") in valid_links]
        dropped = len(picked) - len(kept)
        if dropped:
            print("  ! dropped %d %s item(s) with a link not in the fetched set" % (dropped, key),
                  file=sys.stderr)
        result[key] = kept
    return result


def rank(items):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        sys.exit("GEMINI_API_KEY is not set. Create one at https://aistudio.google.com/apikey")
    # Actions passes an empty string for an unset `vars.X`, so `or` not `get(..., default)`.
    # Newest Flash, not Pro: the free tier's Pro quota is zero (verified Sept 2026, every
    # call 429s), and the -latest alias moves up with each Flash release on its own.
    # The newest Flash is also the one Google overloads first -- on 28 Sept 2026 3.7, 3.8
    # and -latest all 503'd while 3.6 answered -- so fall back rather than send nothing.
    # ponytail: pinned fallback; when Google retires it the 404 message below says so.
    models = list(dict.fromkeys([os.environ.get("GEMINI_MODEL") or "gemini-flash-latest",
                                 "gemini-3.6-flash"]))

    listing = "\n".join(
        "[%d] (%s) %s\n    %s\n    %s" % (i, it["source"], it["title"], it["link"],
                                          it["summary"][:280])
        for i, it in enumerate(items))
    body = json.dumps({
        "contents": [{"parts": [{"text": PROMPT.format(
            profile=PROFILE, n=len(items), hours=WINDOW_HOURS, ships=KEEP_SHIPS,
            industry=KEEP_INDUSTRY, research=KEEP_RESEARCH, items=listing)}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2},
    }).encode()

    resp = None
    for model in models:
        resp = _call_gemini(model, body, key)
        if resp is not None:
            break
        print("  ! %s kept failing, trying the next model" % model, file=sys.stderr)
    if resp is None:
        sys.exit("Gemini did not answer on %s. Nothing to send today." % ", ".join(models))

    result = json.loads(_extract_text(resp))
    return _keep_known_links(result, {it["link"] for it in items})


def _call_gemini(model, body, key):
    """The parsed response, or None if the model stayed overloaded or unreachable."""
    # Auth keys want the header form; ?key= is the legacy standard-key style Google is retiring.
    url = ("https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % model)
    req = urllib.request.Request(url, data=body, headers={
        **UA, "Content-Type": "application/json", "x-goog-api-key": key})

    # "High demand" 503s usually clear within a minute or two, so wait between tries rather
    # than hammering. If all three fail, rank() moves on to the next model.
    for attempt in (1, 2, 3):
        try:
            return json.loads(urllib.request.urlopen(req, timeout=180).read())
        except urllib.error.HTTPError as e:
            if e.code in (500, 502, 503, 504):
                if attempt < 3:
                    print("  ! Gemini %d, retrying in %ds" % (e.code, 30 * attempt),
                          file=sys.stderr)
                    time.sleep(30 * attempt)
                    continue
                return None
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
        except (TimeoutError, urllib.error.URLError, ConnectionError) as e:
            # A hung or dropped connection, not an HTTP answer -- same "try again" case.
            if attempt < 3:
                print("  ! Gemini %s, retrying in %ds" % (type(e).__name__, 30 * attempt),
                      file=sys.stderr)
                time.sleep(30 * attempt)
                continue
            return None


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
             "research": "#4f46e5", "tool": "#0d9488", "harness": "#c2410c",
             "funding": "#15803d", "people": "#be185d", "business": "#1d4ed8",
             "policy": "#b45309", "opinion": "#6b7280"}
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
    ships, industry, research = (digest.get(k, []) for k in SECTIONS)
    body = (section("Shipped", "Out now — usable today.", ships)
            + section("Industry", "Money, people, deals, policy.", industry)
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
    {date} &nbsp;&middot;&nbsp; {ships} shipped, {industry} industry, {research} to read</div>
  {body}
  <p style="font:13px/1.6 {sans};color:#9ca3af;margin:26px 0 0">{note}</p>
</div></body></html>""".format(date=escape(date_str), sans=SANS, body=body,
                               ships=len(ships), industry=len(industry), research=len(research),
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


def send_file(path):
    """Email an already-built digest. The Claude routine does the picking and pushes the
    HTML to the claude/digest branch; send.yml calls this to deliver it, because the
    routine's sandbox can't reach Gmail's mail server itself."""
    f = Path(path)
    if not (path and f.is_file()):
        sys.exit("--send-file needs an existing HTML file, got %r" % path)
    send(f.read_text(encoding="utf-8"), local_now().strftime("%A, %d %B %Y"))


def collect_json(path):
    """Fetch every source and write the items as JSON for the Claude routine to pick from.
    Runs on GitHub (collect.yml) because the routine's sandbox can't reach GitHub release
    feeds or discovery for repos not attached to it, and some sites block its network."""
    if not path:
        sys.exit("--collect-json needs an output path")
    items = collect()
    for it in items:
        it["date"] = it["date"].isoformat() if it["date"] else None
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(items, indent=1), encoding="utf-8")
    print("  wrote %d items to %s" % (len(items), path))


def remember(digest):
    """Only remember what was actually sent, so a good item crowded out today can return."""
    sent = {it.get("link") for k in SECTIONS for it in digest.get(k, [])}
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

    # arXiv topic AND-patterns: both terms, either order, across lines.
    hit = lambda t: any(re.search(p, t, re.I | re.S) for p in ARXIV_TOPICS)
    assert hit("A Tamil benchmark.\nWe study low-resource ASR")
    assert not hit("A low-resource vision benchmark")
    assert hit("Retrieval-Augmented Generation for call centres")
    assert b["date"].tzinfo is not None, b["date"]
    assert parse_date("not a date") is None and parse_date("") is None
    assert strip_html("<b>a</b>\n\n b") == "a b"
    html = render({"ships": [{"title": "<script>x</script>", "link": "https://x.test/c",
                             "source": "S", "what": "w", "why": "y", "tag": "model"}],
                   "industry": [{"title": "Big round", "link": "https://x.test/d",
                                 "source": "S", "what": "w", "why": "y", "tag": "funding"}],
                   "research": [], "skipped_note": "n"}, "Today")
    assert "<script>x</script>" not in html and "&lt;script&gt;" in html, "title not escaped"
    assert "Big round" in html and "1 industry" in html, "industry section missing"

    # A link the model invents rather than copies from the fetched items gets dropped.
    filtered = _keep_known_links(
        {"ships": [{"link": "https://real.test/1"}, {"link": "https://made-up.test/2"}],
         "industry": [{"link": "https://made-up.test/3"}],
         "research": [{"link": "https://real.test/1"}]},
        {"https://real.test/1"})
    assert [e["link"] for e in filtered["ships"]] == ["https://real.test/1"]
    assert filtered["industry"] == [], "invented industry link should be dropped"
    assert len(filtered["research"]) == 1

    # A blocked or empty Gemini response should raise a clear error, not a bare KeyError.
    for bad_resp in ({"candidates": []}, {"candidates": [{"content": {"parts": []}}]}):
        try:
            _extract_text(bad_resp)
            assert False, "should have exited on %r" % bad_resp
        except SystemExit:
            pass

    # An overloaded model falls back to the next one instead of sending nothing.
    tried, real_open, real_sleep = [], urllib.request.urlopen, time.sleep
    def fake_open(req, timeout=None):
        tried.append(req.full_url.split("/models/")[1].split(":")[0])
        if tried[-1] == "gemini-flash-latest":
            raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, None)
        class R:
            read = lambda self: json.dumps({"candidates": [{"content": {"parts": [
                {"text": '{"ships": []}'}]}}]}).encode()
        return R()
    urllib.request.urlopen, time.sleep = fake_open, lambda s: None
    os.environ["GEMINI_API_KEY"] = os.environ.get("GEMINI_API_KEY") or "test"
    try:
        rank([{"source": "s", "title": "t", "link": "https://x.test/e", "summary": ""}])
    finally:
        urllib.request.urlopen, time.sleep = real_open, real_sleep
    assert tried == ["gemini-flash-latest"] * 3 + ["gemini-3.6-flash"], tried

    # --send-file with a missing path should stop with a message, not a traceback.
    for bad in ("", "no/such/file.html"):
        try:
            send_file(bad)
            assert False, "send_file(%r) should have exited" % bad
        except SystemExit:
            pass

    # --collect-json writes dates as text (JSON has no datetime) and keeps undated items.
    import tempfile
    real_collect = globals()["collect"]
    globals()["collect"] = lambda: [dict(r), dict(b, date=None)]
    try:
        out = Path(tempfile.mkdtemp()) / "sub" / "items.json"
        collect_json(str(out))
        got = json.loads(out.read_text(encoding="utf-8"))
    finally:
        globals()["collect"] = real_collect
    assert got[0]["date"].startswith("2026-09-03") and got[1]["date"] is None, got

    # A non-numeric DIGEST_KEEP_SHIPS should warn and leave the value alone, not crash.
    saved = KEEP_SHIPS
    os.environ["DIGEST_KEEP_SHIPS"] = "not a number"
    load_config()
    del os.environ["DIGEST_KEEP_SHIPS"]
    assert KEEP_SHIPS == saved, "bad DIGEST_KEEP_SHIPS should not have changed KEEP_SHIPS"
    saved = KEEP_INDUSTRY
    os.environ["DIGEST_KEEP_INDUSTRY"] = "7"
    load_config()
    del os.environ["DIGEST_KEEP_INDUSTRY"]
    assert KEEP_INDUSTRY == 7, "DIGEST_KEEP_INDUSTRY should override"
    globals()["KEEP_INDUSTRY"] = saved

    # The send gate. This is the whole reason the mail lands when it does, so it gets
    # real tests -- `now` is injectable so they don't depend on when they are run.
    os.environ["DIGEST_SEND_AFTER"] = "09:45"
    assert too_early(datetime(2026, 1, 1, 9, 44)), "09:44 is before the cutoff"
    assert not too_early(datetime(2026, 1, 1, 9, 45)), "09:45 is the cutoff itself"
    assert not too_early(datetime(2026, 1, 1, 14, 30)), "a late run must still send"
    assert too_early(datetime(2026, 1, 1, 0, 1)), "just after midnight is too early"
    os.environ["DIGEST_SEND_AFTER"] = "nonsense"
    assert not too_early(datetime(2026, 1, 1, 14, 30)), "bad cutoff should fall back, not crash"
    del os.environ["DIGEST_SEND_AFTER"]

    os.environ["DIGEST_UTC_OFFSET"] = "+05:30"
    assert _offset() == timedelta(hours=5, minutes=30)
    os.environ["DIGEST_UTC_OFFSET"] = "-04:00"
    assert _offset() == timedelta(hours=-4)
    os.environ["DIGEST_UTC_OFFSET"] = "garbage"
    assert _offset() == timedelta(hours=5, minutes=30), "bad offset should fall back to IST"
    del os.environ["DIGEST_UTC_OFFSET"]

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


def _offset():
    """UTC_OFFSET (or DIGEST_UTC_OFFSET) as a timedelta, e.g. '+05:30' -> 5h30m."""
    raw = (os.environ.get("DIGEST_UTC_OFFSET") or UTC_OFFSET).strip()
    try:
        sign = -1 if raw.startswith("-") else 1
        hh, mm = raw.lstrip("+-").split(":")
        return sign * timedelta(hours=int(hh), minutes=int(mm))
    except Exception:
        print("  ! DIGEST_UTC_OFFSET=%r is not like +05:30, using %s" % (raw, UTC_OFFSET),
              file=sys.stderr)
        hh, mm = UTC_OFFSET.lstrip("+").split(":")
        return timedelta(hours=int(hh), minutes=int(mm))


def local_now():
    """Wall-clock time where the reader is. The Actions runner is UTC, so everything
    user-facing -- the send gate, the date on the mail, the filename -- goes through here."""
    return datetime.now(timezone.utc).replace(tzinfo=None) + _offset()


def sent_today():
    return SENT_FILE.exists() and SENT_FILE.read_text().strip() == local_now().strftime("%Y-%m-%d")


def too_early(now=None):
    """Has the local clock reached SEND_AFTER yet?

    This is the whole timing mechanism. GitHub fires this repo's crons 4-6 hours late by
    a varying amount, so the workflow fires one every half hour through the night and the
    first run to land at or after SEND_AFTER is the one that sends. If GitHub ever starts
    firing on time instead, the later crons cover that -- it self-corrects either way.
    """
    raw = (os.environ.get("DIGEST_SEND_AFTER") or SEND_AFTER).strip()
    try:
        hh, mm = [int(x) for x in raw.split(":")]
    except Exception:
        print("  ! DIGEST_SEND_AFTER=%r is not like 09:45, using %s" % (raw, SEND_AFTER),
              file=sys.stderr)
        hh, mm = [int(x) for x in SEND_AFTER.split(":")]
    now = now or local_now()
    return (now.hour, now.minute) < (hh, mm)


def main():
    args = sys.argv[1:]
    if "--self-check" in args:
        return self_check()
    load_dotenv()
    load_config()
    if "--send-file" in args:
        rest = args[args.index("--send-file") + 1:]
        return send_file(rest[0] if rest else "")
    if "--collect-json" in args:
        rest = args[args.index("--collect-json") + 1:]
        return collect_json(rest[0] if rest else "")

    # The workflow fires a cron every half hour through the night because GitHub runs
    # them hours late by a varying amount. These two checks are what turn that spray of
    # runs into one mail at roughly the right local time: everything before SEND_AFTER
    # stops here, and so does everything after the day's mail has gone.
    if "--once-daily" in args:
        if sent_today():
            return print("Already sent today. Nothing to do.")
        if too_early():
            return print("Too early: local time %s, sends after %s."
                         % (local_now().strftime("%H:%M"),
                            os.environ.get("DIGEST_SEND_AFTER") or SEND_AFTER))

    date_str = local_now().strftime("%A, %d %B %Y")
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
    out = HERE / "digests" / (local_now().strftime("%Y-%m-%d") + ".html")
    out.parent.mkdir(exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print("  wrote %s  (%d shipped, %d industry, %d research)"
          % ((out,) + tuple(len(digest.get(k, [])) for k in SECTIONS)))

    if "--no-email" not in args:
        send(html, date_str)
        SENT_FILE.write_text(local_now().strftime("%Y-%m-%d"))
    remember(digest)


if __name__ == "__main__":
    main()
