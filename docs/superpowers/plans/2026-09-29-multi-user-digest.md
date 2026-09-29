# Multi-user AI digest: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A daily personalised news email for fewer than 10 people. Each person has their own
sources, Gemini key, send time and sections. Sign-up and edits happen by email, and it
runs on AWS always-free services.

**Architecture:** One Python Lambda function with three jobs:

- `inbox` reads the sign-up address over IMAP and handles sign-ups, edits and commands.
- `tick` sends digests that are due.
- `selftest` checks the deployment.

EventBridge Scheduler runs `inbox` and `tick` every 15 minutes. DynamoDB holds people,
already-sent links, and a cache. Gmail (SMTP on port 465, and IMAP) does all the email.
The pure logic (config checks, due times, inbox commands, prompt building) is separate
from the input/output (network, AWS, email), so it can be tested offline with fakes.

**Tech Stack:** Python 3.13 (the Lambda runtime), the standard library, boto3 (already in
the Lambda runtime), and `tzdata`, which is bundled because Windows and Lambda may not
include timezone data. Deployed with the AWS CLI; tests use `unittest`.

**Spec:** `docs/superpowers/specs/2026-09-29-multi-user-digest-design.md`

## Global Constraints

- All work is on branch `multiuser`. `main` stays untouched until cutover, because the
  owner's Claude routine runs `main`'s `digest.py` every morning.
- No dependencies beyond the standard library, boto3 and tzdata.
- Costs stay within AWS always-free limits:
  - DynamoDB uses provisioned capacity, at most 25 read and 25 write units in total
    across all tables.
  - Lambda gets 256 MB of memory and a 900-second timeout.
  - The two schedules run every 15 minutes.
- `send_time` is `HH:MM` on a quarter hour. `timezone` is an IANA name.
- `counts` is 0 to 20 for each of `ships`, `industry`, `research`, `practice`. A 0 turns
  that section off, and at least one must be above 0.
- A person is given up on 3 hours after their send time, and gets one "no digest today"
  note.
- `sent` rows expire after 60 days.
- Commit messages are plain English with no AI-attribution trailer (the owner's rule).
- Gemini models are tried in order: `gemini-flash-latest`, then `gemini-3.6-flash`.

## Review Focus

1. **Send times near midnight.** Example: someone with `send_time` 23:45 whose digest
   keeps failing. At 00:30 the next morning they must not get a give-up note for
   yesterday, and nothing may crash. Yesterday is skipped silently. Test in Task 6.
2. **The config file arriving in an odd form**: as `application/octet-stream`, with a
   UTF-8 byte-order mark, or with the AI's ```json fences left in. It must still be
   read. Tests in Tasks 7 and 8.
3. **Editing the config after today's digest went out.** That must not trigger a second
   digest the same day. Test in Task 8.
4. **Gemini returning more items than allowed, or items for a section set to 0.** Those
   must be trimmed or dropped. Test in Task 3.
5. **One person's revoked key or dead feeds.** That must not stop anyone else's digest,
   and they get exactly one give-up note that day, not one every 15 minutes. Test in
   Task 6.

---

## File map

| File | Responsibility |
|---|---|
| `app/__init__.py` | Makes `app` a package (empty). |
| `app/config.py` | `SECTIONS`, `validate_config(raw) -> (cfg or None, errors)`. |
| `app/sources.py` | Fetchers, the shared AI sources, `discover_repos`, `gather`, `check_sources`. |
| `app/pick.py` | Picking instructions, `build_prompt`, `keep_known_links`, `call_gemini`, `check_key`. |
| `app/render.py` | Digest HTML, give-up note HTML. |
| `app/store.py` | `MemoryStore` (tests) and `DynamoStore` (AWS), with the same methods. |
| `app/tick.py` | `due_state`, `mark_new_user`, `first_digest_text`, `run_tick`. |
| `app/mailer.py` | `send`, `parse`, `fetch_new`, `mark_seen` (Gmail SMTP and IMAP). |
| `app/inbox.py` | `handle(...)`: sign-up, edits and commands. |
| `app/handler.py` | Lambda entry point: builds the real dependencies, runs `inbox`, `tick` or `selftest`. |
| `tests/test_*.py` | One `unittest` file per module above. |
| `deploy/deploy.sh` | Creates or updates the tables, roles, function and schedules. |
| `config.example.json` | The owner's config with the key blanked, the example friends copy. |
| `SETUP.md` | The friend-facing sign-up guide, including the prompt for their own AI. |
| `OPERATIONS.md` | The owner's guide: deploy, invite, logs, costs. |

---

### Task 0: Branch and dev setup

**Files:** Create `app/__init__.py`, `tests/__init__.py`; modify `.gitignore`.

- [ ] **Step 1: Create the branch**

```bash
cd /d/DevProjects/ai-news && git checkout main && git pull -q && git checkout -b multiuser
```

- [ ] **Step 2: Install timezone data for local tests** (Windows Python ships none; verified 2026-09-29: `ZoneInfo('Asia/Kolkata')` fails without it)

```bash
python -m pip install --quiet tzdata && python -c "from zoneinfo import ZoneInfo; ZoneInfo('Asia/Kolkata'); print('tz ok')"
```
Expected: `tz ok`

- [ ] **Step 3: Package files and ignores**

Create empty `app/__init__.py` and `tests/__init__.py`. Append to `.gitignore`:
```
.env.deploy
build/
function.zip
```

- [ ] **Step 4: Commit**

```bash
git add app/__init__.py tests/__init__.py .gitignore
git commit -m "Start the multi-user version on its own branch"
```

---

### Task 1: Config checking

**Files:** Create `app/config.py`, `tests/test_config.py`.

**Interfaces:**
- Produces: `SECTIONS = ("ships", "industry", "research", "practice")`; `SOURCE_KEYS` (tuple); `validate_config(raw) -> (dict | None, list[str])`. A cleaned config always has every section in `counts` and every key in `sources` (as lists).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_config.py
import copy, unittest
from app.config import validate_config, SECTIONS

GOOD = {
    "name": "Priya", "gemini_key": "AIzaSyExampleExampleExample12345",
    "send_time": "08:15", "timezone": "Europe/London",
    "counts": {"ships": 10, "industry": 0, "research": 5, "practice": 5},
    "profile": "I am a data engineer. " * 20,
    "sources": {"rss": ["https://example.com/feed.xml"], "github_repos": ["apache/airflow"],
                "hn_queries": ["dbt"], "arxiv_categories": ["cs.DB"], "arxiv_keywords": ["query"]},
}

def bad(**changes):
    raw = copy.deepcopy(GOOD); raw.update(changes); return raw

class ValidateConfig(unittest.TestCase):
    def test_good_config_is_cleaned(self):
        cfg, errors = validate_config(GOOD)
        self.assertEqual(errors, [])
        self.assertEqual(set(cfg["counts"]), set(SECTIONS))
        self.assertEqual(cfg["sources"]["github_repos"], ["apache/airflow"])

    def test_missing_counts_and_sources_keys_default(self):
        raw = bad(counts={"ships": 3}, sources={"rss": []})
        cfg, errors = validate_config(raw)
        self.assertEqual(errors, [])
        self.assertEqual(cfg["counts"], {"ships": 3, "industry": 0, "research": 0, "practice": 0})
        self.assertEqual(cfg["sources"]["hn_queries"], [])

    def test_each_problem_is_named(self):
        cases = {
            "send_time": ("9:45", "send_time"), "timezone": ("Mars/Olympus", "timezone"),
            "gemini_key": ("PASTE_YOUR_GEMINI_KEY_HERE", "gemini_key"),
            "profile": ("too short", "profile"), "name": ("", "name"),
        }
        for key, (value, word) in cases.items():
            cfg, errors = validate_config(bad(**{key: value}))
            self.assertIsNone(cfg, key)
            self.assertTrue(any(word in e for e in errors), (key, errors))

    def test_quarter_hours_only(self):
        self.assertTrue(validate_config(bad(send_time="09:20"))[1])
        self.assertFalse(validate_config(bad(send_time="23:45"))[1])

    def test_counts_rules(self):
        self.assertTrue(validate_config(bad(counts={"ships": 0}))[1])       # all off
        self.assertTrue(validate_config(bad(counts={"ships": 21}))[1])      # too many
        self.assertTrue(validate_config(bad(counts={"ships": True}))[1])    # not a number
        self.assertTrue(validate_config(bad(counts={"news": 3}))[1])        # unknown section

    def test_unknown_top_level_key_is_rejected(self):
        raw = bad(); raw["sendtime"] = "09:00"
        cfg, errors = validate_config(raw)
        self.assertIsNone(cfg)
        self.assertIn("sendtime", errors[0])

    def test_source_shapes(self):
        self.assertTrue(validate_config(bad(sources={"github_repos": ["airflow"]}))[1])
        self.assertTrue(validate_config(bad(sources={"rss": ["example.com/feed"]}))[1])
        self.assertTrue(validate_config(bad(sources={"arxiv_categories": ["databases"]}))[1])
        self.assertTrue(validate_config(bad(sources={"rss": ["https://a.b/%d" % i for i in range(21)]}))[1])

    def test_not_an_object(self):
        self.assertEqual(validate_config([1, 2])[0], None)

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to see it fail**

Run: `python -m unittest tests.test_config -v`
Expected: `ModuleNotFoundError: No module named 'app.config'`

- [ ] **Step 3: Implement**

```python
# app/config.py
"""Checks a person's config.json. Returns it cleaned up, or says exactly what's wrong in
words a person can act on -- these errors are emailed back to them verbatim."""
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SECTIONS = ("ships", "industry", "research", "practice")
SOURCE_LIMITS = {"rss": 20, "github_repos": 25, "hn_queries": 8,
                 "arxiv_categories": 10, "arxiv_keywords": 20}
SOURCE_KEYS = tuple(SOURCE_LIMITS)
KEYS = {"name", "gemini_key", "send_time", "timezone", "counts", "profile", "sources"}
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
ARXIV_CAT_RE = re.compile(r"^[a-z-]+(\.[A-Za-z-]+)?$")
TIME_RE = re.compile(r"([01]\d|2[0-3]):(00|15|30|45)")


def _is_count(v):
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 20


def validate_config(raw):
    """(cleaned config, []) if usable, else (None, [plain-English problems])."""
    if not isinstance(raw, dict):
        return None, ["The file must be one JSON object, starting with { and ending with }."]
    extra, missing = sorted(set(raw) - KEYS), sorted(KEYS - set(raw))
    errors = []
    if extra:
        errors.append("Unknown key(s): %s. The only keys allowed are: %s."
                      % (", ".join(extra), ", ".join(sorted(KEYS))))
    if missing:
        errors.append("Missing key(s): %s." % ", ".join(missing))
    if errors:
        return None, errors

    name, key, t, tz = raw["name"], raw["gemini_key"], raw["send_time"], raw["timezone"]
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
        errors.append('"name" must be text, 1 to 60 characters.')
    if not isinstance(key, str) or len(key.strip()) < 20 or "PASTE" in key.upper():
        errors.append('"gemini_key" must be your Gemini API key from '
                      "https://aistudio.google.com/apikey (it usually starts with AIza).")
    if not (isinstance(t, str) and TIME_RE.fullmatch(t)):
        errors.append('"send_time" must look like "09:45": 24-hour clock, and the minutes '
                      "must be 00, 15, 30 or 45.")
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        errors.append('"timezone" must be a timezone name like "Asia/Kolkata", '
                      '"Europe/London" or "America/New_York" (not "IST" or "+05:30").')

    counts = raw["counts"]
    if not isinstance(counts, dict) or set(counts) - set(SECTIONS):
        errors.append('"counts" may only have these keys: %s.' % ", ".join(SECTIONS))
    else:
        for s in SECTIONS:
            if not _is_count(counts.get(s, 0)):
                errors.append('"counts.%s" must be a whole number from 0 to 20.' % s)
        if not any(_is_count(counts.get(s, 0)) and counts.get(s, 0) > 0 for s in SECTIONS):
            errors.append('"counts" must have at least one section above 0.')

    profile = raw["profile"]
    if not isinstance(profile, str) or not 300 <= len(profile.strip()) <= 6000:
        errors.append('"profile" must be text between 300 and 6000 characters '
                      "(roughly 50 to 1000 words).")

    sources = raw["sources"]
    if not isinstance(sources, dict) or set(sources) - set(SOURCE_KEYS):
        errors.append('"sources" may only have these keys: %s.' % ", ".join(SOURCE_KEYS))
    else:
        for k, limit in SOURCE_LIMITS.items():
            v = sources.get(k, [])
            if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
                errors.append('"sources.%s" must be a list of text values ([] if none).' % k)
                continue
            if len(v) > limit:
                errors.append('"sources.%s" has %d entries; the most allowed is %d.'
                              % (k, len(v), limit))
            for x in v:
                x = x.strip()
                if k == "rss" and not x.startswith(("http://", "https://")):
                    errors.append('"%s" in sources.rss must be a full feed URL starting '
                                  "with https://." % x)
                if k == "github_repos" and not REPO_RE.match(x):
                    errors.append('"%s" in sources.github_repos must look like "owner/name".' % x)
                if k == "arxiv_categories" and not ARXIV_CAT_RE.match(x):
                    errors.append('"%s" in sources.arxiv_categories must be a category code '
                                  'like "cs.DB".' % x)
    if errors:
        return None, errors
    return {"name": name.strip(), "gemini_key": key.strip(), "send_time": t, "timezone": tz,
            "counts": {s: counts.get(s, 0) for s in SECTIONS},
            "profile": profile.strip(),
            "sources": {k: [x.strip() for x in sources.get(k, [])] for k in SOURCE_KEYS}}, []
```

- [ ] **Step 4: Run to see it pass**

Run: `python -m unittest tests.test_config -v`
Expected: 8 tests, OK

- [ ] **Step 5: Commit**

```bash
git add app/config.py tests/test_config.py
git commit -m "Check a person's config.json and explain every problem in plain words"
```

---

### Task 2: Sources: shared AI news plus each person's own

**Files:** Create `app/sources.py`, `tests/test_sources.py`.

**Interfaces:**
- Consumes: `SOURCE_KEYS` from Task 1 (the source dict shape).
- Produces:
  - `parse_feed(name, xml, limit=15, summary_chars=400) -> list[item]`, where an item is `{"source","title","link","summary","date": datetime|None}`.
  - `discover_repos(cache_get, cache_put, token=None, sleep=time.sleep) -> list[str]`.
  - `gather(user_sources, discovered, seen, include_shared=True, now=None) -> (items, report)`, where `report` is `{label: "N items" | "error: ..."}` for the person's own sources only.
  - `check_sources(user_sources) -> report`.

Fetchers are copied from `digest.py` (`get`, `strip_html`, `parse_date`, `parse_feed`, `fetch_hn`, `fetch_release`, `fetch_hf_models`, `fetch_hf_papers`) with three changes:

1. They **raise** instead of printing and returning `[]`, so `gather` can report per source.
2. arXiv takes categories and plain keywords, matched case-insensitively. No regexes, because friends write these.
3. The Hugging Face trending models and daily papers get `date: None`. They're "trending now" lists, and their creation dates are often older than the 72-hour window, which is why they always came back empty before (seen 2026-09-28). The sent-links history prevents repeats instead.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_sources.py
import unittest
from datetime import datetime, timezone
from unittest import mock
from app import sources

RSS = b"""<rss><channel><item><title>Model &amp; Speed</title>
  <link>https://x.test/a</link><description>&lt;p&gt;Body   text&lt;/p&gt;</description>
  <pubDate>Wed, 03 Sep 2026 10:00:00 GMT</pubDate></item></channel></rss>"""
ATOM = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Atom Post</title>
  <link rel="alternate" href="https://x.test/b"/><summary>Sum</summary>
  <published>2026-09-03T10:00:00Z</published></entry></feed>"""
ARXIV = b"""<rss><channel>
<item><title>Faster Query Planning</title><link>https://arxiv.org/abs/1</link>
<description>arXiv:1 Announce Type: new Abstract: A query optimizer.</description></item>
<item><title>Birds</title><link>https://arxiv.org/abs/2</link>
<description>arXiv:2 Announce Type: new Abstract: About birds.</description></item>
<item><title>Old query paper</title><link>https://arxiv.org/abs/3</link>
<description>arXiv:3 Announce Type: replace Abstract: query.</description></item>
</channel></rss>"""

class Parsing(unittest.TestCase):
    def test_rss_and_atom(self):
        r = sources.parse_feed("R", RSS)[0]
        self.assertEqual((r["title"], r["summary"]), ("Model & Speed", "Body text"))
        self.assertEqual(r["date"].tzinfo is not None, True)
        self.assertEqual(sources.parse_feed("A", ATOM)[0]["link"], "https://x.test/b")

    def test_arxiv_keywords_plain_and_case_insensitive(self):
        with mock.patch.object(sources, "get", return_value=ARXIV):
            hits = sources.fetch_arxiv(["cs.DB"], ["QUERY"])
        self.assertEqual([h["link"] for h in hits], ["https://arxiv.org/abs/1"])  # new only
        self.assertEqual(hits[0]["summary"], "A query optimizer.")

class Gather(unittest.TestCase):
    NOW = datetime(2026, 9, 5, tzinfo=timezone.utc)
    OWN = {"rss": ["https://ok.test/feed", "https://dead.test/feed"], "github_repos": [],
           "hn_queries": [], "arxiv_categories": [], "arxiv_keywords": []}

    def fake_get(self, url, timeout=25, headers=None):
        if "dead.test" in url:
            raise OSError("connection refused")
        return RSS

    def test_own_sources_reported_and_failures_isolated(self):
        with mock.patch.object(sources, "get", side_effect=self.fake_get):
            items, report = sources.gather(self.OWN, [], set(), include_shared=False, now=self.NOW)
        self.assertEqual([i["link"] for i in items], ["https://x.test/a"])
        self.assertEqual(report["feed https://ok.test/feed"], "1 items")
        self.assertTrue(report["feed https://dead.test/feed"].startswith("error: OSError"))

    def test_seen_links_and_old_items_dropped(self):
        with mock.patch.object(sources, "get", side_effect=self.fake_get):
            items, _ = sources.gather(self.OWN, [], {"https://x.test/a"}, include_shared=False,
                                      now=self.NOW)
            self.assertEqual(items, [])
            late = datetime(2026, 9, 20, tzinfo=timezone.utc)   # item is 17 days old
            items, _ = sources.gather(self.OWN, [], set(), include_shared=False, now=late)
            self.assertEqual(items, [])

class Discover(unittest.TestCase):
    def test_uses_fresh_cache_without_network(self):
        with mock.patch("time.time", return_value=1000.0):
            got = sources.discover_repos(lambda k: (["a/b"], 900.0), lambda k, v: None)
        self.assertEqual(got, ["a/b"])

    def test_falls_back_to_stale_cache_when_search_fails(self):
        with mock.patch.object(sources, "get", side_effect=OSError("down")):
            got = sources.discover_repos(lambda k: (["a/b"], 0.0), lambda k, v: None,
                                         sleep=lambda s: None)
        self.assertEqual(got, ["a/b"])

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to see it fail**

Run: `python -m unittest tests.test_sources -v`
Expected: `ModuleNotFoundError: No module named 'app.sources'`

- [ ] **Step 3: Implement**

```python
# app/sources.py
"""Fetching news: the shared AI sources everyone gets, plus each person's own.
Fetchers raise on failure; gather() catches per source so one dead feed never stops a
digest, and reports how each of the person's own sources did."""
import json, re, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape

WINDOW_HOURS = 72
MAX_PER_SOURCE = 15
ARXIV_MAX = 30
UA = {"User-Agent": "Mozilla/5.0 (ai-news daily digest)"}

# Everyone's "AI included" part. Field-specific sources come from each person's config.
SHARED_FEEDS = [
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
    ("TechCrunch AI",   "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("The Verge AI",    "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    ("Cursor",          "https://cursor.com/changelog/rss.xml"),
    ("VS Code",         "https://code.visualstudio.com/feed.xml"),
    ("GitHub",          "https://github.blog/changelog/feed/"),
]
SHARED_HN = ["llm", "ai agents", "open source model", "anthropic", "openai", "gemini"]
SHARED_TOPICS = ["llm", "ai-agents", "rag", "vector-database", "llmops", "mlops",
                 "code-generation"]
REPOS_PER_TOPIC = 6
MUTED_REPOS = {"promptfoo/promptfoo", "ollama/ollama"}   # ship near-daily, rarely news


def get(url, timeout=25, headers=None):
    # One retry for truncated reads and timeouts; HTTP errors won't fix themselves.
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    for attempt in (1, 2):
        try:
            return urllib.request.urlopen(req, timeout=timeout).read()
        except urllib.error.HTTPError:
            raise
        except Exception:
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
    entries = root.findall(".//item") or root.findall(".//%sentry" % A)
    out = []
    for it in entries[:limit]:
        title = it.findtext("title") or it.findtext(A + "title") or ""
        link = it.findtext("link") or ""
        if not link:  # Atom puts the URL in an attribute
            el = it.find(A + "link[@rel='alternate']")
            if el is None:
                el = it.find(A + "link")
            link = el.get("href", "") if el is not None else ""
        summary = (it.findtext("description") or it.findtext(A + "summary")
                   or it.findtext(A + "content") or "")
        date = parse_date(it.findtext("pubDate") or it.findtext(A + "updated")
                          or it.findtext(A + "published"))
        if title and link:
            out.append({"source": name, "title": strip_html(title, 300), "link": link.strip(),
                        "summary": strip_html(summary, summary_chars), "date": date})
    return out


def feed_name(url):
    host = urllib.parse.urlparse(url).netloc
    return host[4:] if host.startswith("www.") else host


def fetch_arxiv(categories, keywords):
    """Today's arXiv listing for these categories, kept if the title or abstract contains
    any keyword (case-insensitive). The RSS listing, not the search API, which 406s
    Python's HTTP client (seen Sept 2026)."""
    papers = parse_feed("arXiv", get("https://rss.arxiv.org/rss/" + "+".join(categories),
                                     timeout=60), limit=None, summary_chars=3000)
    kws = [k.lower() for k in keywords]
    hits = [p for p in papers
            if "Announce Type: replace" not in p["summary"][:80]
            and any(k in (p["title"] + " " + p["summary"]).lower() for k in kws)]
    for p in hits:
        p["summary"] = re.sub(r"^.*?Abstract:\s*", "", p["summary"])[:400]
    return hits[:ARXIV_MAX]


def fetch_hn(query, now=None):
    now = now or datetime.now(timezone.utc)
    cutoff = int((now - timedelta(hours=WINDOW_HOURS)).timestamp())
    url = ("https://hn.algolia.com/api/v1/search?tags=story&hitsPerPage=10&query="
           + urllib.parse.quote(query) + "&numericFilters=created_at_i>%d,points>30" % cutoff)
    hits = json.loads(get(url))["hits"]
    return [{"source": "HN (%dpts)" % h["points"], "title": h.get("title") or "",
             "link": h.get("url") or "https://news.ycombinator.com/item?id=" + h["objectID"],
             "summary": "", "date": parse_date(h.get("created_at"))}
            for h in hits if h.get("title")]


def fetch_release(repo):
    """A repo's releases.atom. Titles are often bare tags, so prefix the repo name."""
    items = parse_feed(repo, get("https://github.com/%s/releases.atom" % repo), limit=4)
    for it in items:
        it["title"] = "%s %s" % (repo.split("/")[-1], it["title"])
        it["summary"] = it["summary"][:300]
        it["source"] = "release: " + repo
    return items


def fetch_hf_models():
    """Trending models on the Hub. date=None: trending lists are 'now' by definition,
    and creation dates are often older than the window (why this came back empty before)."""
    models = json.loads(get("https://huggingface.co/api/models?sort=trendingScore"
                            "&direction=-1&limit=30&full=false"))
    out = []
    for m in models:
        mid = m.get("modelId") or m.get("id")
        if mid:
            tags = [t for t in m.get("tags", []) if ":" not in t][:6]
            out.append({"source": "HF model (%s dl)" % m.get("downloads", 0),
                        "title": "New on the Hub: " + mid, "link": "https://huggingface.co/" + mid,
                        "summary": "pipeline: %s. tags: %s" % (m.get("pipeline_tag", "?"),
                                                               ", ".join(tags)),
                        "date": None})
    return out


def fetch_hf_papers():
    papers = json.loads(get("https://huggingface.co/api/daily_papers"))
    out = []
    for p in papers[:25]:
        paper = p.get("paper", {})
        if paper.get("id"):
            out.append({"source": "HF Papers (%s up)" % paper.get("upvotes", 0),
                        "title": strip_html(paper.get("title", ""), 300),
                        "link": "https://huggingface.co/papers/" + paper["id"],
                        "summary": strip_html(paper.get("summary", "")), "date": None})
    return out


def discover_repos(cache_get, cache_put, token=None, sleep=time.sleep):
    """Big, active repos in the shared AI topics, refreshed weekly. Falls back to the last
    good list, because losing discovery would quietly empty most of Shipped."""
    cached = cache_get("repos")   # (list, updated_epoch) or None
    if cached and cached[0] and time.time() - cached[1] < 7 * 86400:
        return cached[0]
    since = (datetime.now(timezone.utc) - timedelta(days=45)).strftime("%Y-%m-%d")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    found = set()
    for i, topic in enumerate(SHARED_TOPICS):
        if i and not token:
            sleep(7)   # unauthenticated search allows 10 requests a minute
        url = ("https://api.github.com/search/repositories?q="
               + urllib.parse.quote("topic:%s stars:>1500 pushed:>%s" % (topic, since))
               + "&sort=stars&order=desc&per_page=%d" % REPOS_PER_TOPIC)
        try:
            found.update(i["full_name"] for i in json.loads(get(url, headers=headers))["items"])
        except Exception as e:
            print("  ! discover %s: %s: %s" % (topic, type(e).__name__, e))
    if found:
        cache_put("repos", sorted(found))
        return sorted(found)
    return cached[0] if cached else []


def gather(user_sources, discovered, seen, include_shared=True, now=None):
    """(items, report). items: deduplicated by link, newer than WINDOW_HOURS (undated
    items always kept), minus links in `seen`. report: how each of the person's OWN
    sources did, keyed by a readable label."""
    now = now or datetime.now(timezone.utc)
    own = user_sources
    jobs = []   # (label or None for shared, fn)
    if include_shared:
        shared_repos = sorted({r for r in discovered if r.lower() not in MUTED_REPOS}
                              - set(own["github_repos"]))
        jobs += [(None, lambda n=n, u=u: parse_feed(n, get(u))) for n, u in SHARED_FEEDS]
        jobs += [(None, lambda q=q: fetch_hn(q, now)) for q in SHARED_HN]
        jobs += [(None, lambda r=r: fetch_release(r)) for r in shared_repos]
        jobs += [(None, fetch_hf_papers), (None, fetch_hf_models)]
    jobs += [("feed " + u, lambda u=u: parse_feed(feed_name(u), get(u))) for u in own["rss"]]
    jobs += [("repo " + r, lambda r=r: fetch_release(r)) for r in own["github_repos"]]
    jobs += [("hn " + q, lambda q=q: fetch_hn(q, now)) for q in own["hn_queries"]]
    if own["arxiv_categories"] and own["arxiv_keywords"]:
        jobs.append(("arxiv " + "+".join(own["arxiv_categories"]),
                     lambda: fetch_arxiv(own["arxiv_categories"], own["arxiv_keywords"])))

    def run(job):
        label, fn = job
        try:
            return label, fn(), None
        except Exception as e:
            return label, [], "%s: %s" % (type(e).__name__, e)

    with ThreadPoolExecutor(16) as ex:
        results = list(ex.map(run, jobs))
    cutoff = now - timedelta(hours=WINDOW_HOURS)
    items, report = {}, {}
    for label, batch, err in results:
        if label:
            report[label] = ("error: " + err[:150]) if err else "%d items" % len(batch)
        for it in batch:
            if (it["date"] and it["date"] < cutoff) or it["link"] in seen:
                continue
            items.setdefault(it["link"], it)
    return list(items.values()), report


def check_sources(user_sources):
    """Try each of a person's own sources once, for the sign-up reply."""
    return gather(user_sources, [], set(), include_shared=False)[1]
```

- [ ] **Step 4: Run to see it pass**

Run: `python -m unittest tests.test_sources -v`
Expected: 6 tests, OK

- [ ] **Step 5: Real-network check** (not part of the unit tests)

```bash
python -c "
from app.sources import check_sources
print(check_sources({'rss':['https://simonwillison.net/atom/everything/'],'github_repos':['apache/airflow'],'hn_queries':['dbt'],'arxiv_categories':['cs.DB'],'arxiv_keywords':['query']}))"
```
Expected: four labels. Each is `N items` or an `error:` with a readable reason. No traceback.

- [ ] **Step 6: Commit**

```bash
git add app/sources.py tests/test_sources.py
git commit -m "Gather shared AI news plus each person's own sources, with a per-source report"
```

---

### Task 3: Picking: instructions for any field, four sections, Gemini call

**Files:** Create `app/pick.py`, `tests/test_pick.py`.

**Interfaces:**
- Consumes: `SECTIONS` from `app.config`.
- Produces:
  - `build_prompt(profile, counts, items) -> str`.
  - `keep_known_links(result, links, counts) -> dict`: keeps only the sections that are on, known links only, trimmed to their counts.
  - `call_gemini(key, prompt, urlopen=..., sleep=...) -> dict`, which raises `GeminiError` with a readable message.
  - `check_key(key, urlopen=...) -> str | None` (the error text, or None if the key is fine).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_pick.py
import io, json, unittest, urllib.error
from app import pick

ITEMS = [{"source": "S", "title": "T1", "link": "https://x.test/1", "summary": "s"},
         {"source": "S", "title": "T2", "link": "https://x.test/2", "summary": "s"}]

def http_error(code):
    return urllib.error.HTTPError("u", code, "msg", {}, io.BytesIO(b"{}"))

def ok_response(obj):
    body = {"candidates": [{"content": {"parts": [{"text": json.dumps(obj)}]}}]}
    class R:
        def read(self): return json.dumps(body).encode()
    return R()

class BuildPrompt(unittest.TestCase):
    def test_only_enabled_sections_appear(self):
        p = pick.build_prompt("I build pipelines. {braces} ok",
                              {"ships": 5, "industry": 0, "research": 0, "practice": 3}, ITEMS)
        self.assertIn("SHIPS -- up to 5", p)
        self.assertIn("PRACTICE -- up to 3", p)
        self.assertNotIn("INDUSTRY --", p)
        self.assertNotIn('"research"', p)
        self.assertIn("{braces}", p)           # profile text is not treated as a template
        self.assertIn("https://x.test/2", p)
        self.assertNotIn("speech", p.lower())  # no owner-specific rules left

class KeepKnownLinks(unittest.TestCase):
    def test_trims_drops_and_removes_off_sections(self):
        result = {"ships": [{"link": "https://x.test/1"}, {"link": "https://made.up/"},
                            {"link": "https://x.test/2"}],
                  "research": [{"link": "https://x.test/1"}], "skipped_note": "n"}
        out = pick.keep_known_links(result, {"https://x.test/1", "https://x.test/2"},
                                    {"ships": 1, "industry": 0, "research": 0, "practice": 0})
        self.assertEqual(out["ships"], [{"link": "https://x.test/1"}])
        self.assertEqual(out["research"], [])
        self.assertEqual(out["skipped_note"], "n")

class CallGemini(unittest.TestCase):
    def test_falls_back_to_second_model(self):
        calls = []
        def urlopen(req, timeout=None):
            calls.append(req.full_url)
            if "flash-latest" in req.full_url:
                raise http_error(503)
            return ok_response({"ships": []})
        out = pick.call_gemini("k", "p", urlopen=urlopen, sleep=lambda s: None)
        self.assertEqual(out, {"ships": []})
        self.assertEqual(sum("flash-latest" in c for c in calls), 2)   # retried once

    def test_rejected_key_raises_immediately(self):
        def urlopen(req, timeout=None): raise http_error(403)
        with self.assertRaisesRegex(pick.GeminiError, "rejected"):
            pick.call_gemini("k", "p", urlopen=urlopen, sleep=lambda s: None)

    def test_all_busy_raises_unavailable(self):
        def urlopen(req, timeout=None): raise http_error(503)
        with self.assertRaisesRegex(pick.GeminiError, "unavailable"):
            pick.call_gemini("k", "p", urlopen=urlopen, sleep=lambda s: None)

class CheckKey(unittest.TestCase):
    def test_bad_key_explained_network_glitch_forgiven(self):
        def bad(req, timeout=None): raise http_error(400)
        def glitch(req, timeout=None): raise TimeoutError()
        self.assertIn("Google", pick.check_key("k", urlopen=bad))
        self.assertIsNone(pick.check_key("k", urlopen=glitch))

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to see it fail**

Run: `python -m unittest tests.test_pick -v`
Expected: `ModuleNotFoundError: No module named 'app.pick'`

- [ ] **Step 3: Implement**

```python
# app/pick.py
"""The picking step: same rules for everyone, each person's profile and counts, their
own Gemini key. The owner's old speech/voice priorities now live in the owner's profile."""
import json, time, urllib.error, urllib.request
from .config import SECTIONS

MODELS = ("gemini-flash-latest", "gemini-3.6-flash")   # newest Flash, then an older one
API = "https://generativelanguage.googleapis.com/v1beta/models"

RULES = {
    "ships": ("SHIPS", "model|tool|library|service|infra",
              "Anything in their field, AI included, that exists NOW and can be downloaded, "
              "installed, called or upgraded today, rather than only written about: models, "
              "libraries, frameworks, databases, developer tools, cloud services, APIs, pricing "
              "and quota changes. A version bump only earns a slot if it changes something "
              "worth knowing: a real feature, a breaking change, a meaningful speedup. Routine "
              "patch releases and dependency bumps do not count. If the title is just a name "
              "and a version number and the notes don't spell out a real change, drop it, "
              "even for a repo this person watches closely."),
    "industry": ("INDUSTRY", "funding|people|business|policy|opinion",
                 "The business and people side of their field and of AI: funding rounds, "
                 "acquisitions, executive moves, company strategy, partnerships, policy and "
                 "regulation, and commentary worth reading. Anything that shipped a usable "
                 "thing goes in SHIPS instead. Prefer items touching the companies and tools "
                 "in their stack."),
    "research": ("RESEARCH", "paper|benchmark|eval|safety",
                 "Papers, benchmarks and evaluations. Ideas rather than artifacts."),
    "practice": ("PRACTICE", "writeup|postmortem|migration|howto",
                 "How teams actually do the work: engineering blog write-ups, postmortems, "
                 "migration stories, lessons learned, and benchmarks run by practitioners. "
                 "Not papers (RESEARCH) and not announcements (SHIPS, INDUSTRY)."),
}

TEMPLATE = """You are curating a daily news digest for one specific engineer. Here is who they are, in their own words:

<<PROFILE>>

Below are <<N>> items from the last few days. Sort the best of them into these lists:

<<RULES>>

Judge relevance by their actual work and interests as described above, not general newsworthiness. Their stated priorities and dislikes override everything else here.

Drop outage chatter entirely. Each item goes in at most one list. Return fewer than the limits if the rest do not clear the bar -- never pad a list to fill it.

Copy "link" and "source" verbatim from the item you picked. Do not invent a URL.

Ground every word of "what" and "why" in the text actually given for that item. Release notes here are truncated, so you will often not know what changed -- say so plainly or drop the item. Never invent a capability, a benchmark or a connection to their work that the text does not support: a made-up reason is worse than a missing item. Tag by what the thing IS.

Return JSON only, with exactly these keys:
<<SCHEMA>>

"what" is 1-2 plain sentences on what the thing actually is.
"why" is one concrete sentence on why it matters to THIS person. For an INDUSTRY item with no real link to their work, say why it matters to their field instead.

ITEMS:
<<ITEMS>>"""


class GeminiError(Exception):
    pass


def build_prompt(profile, counts, items):
    on = [s for s in SECTIONS if counts.get(s, 0) > 0]
    rules = "\n\n".join("%s -- up to %d items. %s" % (RULES[s][0], counts[s], RULES[s][2])
                        for s in on)
    schema = "{" + ", ".join(
        '"%s": [{"title": "...", "link": "...", "source": "...", "what": "...", '
        '"why": "...", "tag": "%s"}]' % (s, RULES[s][1]) for s in on)
    schema += ', "skipped_note": "one short line on what else was in the pile and why it did not make it"}'
    listing = "\n".join("[%d] (%s) %s\n    %s\n    %s"
                        % (i, it["source"], it["title"], it["link"], it["summary"][:280])
                        for i, it in enumerate(items))
    # Plain replace, not str.format: profiles and item text may contain { }.
    return (TEMPLATE.replace("<<PROFILE>>", profile).replace("<<N>>", str(len(items)))
            .replace("<<RULES>>", rules).replace("<<SCHEMA>>", schema)
            .replace("<<ITEMS>>", listing))


def keep_known_links(result, links, counts):
    """Only sections that are on, only links we actually fetched, at most `counts` each."""
    for s in SECTIONS:
        picked = result.get(s) if isinstance(result.get(s), list) else []
        result[s] = [e for e in picked
                     if isinstance(e, dict) and e.get("link") in links][:counts.get(s, 0)]
    return result


def _text(resp):
    try:
        return resp["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        raise ValueError("no text in the answer (blocked or empty)")


def call_gemini(key, prompt, urlopen=urllib.request.urlopen, sleep=time.sleep):
    """Picks as a dict. Each model gets two tries; busy/overloaded moves on to the next.
    Raises GeminiError when nothing worked -- the tick retries 15 minutes later."""
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                       "generationConfig": {"responseMimeType": "application/json",
                                            "temperature": 0.2}}).encode()
    last = "no attempt made"
    for model in MODELS:
        for attempt in (1, 2):
            req = urllib.request.Request("%s/%s:generateContent" % (API, model), data=body,
                                         headers={"Content-Type": "application/json",
                                                  "x-goog-api-key": key})
            try:
                return json.loads(_text(json.loads(urlopen(req, timeout=240).read())))
            except urllib.error.HTTPError as e:
                if e.code in (400, 401, 403):
                    raise GeminiError("Google rejected the Gemini key (HTTP %d). Create a new "
                                      "one at https://aistudio.google.com/apikey and send an "
                                      "updated config.json." % e.code)
                last = "HTTP %d from %s" % (e.code, model)
                if e.code in (429, 500, 502, 503, 504) and attempt == 1:
                    sleep(20)
                    continue
                break
            except (TimeoutError, urllib.error.URLError, ConnectionError) as e:
                last = "%s from %s" % (type(e).__name__, model)
                if attempt == 1:
                    sleep(20)
                    continue
            except ValueError as e:   # not JSON, or no text
                last = "unusable answer from %s: %s" % (model, e)
                break
    raise GeminiError("Gemini unavailable (%s)" % last)


def check_key(key, urlopen=urllib.request.urlopen):
    """None if the key works (or Google can't be reached right now); else what to fix."""
    req = urllib.request.Request(API, headers={"x-goog-api-key": key})
    try:
        urlopen(req, timeout=20).read()
        return None
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403):
            return ("Google rejected this Gemini key (HTTP %d). Copy it again from "
                    "https://aistudio.google.com/apikey -- the whole key, no spaces." % e.code)
        return None
    except Exception:
        return None
```

- [ ] **Step 4: Run to see it pass**

Run: `python -m unittest tests.test_pick -v`
Expected: 6 tests, OK

- [ ] **Step 5: Commit**

```bash
git add app/pick.py tests/test_pick.py
git commit -m "Picking rules for any field with four sections, on each person's own Gemini key"
```

---

### Task 4: The email itself

**Files:** Create `app/render.py`, `tests/test_render.py`.

**Interfaces:**
- Consumes: `SECTIONS`.
- Produces: `render(digest, date_str, name, signup_address) -> str` (HTML); `failure_note(name, date_str, reason, signup_address) -> str` (HTML).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_render.py
import unittest
from app.render import render, failure_note

ENTRY = {"title": "<script>x</script>", "link": "https://x.test/c", "source": "S",
         "what": "w", "why": "y", "tag": "writeup"}

class Render(unittest.TestCase):
    def test_sections_escape_and_footer(self):
        html = render({"ships": [], "industry": [], "research": [], "practice": [ENTRY],
                       "skipped_note": "n"}, "Tuesday 29 September", "Priya", "me+ai@gmail.com")
        self.assertNotIn("<script>x</script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("Practice", html)
        self.assertNotIn(">Shipped<", html)          # empty sections are left out
        self.assertIn("1 practice", html)
        self.assertIn("me+ai@gmail.com", html)
        self.assertIn("pause", html)

    def test_nothing_picked(self):
        html = render({"ships": []}, "d", "P", "a@b.c")
        self.assertIn("Nothing cleared the bar", html)

    def test_failure_note_explains(self):
        html = failure_note("Priya", "Tue", "Gemini unavailable (HTTP 503)", "a@b.c")
        self.assertIn("Gemini unavailable", html)
        self.assertIn("a@b.c", html)

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to see it fail**

Run: `python -m unittest tests.test_render -v`
Expected: `ModuleNotFoundError: No module named 'app.render'`

- [ ] **Step 3: Implement**

```python
# app/render.py
"""The digest email and the rare 'no digest today' note. Every value is escaped."""
from html import escape
from .config import SECTIONS

TITLES = {"ships": ("Shipped", "Out now -- usable today."),
          "industry": ("Industry", "Money, people, deals, policy."),
          "research": ("Research", "Papers and benchmarks."),
          "practice": ("Practice", "How teams actually do the work.")}
TAG_COLOR = {"model": "#7c3aed", "tool": "#0d9488", "library": "#0891b2", "service": "#2563eb",
             "infra": "#64748b", "funding": "#15803d", "people": "#be185d",
             "business": "#1d4ed8", "policy": "#b45309", "opinion": "#6b7280",
             "paper": "#4f46e5", "benchmark": "#16a34a", "eval": "#16a34a", "safety": "#dc2626",
             "writeup": "#ea580c", "postmortem": "#dc2626", "migration": "#0d9488",
             "howto": "#7c3aed"}
SANS = "-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif"


def _section(key, entries):
    if not entries:
        return ""
    heading, blurb = TITLES[key]
    rows = []
    for i, it in enumerate(entries, 1):
        tag = it.get("tag", "")
        rows.append(
            '<div style="margin:0 0 24px;padding:0 0 20px;border-bottom:1px solid #e8e8ea">'
            '<div style="font:600 11px/1.4 %s;letter-spacing:.09em;text-transform:uppercase;'
            'color:%s;margin-bottom:6px">%02d &middot; %s &middot; %s</div>'
            '<a href="%s" style="font:600 17px/1.4 %s;color:#111827;text-decoration:none">%s</a>'
            '<p style="font:15px/1.6 %s;color:#374151;margin:8px 0 0">%s</p>'
            '<p style="font:15px/1.6 %s;color:#6b21a8;margin:6px 0 0">'
            '<strong style="color:#581c87">Why you care:</strong> %s</p></div>'
            % (SANS, TAG_COLOR.get(tag, "#64748b"), i, escape(tag),
               escape(it.get("source", "")), escape(it.get("link", "#")), SANS,
               escape(it.get("title", "")), SANS, escape(it.get("what", "")), SANS,
               escape(it.get("why", ""))))
    return ('<div style="font:700 13px/1.3 %s;letter-spacing:.1em;text-transform:uppercase;'
            'color:#111827;margin:32px 0 3px">%s</div>'
            '<div style="font:13px/1.5 %s;color:#9ca3af;margin:0 0 18px">%s</div>%s'
            % (SANS, escape(heading), SANS, escape(blurb), "".join(rows)))


def _page(title, inner):
    return ('<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>%s</title></head><body style="margin:0;background:#f6f6f7;padding:24px 12px">'
            '<div style="max-width:660px;margin:0 auto;background:#fff;border-radius:12px;'
            'padding:28px">%s</div></body></html>' % (escape(title), inner))


def _footer(signup_address):
    a = escape(signup_address)
    return ('<p style="font:13px/1.6 %s;color:#9ca3af;margin:26px 0 0">To change your feed, '
            'email a new config.json to %s. To pause, resume or stop, email %s with the '
            'subject <b>pause</b>, <b>resume</b> or <b>stop</b>. Subject <b>config</b> sends '
            'you your current settings.</p>' % (SANS, a, a))


def render(digest, date_str, name, signup_address):
    body = "".join(_section(s, digest.get(s) or []) for s in SECTIONS)
    counts = ", ".join("%d %s" % (len(digest.get(s) or []), TITLES[s][0].lower())
                       for s in SECTIONS if digest.get(s))
    if not body:
        body = '<p style="font:15px %s;color:#6b7280">Nothing cleared the bar today.</p>' % SANS
    note = digest.get("skipped_note", "")
    inner = ('<div style="font:700 23px/1.2 %s;color:#111827">Your digest, %s</div>'
             '<div style="font:14px %s;color:#9ca3af;margin:5px 0 0">%s &middot; %s</div>%s'
             '<p style="font:13px/1.6 %s;color:#9ca3af;margin:24px 0 0">%s</p>%s'
             % (SANS, escape(name), SANS, escape(date_str), escape(counts or "nothing today"),
                body, SANS, escape(note), _footer(signup_address)))
    return _page("Your digest -- " + date_str, inner)


def failure_note(name, date_str, reason, signup_address):
    inner = ('<p style="font:15px/1.6 %s;color:#111827">Hi %s, there is no digest for %s. '
             'It kept failing for 3 hours after your send time. The last error was:</p>'
             '<p style="font:14px/1.6 monospace;color:#b91c1c">%s</p>'
             '<p style="font:15px/1.6 %s;color:#374151">If it mentions your key, create a new '
             'one at https://aistudio.google.com/apikey and email an updated config.json to '
             '%s. Otherwise it is usually Gemini being busy, and tomorrow will be fine.</p>%s'
             % (SANS, escape(name), escape(date_str), escape(reason), SANS,
                escape(signup_address), _footer(signup_address)))
    return _page("No digest today", inner)
```

- [ ] **Step 4: Run to see it pass**

Run: `python -m unittest tests.test_render -v`
Expected: 3 tests, OK

- [ ] **Step 5: Commit**

```bash
git add app/render.py tests/test_render.py
git commit -m "Render the four-section digest and the no-digest-today note"
```

---

### Task 5: Storage

**Files:** Create `app/store.py`, `tests/test_store.py`.

**Interfaces:**
- Produces two classes with identical methods: `get_user(email) -> dict|None`, `put_user(user)`, `delete_user(email)` (also deletes their sent links), `all_users() -> list[dict]`, `sent_links(email) -> set[str]`, `add_sent(email, links)`, `cache_get(name) -> (value, updated_epoch)|None`, `cache_put(name, value)`.
- A user dict looks like `{"email", "config", "status": "active"|"paused", "created", "last_sent_date": "YYYY-MM-DD"|None, "source_report": {}, "last_error": str (optional)}`.
- DynamoDB tables: `ainews-users` (hash `email`, attribute `data` = the user as JSON text), `ainews-sent` (hash `email`, range `link`, `expires` epoch with TTL), `ainews-cache` (hash `name`, `value` JSON text, `updated` epoch).

- [ ] **Step 1: Write the failing tests** (the `MemoryStore` contract. `DynamoStore` is checked for real by `selftest` in Task 10.)

```python
# tests/test_store.py
import unittest
from app.store import MemoryStore

class MemoryStoreContract(unittest.TestCase):
    def test_round_trip_and_isolation(self):
        s = MemoryStore()
        s.put_user({"email": "a@x.test", "status": "active", "config": {"n": 1}})
        u = s.get_user("a@x.test"); u["status"] = "paused"          # a copy, not the stored one
        self.assertEqual(s.get_user("a@x.test")["status"], "active")
        s.add_sent("a@x.test", ["l1", "l2"])
        self.assertEqual(s.sent_links("a@x.test"), {"l1", "l2"})
        s.delete_user("a@x.test")
        self.assertIsNone(s.get_user("a@x.test"))
        self.assertEqual(s.sent_links("a@x.test"), set())

    def test_cache(self):
        s = MemoryStore()
        self.assertIsNone(s.cache_get("repos"))
        s.cache_put("repos", ["a/b"])
        self.assertEqual(s.cache_get("repos")[0], ["a/b"])

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to see it fail**

Run: `python -m unittest tests.test_store -v`
Expected: `ModuleNotFoundError: No module named 'app.store'`

- [ ] **Step 3: Implement**

```python
# app/store.py
"""Where people and their history live. MemoryStore for tests; DynamoStore on AWS. Users
are stored as one JSON text attribute, which sidesteps DynamoDB's Decimal conversion."""
import json, time

SENT_DAYS = 60


def _copy(x):
    return json.loads(json.dumps(x))


class MemoryStore:
    def __init__(self):
        self.users, self.sent, self.cache = {}, {}, {}

    def get_user(self, email):
        return _copy(self.users[email]) if email in self.users else None

    def put_user(self, user):
        self.users[user["email"]] = _copy(user)

    def delete_user(self, email):
        self.users.pop(email, None)
        self.sent.pop(email, None)

    def all_users(self):
        return [_copy(u) for u in self.users.values()]

    def sent_links(self, email):
        return set(self.sent.get(email, ()))

    def add_sent(self, email, links):
        self.sent.setdefault(email, set()).update(links)

    def cache_get(self, name):
        return self.cache.get(name)

    def cache_put(self, name, value):
        self.cache[name] = (_copy(value), time.time())


class DynamoStore:
    def __init__(self, prefix="ainews"):
        import boto3
        db = boto3.resource("dynamodb")
        self.users = db.Table(prefix + "-users")
        self.sent = db.Table(prefix + "-sent")
        self.cache = db.Table(prefix + "-cache")

    def get_user(self, email):
        item = self.users.get_item(Key={"email": email}).get("Item")
        return json.loads(item["data"]) if item else None

    def put_user(self, user):
        self.users.put_item(Item={"email": user["email"], "data": json.dumps(user)})

    def delete_user(self, email):
        links = self.sent_links(email)
        with self.sent.batch_writer() as b:
            for link in links:
                b.delete_item(Key={"email": email, "link": link})
        self.users.delete_item(Key={"email": email})

    def all_users(self):
        out, kw = [], {}
        while True:
            page = self.users.scan(**kw)
            out += [json.loads(i["data"]) for i in page["Items"]]
            if "LastEvaluatedKey" not in page:
                return out
            kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def sent_links(self, email):
        from boto3.dynamodb.conditions import Key
        links, kw = set(), {"KeyConditionExpression": Key("email").eq(email),
                            "ProjectionExpression": "link"}
        while True:
            page = self.sent.query(**kw)
            links |= {i["link"] for i in page["Items"]}
            if "LastEvaluatedKey" not in page:
                return links
            kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def add_sent(self, email, links):
        expires = int(time.time()) + SENT_DAYS * 86400
        with self.sent.batch_writer() as b:
            for link in set(links):
                b.put_item(Item={"email": email, "link": link, "expires": expires})

    def cache_get(self, name):
        item = self.cache.get_item(Key={"name": name}).get("Item")
        return (json.loads(item["value"]), float(item["updated"])) if item else None

    def cache_put(self, name, value):
        self.cache.put_item(Item={"name": name, "value": json.dumps(value),
                                  "updated": int(time.time())})
```

- [ ] **Step 4: Run to see it pass**

Run: `python -m unittest tests.test_store -v`
Expected: 2 tests, OK

- [ ] **Step 5: Commit**

```bash
git add app/store.py tests/test_store.py
git commit -m "Store people, sent links and the discovery cache (memory for tests, DynamoDB on AWS)"
```

---

### Task 6: Who is due, and the send loop

**Files:** Create `app/tick.py`, `tests/test_tick.py`.

**Interfaces:**
- Consumes: the user dict shape from Task 5.
- Produces:
  - `due_state(user, now_utc) -> "send" | "give_up" | None`.
  - `mark_new_user(user, now_utc) -> user`: a sign-up after today's send time starts tomorrow.
  - `first_digest_text(user, now_utc) -> str`, e.g. `"today at 09:45 (Europe/London)"` or `"tomorrow at 09:45 (Europe/London)"`.
  - `run_tick(store, now_utc, make_digest, send, note, log=print) -> int` (the number of digests sent). It takes `make_digest(user) -> (html, links, report)` (which raises on failure), `send(email, subject, html)` and `note(user) -> html`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_tick.py
import unittest
from datetime import datetime, timezone
from app.store import MemoryStore
from app import tick

def user(email="a@x.test", tz="Asia/Kolkata", at="09:45", status="active", last=None):
    return {"email": email, "status": status, "last_sent_date": last,
            "config": {"name": "A", "timezone": tz, "send_time": at}}

def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)

class DueState(unittest.TestCase):
    def test_before_at_and_after_send_time(self):
        u = user()   # 09:45 IST = 04:15 UTC
        self.assertIsNone(tick.due_state(u, utc(2026, 9, 29, 4, 0)))
        self.assertEqual(tick.due_state(u, utc(2026, 9, 29, 4, 15)), "send")
        self.assertEqual(tick.due_state(u, utc(2026, 9, 29, 7, 14)), "send")
        self.assertEqual(tick.due_state(u, utc(2026, 9, 29, 7, 15)), "give_up")

    def test_already_sent_or_paused(self):
        self.assertIsNone(tick.due_state(user(last="2026-09-29"), utc(2026, 9, 29, 5, 0)))
        self.assertIsNone(tick.due_state(user(status="paused"), utc(2026, 9, 29, 5, 0)))

    def test_other_timezone(self):
        u = user(tz="America/New_York", at="07:00")   # 11:00 UTC in September (EDT)
        self.assertIsNone(tick.due_state(u, utc(2026, 9, 29, 10, 45)))
        self.assertEqual(tick.due_state(u, utc(2026, 9, 29, 11, 0)), "send")

    def test_late_send_time_does_not_give_up_on_yesterday(self):
        u = user(tz="Europe/London", at="23:45")      # BST: 22:45 UTC
        # 00:30 local next day: yesterday is silently skipped, today not due yet
        self.assertIsNone(tick.due_state(u, utc(2026, 9, 29, 23, 30)))

class NewUsers(unittest.TestCase):
    def test_signup_after_send_time_starts_tomorrow(self):
        u = tick.mark_new_user(user(), utc(2026, 9, 29, 6, 0))       # 11:30 IST
        self.assertEqual(u["last_sent_date"], "2026-09-29")
        self.assertIn("tomorrow at 09:45", tick.first_digest_text(u, utc(2026, 9, 29, 6, 0)))
        u = tick.mark_new_user(user(), utc(2026, 9, 29, 3, 0))       # 08:30 IST
        self.assertIsNone(u["last_sent_date"])
        self.assertIn("today at 09:45", tick.first_digest_text(u, utc(2026, 9, 29, 3, 0)))

class RunTick(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.store.put_user(user("ok@x.test"))
        self.store.put_user(user("broken@x.test"))
        self.sent = []

    def make_digest(self, u):
        if u["email"] == "broken@x.test":
            raise RuntimeError("Gemini unavailable (HTTP 503)")
        return "<html>", ["https://x.test/1"], {"feed a": "3 items"}

    def run(self, now):
        return tick.run_tick(self.store, now, self.make_digest,
                             lambda e, s, h: self.sent.append((e, s)),
                             lambda u: "<note>", log=lambda *a: None)

    def test_one_failure_does_not_stop_others_and_gives_up_once(self):
        self.assertEqual(self.run(utc(2026, 9, 29, 4, 15)), 1)
        self.assertEqual([e for e, _ in self.sent], ["ok@x.test"])
        self.assertEqual(self.store.sent_links("ok@x.test"), {"https://x.test/1"})
        self.assertIn("503", self.store.get_user("broken@x.test")["last_error"])
        self.run(utc(2026, 9, 29, 4, 30))                  # retry: still failing, nothing new
        self.assertEqual(len(self.sent), 1)
        self.run(utc(2026, 9, 29, 7, 15))                  # 3h later: one give-up note
        self.run(utc(2026, 9, 29, 7, 30))                  # and not again
        self.assertEqual([e for e, _ in self.sent], ["ok@x.test", "broken@x.test"])

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to see it fail**

Run: `python -m unittest tests.test_tick -v`
Expected: `ModuleNotFoundError: No module named 'app.tick'`

- [ ] **Step 3: Implement**

```python
# app/tick.py
"""Who is due, and the send loop. Pure logic; the network and AWS are passed in."""
from datetime import timedelta
from zoneinfo import ZoneInfo

GIVE_UP_HOURS = 3


def _moment(cfg, now_utc):
    local = now_utc.astimezone(ZoneInfo(cfg["timezone"]))
    h, m = (int(x) for x in cfg["send_time"].split(":"))
    return local, local.replace(hour=h, minute=m, second=0, microsecond=0)


def due_state(user, now_utc):
    if user.get("status") != "active":
        return None
    local, at = _moment(user["config"], now_utc)
    if user.get("last_sent_date") == local.date().isoformat() or local < at:
        return None
    return "give_up" if local >= at + timedelta(hours=GIVE_UP_HOURS) else "send"


def mark_new_user(user, now_utc):
    local, at = _moment(user["config"], now_utc)
    user["last_sent_date"] = local.date().isoformat() if local >= at else None
    return user


def first_digest_text(user, now_utc):
    local, _ = _moment(user["config"], now_utc)
    day = "tomorrow" if user.get("last_sent_date") == local.date().isoformat() else "today"
    return "%s at %s (%s)" % (day, user["config"]["send_time"], user["config"]["timezone"])


def run_tick(store, now_utc, make_digest, send, note, log=print):
    sent = 0
    for user in store.all_users():
        state = due_state(user, now_utc)
        if not state:
            continue
        email = user["email"]
        local, _ = _moment(user["config"], now_utc)
        today = local.date().isoformat()
        try:
            if state == "give_up":
                send(email, "No digest today (%s)" % local.strftime("%d %b"), note(user))
                user["last_sent_date"] = today
                store.put_user(user)
                continue
            html, links, report = make_digest(user)
            send(email, "Your digest, %s" % local.strftime("%A %d %B"), html)
        except Exception as e:
            user["last_error"] = ("%s: %s" % (type(e).__name__, e))[:300]
            log("  ! %s: %s" % (email, user["last_error"]))
            store.put_user(user)
            continue
        # Mark sent before recording links, so a storage hiccup can't cause a resend.
        user["last_sent_date"] = today
        user["source_report"] = report
        user.pop("last_error", None)
        store.put_user(user)
        try:
            store.add_sent(email, links)
        except Exception as e:
            log("  ! %s: could not record sent links: %s" % (email, e))
        sent += 1
    return sent
```

- [ ] **Step 4: Run to see it pass**

Run: `python -m unittest tests.test_tick -v`
Expected: 6 tests, OK

- [ ] **Step 5: Commit**

```bash
git add app/tick.py tests/test_tick.py
git commit -m "Work out who is due in their own timezone, retry, and give up once after 3 hours"
```

---

### Task 7: Gmail: sending, reading, parsing

**Files:** Create `app/mailer.py`, `tests/test_mailer.py`.

**Interfaces:**
- Produces:
  - `send(gmail, password, to, subject, html=None, text=None, attachment=None, reply_to=None)`, where `attachment` is JSON text sent as `config.json`.
  - `parse(raw_bytes) -> {"sender", "subject", "attachment": str|None, "auto": bool}`. The attachment has any byte-order mark removed and any ``` fences stripped.
  - `fetch_new(gmail, password, to_address) -> (imap, [(uid, raw_bytes)])`.
  - `mark_seen(imap, uid)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_mailer.py
import unittest
from email.message import EmailMessage
from app import mailer

def raw(attach_bytes=None, maintype="application", subtype="octet-stream",
        filename="config.json", headers=None):
    m = EmailMessage()
    m["From"] = "Priya <Priya@Example.com>"; m["To"] = "me+ai@gmail.com"; m["Subject"] = "join ABC"
    for k, v in (headers or {}).items():
        m[k] = v
    m.set_content("hi")
    if attach_bytes is not None:
        m.add_attachment(attach_bytes, maintype=maintype, subtype=subtype, filename=filename)
    return m.as_bytes()

class Parse(unittest.TestCase):
    def test_octet_stream_with_bom_and_fences(self):
        body = "﻿```json\n{\"name\": \"P\"}\n```".encode("utf-8")
        p = mailer.parse(raw(body))
        self.assertEqual(p["sender"], "priya@example.com")
        self.assertEqual(p["subject"], "join ABC")
        self.assertEqual(p["attachment"].strip(), '{"name": "P"}')
        self.assertFalse(p["auto"])

    def test_no_json_attachment(self):
        self.assertIsNone(mailer.parse(raw(b"x", filename="cv.pdf"))["attachment"])
        self.assertIsNone(mailer.parse(raw())["attachment"])

    def test_auto_replies_flagged(self):
        self.assertTrue(mailer.parse(raw(headers={"Auto-Submitted": "auto-replied"}))["auto"])

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to see it fail**

Run: `python -m unittest tests.test_mailer -v`
Expected: `ModuleNotFoundError: No module named 'app.mailer'`

- [ ] **Step 3: Implement**

```python
# app/mailer.py
"""All email goes through the owner's Gmail: SMTP on 465 to send (Lambda may use 465;
only port 25 is blocked), IMAP to read mail sent to the sign-up '+' address."""
import email, imaplib, re, smtplib
from email import policy
from email.message import EmailMessage
from email.utils import parseaddr


def send(gmail, password, to, subject, html=None, text=None, attachment=None, reply_to=None):
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, gmail, to
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(text or "This email is HTML. Open it in a client that shows HTML.")
    if html:
        msg.add_alternative(html, subtype="html")
    if attachment is not None:
        msg.add_attachment(attachment.encode("utf-8"), maintype="application",
                           subtype="json", filename="config.json")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=45) as s:
        s.login(gmail, password)
        s.send_message(msg)


def _clean(text):
    text = text.lstrip("﻿").strip()
    fenced = re.fullmatch(r"```[a-zA-Z]*\s*(.*?)\s*```", text, re.S)
    return fenced.group(1) if fenced else text


def parse(raw_bytes):
    msg = email.message_from_bytes(raw_bytes, policy=policy.default)
    sender = parseaddr(str(msg.get("From", "")))[1].lower()
    auto = (str(msg.get("Auto-Submitted", "no")).lower() != "no"
            or "mailer-daemon" in sender or msg.get("X-Autoreply") is not None)
    attachment = None
    for part in msg.iter_attachments():
        if (part.get_filename() or "").lower().endswith(".json"):
            content = part.get_content()
            if isinstance(content, bytes):
                content = content.decode("utf-8", "replace")
            attachment = _clean(content)
            break
    return {"sender": sender, "subject": str(msg.get("Subject", "")).strip(),
            "attachment": attachment, "auto": auto}


def fetch_new(gmail, password, to_address):
    """Unread mail addressed to to_address, fetched without marking it read."""
    imap = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    imap.login(gmail, password)
    imap.select("INBOX")
    _, data = imap.uid("search", None, "UNSEEN", "TO", '"%s"' % to_address)
    out = []
    for uid in data[0].split():
        _, parts = imap.uid("fetch", uid, "(BODY.PEEK[])")
        out.append((uid, parts[0][1]))
    return imap, out


def mark_seen(imap, uid):
    imap.uid("store", uid, "+FLAGS", "(\\Seen)")
```

- [ ] **Step 4: Run to see it pass**

Run: `python -m unittest tests.test_mailer -v`
Expected: 3 tests, OK

- [ ] **Step 5: Commit**

```bash
git add app/mailer.py tests/test_mailer.py
git commit -m "Send and read mail through Gmail, and pull config.json out of any attachment shape"
```

---

### Task 8: Handling sign-ups, edits and commands

**Files:** Create `app/inbox.py`, `tests/test_inbox.py`.

**Interfaces:**
- Consumes: `validate_config`; `mark_new_user`, `first_digest_text`; the store.
- Produces: `handle(sender, subject, attachment, store, invite_code, check_key, check_sources, now_utc, signup_address) -> (subject, text, attachment_json|None) | None`. It returns None when the email came from a stranger with no invite code (no reply is sent).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_inbox.py
import copy, json, unittest
from datetime import datetime, timezone
from app.store import MemoryStore
from app import inbox
from tests.test_config import GOOD

NOW = datetime(2026, 9, 29, 3, 0, tzinfo=timezone.utc)   # 04:00 in London, before 08:15

class Inbox(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.key_error = None

    def h(self, sender, subject, attachment=None, now=NOW):
        return inbox.handle(sender, subject, attachment, self.store, "ABC123",
                            lambda k: self.key_error,
                            lambda s: {"feed https://example.com/feed.xml": "4 items"},
                            now, "me+ai@gmail.com")

    def join(self, cfg=None, subject="join abc123"):
        return self.h("p@x.test", subject, json.dumps(cfg or GOOD))

    def test_signup_with_invite_code_case_insensitive(self):
        subj, text, _ = self.join()
        self.assertIn("You're in", subj)
        self.assertIn("today at 08:15", text)
        self.assertIn("4 items", text)
        self.assertEqual(self.store.get_user("p@x.test")["status"], "active")

    def test_wrong_or_missing_invite(self):
        subj, text, _ = self.join(subject="join nope")
        self.assertIn("invite", text.lower())
        self.assertIsNone(self.store.get_user("p@x.test"))
        self.assertIsNone(self.h("stranger@x.test", "hello"))          # no reply at all

    def test_bad_json_and_bad_fields_listed(self):
        _, text, _ = self.h("p@x.test", "join ABC123", "{not json")
        self.assertIn("isn't valid JSON", text)
        bad = copy.deepcopy(GOOD); bad["send_time"] = "9:20"
        _, text, _ = self.join(bad)
        self.assertIn("send_time", text)
        self.assertIsNone(self.store.get_user("p@x.test"))

    def test_rejected_key(self):
        self.key_error = "Google rejected this Gemini key"
        _, text, _ = self.join()
        self.assertIn("rejected", text)
        self.assertIsNone(self.store.get_user("p@x.test"))

    def test_edit_keeps_status_and_todays_send(self):
        self.join()
        u = self.store.get_user("p@x.test"); u["last_sent_date"] = "2026-09-29"
        u["status"] = "paused"; self.store.put_user(u)
        edited = copy.deepcopy(GOOD); edited["name"] = "Priya K"
        subj, _, _ = self.h("p@x.test", "update", json.dumps(edited))
        u = self.store.get_user("p@x.test")
        self.assertIn("Updated", subj)
        self.assertEqual((u["config"]["name"], u["status"], u["last_sent_date"]),
                         ("Priya K", "paused", "2026-09-29"))

    def test_commands(self):
        self.join()
        self.assertIn("Paused", self.h("p@x.test", "pause")[0])
        self.assertEqual(self.store.get_user("p@x.test")["status"], "paused")
        self.assertIn("Resumed", self.h("p@x.test", "Resume")[0])
        subj, _, att = self.h("p@x.test", "config")
        self.assertEqual(json.loads(att)["name"], "Priya")
        self.assertIn("Stopped", self.h("p@x.test", "stop")[0])
        self.assertIsNone(self.store.get_user("p@x.test"))

    def test_unknown_subject_from_member_gets_help(self):
        self.join()
        subj, text, _ = self.h("p@x.test", "what?")
        self.assertIn("How to use", subj)
        self.assertIn("me+ai@gmail.com", text)

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to see it fail**

Run: `python -m unittest tests.test_inbox -v`
Expected: `ModuleNotFoundError: No module named 'app.inbox'`

- [ ] **Step 3: Implement**

```python
# app/inbox.py
"""What happens when an email reaches the sign-up address. Replies are plain text, written
for a person reading them on a phone."""
import json
from .config import validate_config
from .tick import first_digest_text, mark_new_user

HELP = """How to use the digest (everything is by email to {addr}):

- Change your feed: attach a new config.json. Any subject works, e.g. "update".
- Pause:  subject "pause"      Resume: subject "resume"
- See your current settings: subject "config" (your config.json comes back attached)
- Leave and delete your data: subject "stop"

The setup guide explains every field in config.json."""


def _report_text(report):
    if not report:
        return "You listed no sources of your own; you'll get the shared AI sources only."
    bad = {k: v for k, v in report.items() if v.startswith("error")}
    lines = ["Your sources, as checked just now:"]
    lines += ["  %s: %s" % (k, v) for k, v in sorted(report.items())]
    if bad:
        lines.append("\n%d of them failed. Ask your AI to fix or replace those, then send "
                     "the updated config.json. Everything else works meanwhile." % len(bad))
    return "\n".join(lines)


def _save(sender, attachment, user, store, check_key, check_sources, now_utc, addr):
    try:
        raw = json.loads(attachment)
    except ValueError as e:
        return ("Problem with your config.json",
                "The attached file isn't valid JSON (%s). Ask your AI for the file again and "
                "say: \"output only valid JSON, double quotes, no comments, no trailing "
                "commas\"." % e, None)
    cfg, errors = validate_config(raw)
    if errors:
        return ("Problems in your config.json",
                "Nothing was saved. Please fix these and send the file again:\n\n"
                + "\n".join("- " + e for e in errors), None)
    key_error = check_key(cfg["gemini_key"])
    if key_error:
        return ("Problem with your Gemini key", "Nothing was saved. " + key_error, None)
    report = check_sources(cfg["sources"])
    if user is None:
        user = mark_new_user({"email": sender, "status": "active", "config": cfg,
                              "created": now_utc.isoformat(), "source_report": report},
                             now_utc)
        subject, opening = "You're in", "You're signed up, %s." % cfg["name"]
    else:
        user["config"], user["source_report"] = cfg, report
        subject, opening = "Updated", "Your settings are updated, %s." % cfg["name"]
    store.put_user(user)
    when = ("Your digest is paused; send \"resume\" to restart it."
            if user["status"] == "paused"
            else "Next digest: %s." % first_digest_text(user, now_utc))
    return (subject, "%s %s\n\n%s\n\n%s" % (opening, when, _report_text(report),
                                            HELP.format(addr=addr)), None)


def handle(sender, subject, attachment, store, invite_code, check_key, check_sources,
           now_utc, signup_address):
    sender = sender.strip().lower()
    words = subject.split()
    cmd = words[0].lower() if words else ""
    has_code = invite_code.lower() in (w.lower() for w in words)
    user = store.get_user(sender)
    addr = signup_address

    if attachment is not None:
        if user is None and not has_code:
            return ("Sign-up not accepted",
                    "To sign up, the subject must be: join <your invite code>\n"
                    "Ask the person who invited you for the code.", None)
        return _save(sender, attachment, user, store, check_key, check_sources, now_utc, addr)

    if user is None:
        if has_code:
            return ("Your config.json is missing",
                    "The invite code is right, but no config.json was attached. Attach the "
                    "file (named config.json) and send again.", None)
        return None   # strangers get no reply

    if cmd == "pause":
        user["status"] = "paused"; store.put_user(user)
        return ("Paused", "Your digest is paused. Send \"resume\" to restart it.", None)
    if cmd == "resume":
        user["status"] = "active"; store.put_user(user)
        return ("Resumed", "Resumed. Next digest: %s." % first_digest_text(user, now_utc), None)
    if cmd == "stop":
        store.delete_user(sender)
        return ("Stopped", "You're unsubscribed and your data is deleted. To come back, "
                "sign up again with an invite code.", None)
    if cmd == "config":
        return ("Your config.json",
                "Your current settings are attached.\n\n"
                + _report_text(user.get("source_report", {})),
                json.dumps(user["config"], indent=2))
    return ("How to use the digest", HELP.format(addr=addr), None)
```

- [ ] **Step 4: Run to see it pass**

Run: `python -m unittest tests.test_inbox -v`
Expected: 7 tests, OK

- [ ] **Step 5: Commit**

```bash
git add app/inbox.py tests/test_inbox.py
git commit -m "Handle sign-ups, config edits and pause/resume/stop/config by email"
```

---

### Task 9: The Lambda entry point

**Files:** Create `app/handler.py`, `tests/test_handler.py`.

**Interfaces:**
- Consumes: everything above.
- Produces: `lambda_handler(event, context)`, where `event["job"]` is `"inbox"`, `"tick"` or `"selftest"`. It also runs locally as `python -m app.handler <job>` (with the settings exported and AWS credentials available).
- Settings (Lambda environment variables): `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `SIGNUP_ADDRESS`, `INVITE_CODE`, optional `GITHUB_TOKEN`, and `TABLE_PREFIX` (default `ainews`).

- [ ] **Step 1: Write the failing test** (it covers only the wiring that isn't input/output. The real services are exercised by `selftest` in Task 10.)

```python
# tests/test_handler.py
import unittest
from unittest import mock
from app import handler

class Handler(unittest.TestCase):
    def test_unknown_job_and_missing_setting(self):
        with self.assertRaisesRegex(ValueError, "unknown job"):
            handler.lambda_handler({"job": "nope"}, None)
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "GMAIL_ADDRESS"):
                handler.setting("GMAIL_ADDRESS")

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to see it fail**

Run: `python -m unittest tests.test_handler -v`
Expected: `ModuleNotFoundError: No module named 'app.handler'`

- [ ] **Step 3: Implement**

```python
# app/handler.py
"""Lambda entry point. EventBridge Scheduler sends {"job": "inbox"} and {"job": "tick"}
every 15 minutes; {"job": "selftest"} is run by hand after deploying."""
import os, sys, traceback
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from . import inbox, mailer, pick, render, sources, tick
from .config import SECTIONS


def setting(name, default=None):
    value = os.environ.get(name) or default
    if value is None:
        raise RuntimeError("missing setting %s" % name)
    return value


def _store():
    from .store import DynamoStore
    return DynamoStore(setting("TABLE_PREFIX", "ainews"))


def run_inbox():
    gmail, pw = setting("GMAIL_ADDRESS"), setting("GMAIL_APP_PASSWORD")
    signup, code = setting("SIGNUP_ADDRESS"), setting("INVITE_CODE")
    store, now = _store(), datetime.now(timezone.utc)
    imap, messages = mailer.fetch_new(gmail, pw, signup)
    handled = 0
    try:
        for uid, raw in messages:
            m = mailer.parse(raw)
            if m["auto"] or m["sender"] == signup.lower():
                mailer.mark_seen(imap, uid)
                continue
            try:
                reply = inbox.handle(m["sender"], m["subject"], m["attachment"], store, code,
                                     pick.check_key, sources.check_sources, now, signup)
                if reply:
                    subject, text, attachment = reply
                    mailer.send(gmail, pw, m["sender"], subject, text=text,
                                attachment=attachment, reply_to=signup)
                mailer.mark_seen(imap, uid)
                handled += 1
            except Exception:
                print("  ! inbox message from %s failed:\n%s"
                      % (m["sender"], traceback.format_exc()))   # left unread: retried
    finally:
        imap.logout()
    return {"handled": handled}


def run_tick():
    gmail, pw, signup = (setting("GMAIL_ADDRESS"), setting("GMAIL_APP_PASSWORD"),
                         setting("SIGNUP_ADDRESS"))
    store, now = _store(), datetime.now(timezone.utc)
    discovered = []   # fetched once per tick, and only if someone is due

    def make_digest(user):
        if not discovered:
            discovered.extend(sources.discover_repos(store.cache_get, store.cache_put,
                                                     token=os.environ.get("GITHUB_TOKEN")))
        cfg = user["config"]
        items, report = sources.gather(cfg["sources"], discovered,
                                       store.sent_links(user["email"]))
        prompt = pick.build_prompt(cfg["profile"], cfg["counts"], items)
        result = pick.keep_known_links(pick.call_gemini(cfg["gemini_key"], prompt),
                                       {i["link"] for i in items}, cfg["counts"])
        links = [e["link"] for s in SECTIONS for e in result[s]]
        day = now.astimezone(ZoneInfo(cfg["timezone"])).strftime("%A %d %B %Y")
        return render.render(result, day, cfg["name"], signup), links, report

    def send(to, subject, html):
        mailer.send(gmail, pw, to, subject, html=html, reply_to=signup)

    def note(user):
        day = now.astimezone(ZoneInfo(user["config"]["timezone"])).strftime("%A %d %B")
        return render.failure_note(user["config"]["name"], day,
                                   user.get("last_error", "unknown error"), signup)

    return {"sent": tick.run_tick(store, now, make_digest, send, note)}


def selftest(event):
    """Checks every outside dependency once and reports each separately."""
    results = {}

    def check(name, fn):
        try:
            results[name] = fn() or "ok"
        except Exception as e:
            results[name] = "FAILED %s: %s" % (type(e).__name__, e)

    check("timezone data", lambda: str(ZoneInfo("Asia/Kolkata")))
    check("dynamodb", lambda: "%d users" % len(_store().all_users()))
    gmail, pw = setting("GMAIL_ADDRESS"), setting("GMAIL_APP_PASSWORD")
    check("imap", lambda: "%d unread for sign-up"
          % len(mailer.fetch_new(gmail, pw, setting("SIGNUP_ADDRESS"))[1]))
    if event.get("to"):
        check("smtp", lambda: mailer.send(gmail, pw, event["to"], "ai-news selftest",
                                          text="SMTP from Lambda works."))
    return results


def lambda_handler(event, context):
    job = (event or {}).get("job")
    if job == "inbox":
        return run_inbox()
    if job == "tick":
        return run_tick()
    if job == "selftest":
        return selftest(event)
    raise ValueError("unknown job %r" % job)


if __name__ == "__main__":
    print(lambda_handler({"job": sys.argv[1], "to": (sys.argv[2:] or [None])[0]}, None))
```

- [ ] **Step 4: Run all tests**

Run: `python -m unittest discover -s tests -v`
Expected: all tests (about 41) OK

- [ ] **Step 5: Commit**

```bash
git add app/handler.py tests/test_handler.py
git commit -m "Lambda entry point: inbox, tick and selftest jobs"
```

---

### Task 10: Deploy to AWS (always-free settings) and prove it works

**Files:** Create `deploy/deploy.sh`, `deploy/env.example`.

- [ ] **Step 1: Re-check the free allowance for provisioned DynamoDB throughput** before creating tables. Fetch https://aws.amazon.com/dynamodb/pricing/ and confirm the always-free tier includes 25 provisioned read and 25 provisioned write capacity units. If it doesn't, stop and report back to the owner before creating anything.

- [ ] **Step 2: Settings file** `deploy/env.example` (committed). The owner copies it to `.env.deploy` (gitignored) and fills it in:

```
GMAIL_ADDRESS=you@gmail.com
GMAIL_APP_PASSWORD=the 16-character app password (same one the current digest uses)
SIGNUP_ADDRESS=you+ainews@gmail.com
INVITE_CODE=pick-a-code
GITHUB_TOKEN=
```

- [ ] **Step 3: Write `deploy/deploy.sh`**

```bash
#!/usr/bin/env bash
# Creates or updates everything on AWS. Safe to re-run: it creates only what's missing
# and always pushes the latest code and settings. Needs: aws CLI logged in, python, pip.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env.deploy; set +a
REGION="${AWS_REGION:-us-east-1}"; export AWS_DEFAULT_REGION="$REGION"
FN=ainews; P=ainews
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)

table() {  # name, key schema args, capacity
  aws dynamodb describe-table --table-name "$1" >/dev/null 2>&1 || {
    echo "creating table $1"
    aws dynamodb create-table --table-name "$1" "${@:2}" >/dev/null
    aws dynamodb wait table-exists --table-name "$1"
  }
}
# Provisioned, small: 7 read + 7 write units in total, inside the always-free 25/25.
table $P-users --attribute-definitions AttributeName=email,AttributeType=S \
  --key-schema AttributeName=email,KeyType=HASH \
  --provisioned-throughput ReadCapacityUnits=1,WriteCapacityUnits=1
table $P-sent --attribute-definitions AttributeName=email,AttributeType=S AttributeName=link,AttributeType=S \
  --key-schema AttributeName=email,KeyType=HASH AttributeName=link,KeyType=RANGE \
  --provisioned-throughput ReadCapacityUnits=5,WriteCapacityUnits=5
table $P-cache --attribute-definitions AttributeName=name,AttributeType=S \
  --key-schema AttributeName=name,KeyType=HASH \
  --provisioned-throughput ReadCapacityUnits=1,WriteCapacityUnits=1
aws dynamodb update-time-to-live --table-name $P-sent \
  --time-to-live-specification Enabled=true,AttributeName=expires >/dev/null 2>&1 || true

# Role the function runs as: logs + these three tables only.
if ! aws iam get-role --role-name $FN-lambda >/dev/null 2>&1; then
  echo "creating role $FN-lambda"
  aws iam create-role --role-name $FN-lambda --assume-role-policy-document \
    '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
  aws iam attach-role-policy --role-name $FN-lambda \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
  sleep 10   # new roles take a moment before Lambda accepts them
fi
aws iam put-role-policy --role-name $FN-lambda --policy-name tables --policy-document "{
  \"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",
  \"Action\":[\"dynamodb:GetItem\",\"dynamodb:PutItem\",\"dynamodb:DeleteItem\",\"dynamodb:Scan\",
              \"dynamodb:Query\",\"dynamodb:BatchWriteItem\",\"dynamodb:DescribeTable\"],
  \"Resource\":\"arn:aws:dynamodb:$REGION:$ACCOUNT:table/$P-*\"}]}"

# Package: our code + tzdata (Lambda's Python may lack timezone files).
rm -rf build function.zip && mkdir build
cp -r app build/
python -m pip install --quiet --no-compile tzdata --target build
( cd build && python -m zipfile -c ../function.zip app tzdata )
python - <<'PY' > build/env.json
import json, os
keys = ["GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "SIGNUP_ADDRESS", "INVITE_CODE", "GITHUB_TOKEN"]
print(json.dumps({"Variables": {k: os.environ[k] for k in keys if os.environ.get(k)}}))
PY

ROLE_ARN="arn:aws:iam::$ACCOUNT:role/$FN-lambda"
if aws lambda get-function --function-name $FN >/dev/null 2>&1; then
  aws lambda update-function-code --function-name $FN --zip-file fileb://function.zip >/dev/null
  aws lambda wait function-updated --function-name $FN
  aws lambda update-function-configuration --function-name $FN --environment file://build/env.json \
    --timeout 900 --memory-size 256 >/dev/null
else
  echo "creating function $FN"
  aws lambda create-function --function-name $FN --runtime python3.13 --architectures arm64 \
    --handler app.handler.lambda_handler --role "$ROLE_ARN" --timeout 900 --memory-size 256 \
    --zip-file fileb://function.zip --environment file://build/env.json >/dev/null
fi
aws lambda wait function-updated --function-name $FN
aws logs create-log-group --log-group-name /aws/lambda/$FN >/dev/null 2>&1 || true
aws logs put-retention-policy --log-group-name /aws/lambda/$FN --retention-in-days 14

# Scheduler: its own role that may only invoke this function.
FN_ARN="arn:aws:lambda:$REGION:$ACCOUNT:function:$FN"
if ! aws iam get-role --role-name $FN-scheduler >/dev/null 2>&1; then
  echo "creating role $FN-scheduler"
  aws iam create-role --role-name $FN-scheduler --assume-role-policy-document \
    '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"scheduler.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
  sleep 10
fi
aws iam put-role-policy --role-name $FN-scheduler --policy-name invoke --policy-document \
  "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"lambda:InvokeFunction\",\"Resource\":\"$FN_ARN\"}]}"
for job in inbox tick; do
  args=(--name $FN-$job --schedule-expression "rate(15 minutes)" --flexible-time-window Mode=OFF
        --target "{\"Arn\":\"$FN_ARN\",\"RoleArn\":\"arn:aws:iam::$ACCOUNT:role/$FN-scheduler\",\"Input\":\"{\\\"job\\\":\\\"$job\\\"}\"}")
  aws scheduler get-schedule --name $FN-$job >/dev/null 2>&1 \
    && aws scheduler update-schedule "${args[@]}" >/dev/null \
    || aws scheduler create-schedule "${args[@]}" >/dev/null
done
echo "deployed: function $FN, schedules $FN-inbox and $FN-tick every 15 minutes"
```

- [ ] **Step 4: Run it**

```bash
chmod +x deploy/deploy.sh && ./deploy/deploy.sh
```
Expected: it ends with `deployed: ...`. On any error, stop and report the exact message.

- [ ] **Step 5: Selftest from inside AWS** (this proves timezone data, DynamoDB, IMAP, and SMTP on port 465 from Lambda)

```bash
aws lambda invoke --function-name ainews --cli-binary-format raw-in-base64-out \
  --payload "{\"job\":\"selftest\",\"to\":\"$(grep GMAIL_ADDRESS .env.deploy | cut -d= -f2)\"}" \
  /dev/stdout
```
Expected: every value is `ok` or a count (`"timezone data": "Asia/Kolkata"`, `"dynamodb": "0 users"`, `"imap": "0 unread for sign-up"`, `"smtp": "ok"`), and the owner receives an email titled "ai-news selftest". If any value is `FAILED`, stop and fix that one before going further.

- [ ] **Step 6: Commit**

```bash
git add deploy/deploy.sh deploy/env.example
git commit -m "Deploy script for AWS: small provisioned tables, one function, two 15-minute schedules"
```

---

### Task 11: The friend-facing guide and example (the owner asked for these to be extremely clear)

**Files:** Create `SETUP.md`, `config.example.json`, `OPERATIONS.md`.

- [ ] **Step 1: Write `config.example.json`**. This is the owner's real settings with the key blanked, and it's what friends' AIs copy the shape from. The owner's former in-code priorities (speech first, coding tools capped) now live in the profile:

```json
{
  "name": "Ayush",
  "gemini_key": "PASTE_YOUR_GEMINI_KEY_HERE",
  "send_time": "09:45",
  "timezone": "Asia/Kolkata",
  "counts": {"ships": 15, "industry": 10, "research": 5, "practice": 5},
  "profile": "I build production AI systems, mostly voice and speech. Real-time voice agents on Pipecat: Deepgram/Sarvam STT, Cartesia TTS, Gemini as the brain, state-machine flows, barge-in, turn-taking, latency budgets. Indian-language speech: Hindi, Tamil, Arabic; ASR and TTS quality for accented and code-mixed speech is a constant problem, and Sarvam, IndicTrans and similar matter a lot. Call analytics: transcription, speaker diarization and role assignment, script adherence scoring, LLM-as-judge rubrics on sales and support calls. Gemini-heavy pipelines (long context, thinking budgets, structured output), some Anthropic and Groq; I track cost and latency per call. RAG with Qdrant, embeddings, document ingestion. AI governance: a proxy in front of agents adding guardrails, policy enforcement, audit trails, jailbreak and PII detection. Agentic coding tooling: Claude Code skills, plugins, evals for agents. Stack: Python + FastAPI, Node/TypeScript, MongoDB, Docker on VMs.\n\nPriorities: anything touching my voice and speech stack, Indian languages, evals, guardrails and inference cost comes first; a new Indic speech model or a voice-agent framework release beats any coding-tool update. I also want the industry side: funding, executive moves, commentary, enterprise announcements. Claude and agentic-coding news is secondary: at most 4 Shipped items, only genuinely new things or real features, never routine point releases. I want to hear about genuinely new kinds of models early (Jev, TypeSafe's decision model, is an example I heard about too late). Only call something speech-related if the item itself is about speech.\n\nI do not care about: routine version-bump releases of libraries I don't actively use, outage chatter.",
  "sources": {
    "rss": [],
    "github_repos": ["pipecat-ai/pipecat", "anthropics/claude-code", "anthropics/skills",
                     "anthropics/claude-agent-sdk-python", "anthropics/claude-agent-sdk-typescript",
                     "modelcontextprotocol/servers", "qdrant/qdrant", "openai/whisper",
                     "AI4Bharat/IndicTrans2"],
    "hn_queries": ["speech recognition", "voice ai", "claude code", "mcp server"],
    "arxiv_categories": ["cs.CL", "cs.SD", "eess.AS", "cs.CR", "cs.IR"],
    "arxiv_keywords": ["speech recognition", "speaker diarization", "text-to-speech",
                       "voice agent", "spoken dialogue", "full-duplex", "guardrail",
                       "jailbreak", "prompt injection", "retrieval-augmented generation",
                       "LLM-as-a-judge", "low-resource", "Hindi", "Tamil", "Indic"]
  }
}
```

Check it: `python -c "import json; from app.config import validate_config; c,e=validate_config(json.load(open('config.example.json'))); print(e)"`
Expected: `['"gemini_key" must be your Gemini API key ...']`. That's the one error it should have, because the key is blanked.

- [ ] **Step 2: Write `SETUP.md`**. Use exactly the text below, replacing `SIGNUP_ADDRESS` everywhere with the real address from `.env.deploy`:

````markdown
# Getting your daily tech digest

Every morning, at a time you choose, you get one email with the news from **your** field
(AI included) that matters to **your** work. It has up to four sections:

- **Shipped** -- things you can use today: releases, new tools, new models.
- **Industry** -- money, people, deals, policy.
- **Research** -- papers and benchmarks.
- **Practice** -- how other teams actually do the work: engineering write-ups,
  postmortems, migration stories.

Setup takes about 15-20 minutes and happens entirely by email. You'll need:
your invite code (from whoever invited you), your CV or LinkedIn text, access to
Claude or ChatGPT, and a Google account.

---

## Step 1 -- Get a free Gemini API key (3 minutes)

Your digest is picked by Google's Gemini using **your own free key**, so it costs nobody
anything.

1. Open https://aistudio.google.com/apikey and sign in with any Google account.
2. Click **Create API key**. If it asks, choose **Create API key in new project**.
3. Copy the key (it usually starts with `AIza`). Keep it for Step 3.

Two things to know:
- **Don't turn on billing** for that project. The free tier is plenty (one request a
  day), and without billing a leaked key can never cost you money.
- On the free tier, Google may use what's sent to Gemini to improve its products. What's
  sent is your profile (Step 2) plus public news headlines -- so don't put anything in
  your profile you'd mind Google reading.

## Step 2 -- Have your own AI write your config.json (10 minutes)

1. Open a **new** chat in Claude (https://claude.ai) or ChatGPT (https://chatgpt.com).
2. Attach your **CV** (PDF is fine). If you like, also paste your LinkedIn **About** and
   **Experience** sections.
3. Copy the **whole** prompt below into the chat. Before sending, fill in the three
   things in `<angle brackets>` near the top.
4. Send it. Answer the questions it asks (up to 5).
5. It will reply with one block of JSON. Check the `profile` paragraph reads like you,
   and ask it to change anything that's off.
6. Copy the JSON and save it as a file named exactly **`config.json`**.
   - Windows: open Notepad, paste, *File > Save As*, set "Save as type" to *All files*,
     name it `config.json`.
   - Mac: open TextEdit, *Format > Make Plain Text*, paste, save as `config.json`.
7. Open the file and replace `PASTE_YOUR_GEMINI_KEY_HERE` with your key from Step 1
   (keep the quote marks around it).

### The prompt to paste

```text
I'm signing up for a personalised daily tech-news email. All its settings live in one
file called config.json. Please write mine.

My send time: <e.g. 08:30 -- minutes must be 00, 15, 30 or 45>
My city (for the timezone): <e.g. London>
Anything I especially want or don't want: <or "nothing special">

I've attached my CV (and maybe pasted my LinkedIn). Before writing anything, ask me up to
5 short questions about whatever is missing -- especially: the tools, languages,
platforms and cloud services I use day to day; what I'm building right now; what I'm
trying to get better at; and what kinds of news I find useless.

Then write the file following these rules exactly.

1. Output ONLY the JSON, in a single code block, nothing before or after. It must be
   valid JSON: double quotes, no comments, no trailing commas.
2. Use exactly these keys and no others: name, gemini_key, send_time, timezone, counts,
   profile, sources.
3. "name": my first name.
4. "gemini_key": exactly the text PASTE_YOUR_GEMINI_KEY_HERE. I'll replace it myself.
5. "send_time": my send time as "HH:MM", 24-hour clock, minutes 00, 15, 30 or 45.
6. "timezone": the IANA timezone name for my city, like "Europe/London",
   "Asia/Kolkata" or "America/New_York". Not "IST" or "+05:30".
7. "counts": how many items I want per section, each a whole number 0 to 20, where 0
   turns that section off. Keys: "ships" (releases and tools I can use today),
   "industry" (funding, people, deals, policy), "research" (papers, benchmarks),
   "practice" (engineering write-ups, postmortems, migration stories). Unless I said
   otherwise, use ships 12, industry 6, research 5, practice 5 -- but set research to 0
   if papers clearly aren't useful for my job.
8. "profile": 150 to 400 words of plain first-person paragraphs. This is the ONLY thing
   the news picker knows about me, so be concrete: name the actual tools, frameworks,
   databases and services I use (named tools beat general areas like "data"). Cover:
   what I build, my stack, what I'm working on now, what I care about most (in priority
   order), and end with a sentence starting "I do not care about:".
9. "sources": where to look for news in MY field. Major AI news (AI lab blogs, AI
   releases, AI papers, AI on Hacker News) is already covered for everyone, so spend
   these on my own field and stack:
   - "rss": up to 20 full RSS/Atom feed URLs (not homepages) of blogs and news sites in
     my field that you are confident exist: official project blogs, company engineering
     blogs, well-known newsletters that publish feeds.
   - "github_repos": up to 25 GitHub repos as "owner/name" for tools I use or should
     follow, whose releases matter to me.
   - "hn_queries": up to 8 short Hacker News search phrases for my field.
   - "arxiv_categories": arXiv category codes relevant to me (e.g. "cs.DB"), or [] if
     papers aren't useful to me.
   - "arxiv_keywords": up to 20 phrases; a paper is kept only if its title or abstract
     contains one. Use [] if arxiv_categories is [].
10. Never invent a feed URL or a repo name. If you're not sure it exists, leave it out.
    The sign-up check tests every source and tells me which ones failed.

Here is someone else's complete config.json, to copy the shape from (not the content):

<PASTE THE CONTENTS OF config.example.json HERE>
```

(Whoever invited you sends you `config.example.json` along with this guide.)

## Step 3 -- Sign up by email (1 minute)

Send an email:

- **To:** `SIGNUP_ADDRESS`
- **Subject:** `join YOUR-INVITE-CODE` (for example `join blue-otter-42`)
- **Attachment:** your `config.json`
- **Body:** anything (it's ignored)

Within about 15 minutes you'll get a reply:

- **"You're in"** -- it says when your first digest arrives, and lists each of your
  sources with how many items it found or why it failed. A few failed feeds are fine:
  paste the failed ones into your AI chat, ask it to fix or replace them, and send the
  updated file (see "Changing things" below).
- **"Problems in your config.json"** -- nothing was saved; it lists exactly what to fix.
  Paste that list into your AI chat, ask it to fix the file, and send it again.
- **"Problem with your Gemini key"** -- copy the key again from Step 1 (the whole key,
  no spaces) and resend.
- **No reply after 30 minutes** -- check the subject has your invite code and the file
  is attached (not pasted into the body), then tell whoever invited you.

## Changing things later

Everything is an email to `SIGNUP_ADDRESS` from the same address you signed up with:

| To... | Send |
|---|---|
| Change anything (profile, sources, time, sections) | subject `update`, new `config.json` attached |
| Get your current file back | subject `config` |
| Pause the digest | subject `pause` |
| Restart it | subject `resume` |
| Leave and delete your data | subject `stop` |

Tips: to tune what you get, tell your AI chat what you liked or didn't in a few recent
digests and ask it to update the `profile`. Setting a section's count to 0 removes it.
````

- [ ] **Step 3: Write `OPERATIONS.md`** (for the owner):

````markdown
# Running the digest (owner's notes)

## Deploy or update
1. `cp deploy/env.example .env.deploy` and fill it in (once).
2. `aws login` if the session expired.
3. `./deploy/deploy.sh` -- safe to re-run; it pushes the latest code and settings.
4. Check: `aws lambda invoke --function-name ainews --cli-binary-format raw-in-base64-out --payload '{"job":"selftest"}' /dev/stdout` -- every line should be ok.

## Invite someone
Send them the invite code, `SETUP.md`, and `config.example.json`. To change the code,
edit INVITE_CODE in `.env.deploy` and re-run the deploy.

## See what happened
`aws logs tail /aws/lambda/ainews --since 2h` shows every run; a person's last error
also sits on their record (`last_error`).

## Costs
Everything runs inside AWS's always-free allowances: DynamoDB with 7 read and 7 write
units provisioned in total (free up to 25 each), Lambda at a few thousand GB-seconds a
month (free up to 400,000), 2 schedules at 96 runs a day each (free up to 14 million a
month), logs kept 14 days. Gemini is each person's own free key. Check the AWS Billing
page after the first month to confirm $0.00.
````

- [ ] **Step 4: Commit**

```bash
git add SETUP.md config.example.json OPERATIONS.md
git commit -m "Friend setup guide with the exact prompt for their own AI, example config, owner notes"
```

---

### Task 12: End-to-end with the owner as the first person

- [ ] **Step 1: Deploy the latest** with `./deploy/deploy.sh`.
- [ ] **Step 2: Owner signs up.** The owner makes `config.json` from `config.example.json` with their real Gemini key, and sets `send_time` to the next quarter hour at least 20 minutes away. They email it to the sign-up address with subject `join <INVITE_CODE>`.
- [ ] **Step 3: Check the reply** arrives within about 15 minutes with the subject "You're in" and a list of sources. If not, run `aws logs tail /aws/lambda/ainews --since 30m` and fix what it shows.
- [ ] **Step 4: Check the digest** arrives within 15 minutes after the send time, with the four sections and the footer. Confirm it with the owner.
- [ ] **Step 5: Commands.** Send `config` (the file comes back attached), `pause` then `resume`, then an `update` with a changed `counts`. Each reply must match `SETUP.md`.
- [ ] **Step 6: Push the branch** with `git push -u origin multiuser`, using the owner's personal GitHub, which this repo's remote already uses. Don't merge. Cutover to `main` happens only after a week of clean daily runs, as a separate, owner-approved step: the owner's config moves in, the Claude routine is turned off, and the GitHub workflows and old `digest.py` are removed.
````
