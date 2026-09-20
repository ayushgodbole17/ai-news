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

**`PROFILE` is the only thing worth tuning**, and it lives in `config.json` for a run by
hand, or the `DIGEST_PROFILE` secret for a scheduled one — never in the script itself. It
is what makes a modest diarization paper outrank a big model launch. When the work
changes, edit whichever of those two you're using; everything else is plumbing.

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

## Running it on a schedule

Want your own copy sent automatically instead of run by hand? Fork the repo and set it up
there. `config.json` is gitignored on purpose, so it never reaches your fork's checkout —
a scheduled run needs your profile a different way, and that way is repository
secrets/variables alongside the ones from Setup:

- `DIGEST_PROFILE` (secret) — the same text as your local `config.json`'s `profile`.
- `DIGEST_KEEP_SHIPS`, `DIGEST_KEEP_RESEARCH` (variables) — only needed if 15/5 isn't
  your split.
- `DIGEST_SEND_AFTER`, `DIGEST_UTC_OFFSET` (variables) — only needed if 09:45 IST isn't
  when you want it. See below for why these exist rather than a cron time.

Set at Settings → Secrets and variables → Actions. `digest.py` reads `config.json` first
if one exists, then lets these env vars override it — so this is also how *this* repo's
own scheduled run gets a real profile, since its checkout has no `config.json` either.
Leave them unset and the workflow just runs on `digest.py`'s built-in defaults.

### Why the schedule looks strange

**GitHub's `schedule:` cron does not keep time on this repo.** Measured across 13
consecutive days, every scheduled run fired **4.3 to 5.6 hours late** — never once on
time, never improving. A cron set for 09:53 IST consistently delivered at 14:30. This is
a known, unresolved platform behaviour (see
[github/community#201738](https://github.com/orgs/community/discussions/201738) and
[#196910](https://github.com/orgs/community/discussions/196910)); adding more cron
entries does not help, because the delay applies to all of them equally.

So the workflow stops trying to name a delivery time. It fires **every half hour from
22:13 to 08:43 UTC**, and `digest.py` decides which of those runs actually sends: the
first one to land at or after `SEND_AFTER` (09:45) on the reader's clock, that hasn't
already sent today. Every other run exits in about a second.

The useful property is that it self-corrects. Under today's ~5-hour delay, the run
scheduled around 23:43 UTC is the one that lands near 10:00 IST and sends. If GitHub
ever starts firing on time, the runs scheduled at 04:13 UTC onwards land in that same
local window instead and those send. Either way the mail arrives at roughly the right
time without anyone re-tuning a cron. And if a day is delayed so badly that nothing
lands in the morning, the first run after `SEND_AFTER` still sends — late beats never.

Move the delivery time with the `DIGEST_SEND_AFTER` variable (`HH:MM`), and the timezone
with `DIGEST_UTC_OFFSET` (`+05:30`, `-04:00`). A fixed offset rather than a zone name,
deliberately: India has no DST so it's exact, and `zoneinfo` would need the `tzdata`
package on Windows, breaking "standard library only".

State (`seen.json`, `last_sent.txt`, `repos.json`) lives in the **Actions cache**, not in
git — the repo history stays clean. The cache key is unique per run with a `restore-keys`
prefix, which is how you carry a rolling file forward. If the cache is ever evicted the
worst case is one repeated digest.

All told, a scheduled run needs five repository **secrets**: `GEMINI_API_KEY`,
`DIGEST_TO`, `DIGEST_FROM`, `DIGEST_SMTP_PASS`, `DIGEST_PROFILE`. Everything else is an
optional repository **variable** for moving off a default: `GEMINI_MODEL`,
`DIGEST_KEEP_SHIPS`, `DIGEST_KEEP_RESEARCH`, `DIGEST_SEND_AFTER`, `DIGEST_UTC_OFFSET`.
Actions supplies `GITHUB_TOKEN` itself — nothing to set up, it just lifts the rate limit
on repo discovery.

The Actions run delivers by email only — `digests/` is gitignored, so the HTML
file is just a local convenience.

Free for public repos; private repos draw on the monthly Actions minutes, and this
job takes well under a minute.
