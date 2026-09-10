# ai-news

A daily email of the AI news that's actually relevant to my work — voice agents,
speech, Indic languages, evals, guardrails, inference cost — and none of the
funding rounds and punditry.

Runs unattended on GitHub Actions, ranks ~100 items a day down to the dozen or so worth
reading, and emails a two-section digest: what shipped, and what's worth reading.
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
sections: **Shipped** (up to 15) for things usable today — models, agents, IDEs, inference
servers, tooling — and **Research & writing** (up to 5) for papers and writeups. Splitting
them is what stops a good release being crowded out by papers. Each item gets a
plain-English "what it is" and a concrete "why you care".

The prompt forbids inventing a rationale: release notes arrive truncated, and without that
rule the model will cheerfully claim a vector-store bump improves your barge-in latency.

**`PROFILE` is the only thing worth tuning**, and it lives in `config.json`, not in the
script. It is what makes a modest diarization paper outrank a big model launch. When the
work changes, edit that file; everything else is plumbing.

The 72-hour window is deliberately generous so a missed run or a weekend doesn't drop
anything. `seen.json` is what stops repeats — and it only records items that were
actually *sent*, so something crowded out of today's ten can still surface tomorrow.

## Setup — running it for yourself

Everything below is per-person. Nobody edits `digest.py` to do any of it.

1. **Clone the repo**, or fork it if you want your own GitHub Actions schedule later.

2. **Your profile** — `cp config.example.json config.json`, then edit `profile` in that
   file to describe what *you* actually work on, and optionally `keep_ships` /
   `keep_research` if 15/5 isn't the split you want. `config.json` is gitignored, so this
   never overwrites anyone else's and never gets pushed. No `config.json` at all just
   falls back to the built-in defaults in `digest.py`.

3. **A Gemini key** — go to https://aistudio.google.com/apikey, click *Create API key*,
   accept the terms. No billing, no Cloud console, free tier is enough (one request a day
   of roughly 7k tokens). Then:

   ```
   GEMINI_API_KEY=...
   ```

   > Keys made in AI Studio now are "auth keys". Google stopped accepting the older
   > "standard" keys in September 2026, so anything created before June 2026 is dead
   > and needs replacing. This script sends the key as an `x-goog-api-key` header,
   > which is the form Google documents for auth keys.

4. **A Gmail app password, if you want email** — your normal Google password will not
   work here. Turn on 2-Step Verification on the Google account, then generate one at
   https://myaccount.google.com/apppasswords. Add:

   ```
   DIGEST_TO=you@gmail.com
   DIGEST_FROM=you@gmail.com
   DIGEST_SMTP_PASS=<16-character app password>
   ```

   Steps 3 and 4 together go into one `.env` file in the repo root (it's gitignored).
   Skip step 4 and the run still works — it just writes the HTML file instead of emailing.

5. **Try it**:

   ```bash
   python digest.py --self-check   # parser asserts, no network, no key
   python digest.py --dry-run      # fetch and list, no LLM call, no email
   python digest.py --no-email     # full run, writes digests/YYYY-MM-DD.html
   python digest.py                # full run and email
   ```

That's a full working setup running by hand. Nothing further is required unless you also
want it to run itself daily — see the next section.

## Running it on a schedule (a fork, or this repo)

If a colleague wants their own copy sent automatically rather than run by hand, they fork
the repo and set it up there — `config.json` is local-only and never travels with a fork,
so add the same three values from step 2 above as GitHub repository secrets instead:
`GEMINI_API_KEY`, `DIGEST_TO`, `DIGEST_FROM`, `DIGEST_SMTP_PASS` (Settings → Secrets and
variables → Actions). `profile` / `keep_ships` / `keep_research` still come from
`config.json` — that part has to be committed to their fork (or hardcoded into
`digest.py` there) since Actions doesn't read local files, only secrets and variables.

`.github/workflows/digest.yml` fires three times a morning — 09:53, 10:37 and 11:21 IST —
and `--once-daily` records the date a mail went out so only the first run through actually
sends; the other two see today's date and stop. Only one email ever arrives.

**GitHub's own `schedule:` cron is not reliable for a fixed clock time, especially on a
new repo.** It is a documented, unresolved platform behaviour — new or low-activity repos
can see scheduled runs fire 4-14 hours late for their first couple of weeks, some days
dropped outright. (See [github/community#201738](https://github.com/orgs/community/discussions/201738)
and [#196910](https://github.com/orgs/community/discussions/196910).) The three-attempt
spread above is free insurance, not a fix for this — it only helps against occasional
per-run congestion, not a multi-hour scheduler-wide delay.

The reliable fix is the one GitHub's own community lands on: point a real external clock
at the `workflow_dispatch` trigger instead of trusting their cron for timing.

1. **Create a fine-grained token**: github.com → Settings → Developer settings → Personal
   access tokens → Fine-grained tokens → generate one scoped only to this repo, with
   **Actions: Read and write** permission. Copy it once.
2. **Sign up at a free cron service** — [cron-job.org](https://cron-job.org) needs nothing
   but an email. Add a job:
   - URL: `https://api.github.com/repos/ayushgodbole17/ai-news/actions/workflows/digest.yml/dispatches`
   - Method: `POST`
   - Headers: `Authorization: Bearer <your token>`, `Accept: application/vnd.github+json`
   - Body: `{"ref":"main"}`
   - Schedule: daily, 10:00, `Asia/Kolkata`
3. Leave the three GitHub crons in place as a backup in case the external service itself
   ever misses a day — `--once-daily` means the two can never overlap or double-send.

State (`seen.json`, `last_sent.txt`, `repos.json`) lives in the **Actions cache**, not in
git — the repo history stays clean. The cache key is unique per run with a `restore-keys`
prefix, which is how you carry a rolling file forward. If the cache is ever evicted the
worst case is one repeated digest.

Add `GEMINI_API_KEY`, `DIGEST_TO`, `DIGEST_FROM` and `DIGEST_SMTP_PASS` as repository
secrets, and optionally `GEMINI_MODEL` as a repository variable to override the default.
Actions supplies `GITHUB_TOKEN` on its own — that one only needs read access and has
nothing to do with the fine-grained token above, which needs write access to trigger runs.

The Actions run delivers by email only — `digests/` is gitignored, so the HTML
file is just a local convenience.

Free for public repos; private repos draw on the monthly Actions minutes, and this
job takes well under a minute.
