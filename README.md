# ai-news

A daily email of the AI news that's actually relevant to my work — voice agents,
speech, Indic languages, evals, guardrails, inference cost — and none of the
funding rounds and punditry.

One file, `digest.py`, no dependencies beyond the Python standard library.

## How it works

It pulls a bit over 100 items a day from four kinds of source: sixteen RSS/Atom feeds
(Hugging Face, DeepMind, OpenAI, Qwen, Cursor, VS Code, Simon Willison and others), the
release feeds of ~50 GitHub repos, six targeted arXiv searches, and Hacker News. The
arXiv and HN queries are scoped to the work — speaker diarization, full-duplex dialogue,
prompt injection, LLM-as-a-judge, low-resource Indic — not the whole of `cs.AI`.

**The watched repos are discovered, not listed.** `TOPICS` names areas (`llm`,
`speech-recognition`, `voice-assistant`, `rag`, `llmops` …) and GitHub is asked which
repos are currently biggest and active in each. So something that did not exist last
month shows up without this file being edited. That sweep runs weekly and caches to
`repos.json`; unauthenticated GitHub search allows only ten requests a minute, so the
topics are queried one at a time with a gap. Setting `GITHUB_TOKEN` removes both the
pacing and the limit.

Hacker News is doing a specific job: Anthropic, Meta and Mistral publish no usable
RSS, so their releases get caught there or not at all.

Everything from the last 72 hours that hasn't been sent before goes to Gemini in one
call, along with `PROFILE` — the description of what I work on. It comes back in two
sections: **Shipped** (up to 10) for things usable today — models, agents, IDEs, inference
servers, tooling — and **Research & writing** (up to 5) for papers and writeups. Splitting
them is what stops a good release being crowded out by papers. Each item gets a
plain-English "what it is" and a concrete "why you care".

The prompt forbids inventing a rationale: release notes arrive truncated, and without that
rule the model will cheerfully claim a vector-store bump improves your barge-in latency.

**`PROFILE` at the top of `digest.py` is the only thing worth tuning.** It is what
makes a modest diarization paper outrank a big model launch. When the work changes,
edit that paragraph; everything else is plumbing.

The 72-hour window is deliberately generous so a missed run or a weekend doesn't drop
anything. `seen.json` is what stops repeats — and it only records items that were
actually *sent*, so something crowded out of today's ten can still surface tomorrow.

## Running it

```bash
python digest.py --self-check   # parser asserts, no network, no key
python digest.py --dry-run      # fetch and list, no LLM call, no email
python digest.py --no-email     # full run, writes digests/YYYY-MM-DD.html
python digest.py                # full run and email
```

## Setup

**Gemini key** — https://aistudio.google.com/apikey, *Create API key*, accept the
terms. No billing, no Cloud console. Then `echo GEMINI_API_KEY=... > .env`.

One request a day of roughly 7k tokens sits well inside the free tier.

> Keys made in AI Studio now are "auth keys". Google stopped accepting the older
> "standard" keys in September 2026, so anything created before June 2026 is dead
> and needs replacing. This script sends the key as an `x-goog-api-key` header,
> which is the form Google documents for auth keys.

**Email** — Gmail needs an app password, not the account password: turn on 2-Step
Verification, then generate one at https://myaccount.google.com/apppasswords.

```
DIGEST_TO=you@gmail.com
DIGEST_FROM=you@gmail.com
DIGEST_SMTP_PASS=<16-character app password>
```

Without those two the run still works and just writes the HTML file.

## The daily schedule

`.github/workflows/digest.yml` fires three times a morning — 09:53, 10:37 and 11:21 IST.
GitHub delays scheduled jobs under load and drops them outright when it is bad enough, so
one cron is not dependable; `--once-daily` records the date a mail went out, and the two
catch-up runs stop when they see it. Only one email ever arrives.

State (`seen.json`, `last_sent.txt`, `repos.json`) lives in the **Actions cache**, not in
git — the repo history stays clean. The cache key is unique per run with a `restore-keys`
prefix, which is how you carry a rolling file forward. If the cache is ever evicted the
worst case is one repeated digest. Add `GEMINI_API_KEY`,
`DIGEST_TO`, `DIGEST_FROM` and `DIGEST_SMTP_PASS` as repository secrets, and
optionally `GEMINI_MODEL` as a repository variable to override the default. Actions
supplies `GITHUB_TOKEN` on its own.

The Actions run delivers by email only — `digests/` is gitignored, so the HTML
file is just a local convenience.

Free for public repos; private repos draw on the monthly Actions minutes, and this
job takes well under a minute.
