# Multi-user AI digest: design

Status: agreed in conversation on 2026-09-29, awaiting written-spec review.

## What it is

A personalised daily news email for fewer than 10 friends and colleagues (AI engineers,
data engineers and similar). Each person gets news from their own field, AI included,
picked against their own profile by their own free Gemini key, at the time they choose in
their own timezone. There is no website: people sign up, edit and leave by email.

Constraints: costs as close to $0 as possible; AWS (the user's account, free-tier trial
already used, so only the always-free allowances count); sending from the owner's Gmail.

## How someone joins

1. The owner sends them an invite code and the setup guide (`SETUP.md` in the repo).
2. The guide has them give their own Claude or ChatGPT their CV, LinkedIn text and
   anything else, along with the owner's `config.json` as a worked example and a
   ready-made prompt. Their AI writes their `config.json` (schema below), including a
   list of sources for their field. The guide also walks them through getting a free
   Gemini key from Google AI Studio (a project with billing off).
3. They email that file as an attachment to the sign-up address (a Gmail "+" address on
   the owner's account) with the invite code in the subject line.
4. Within 15 minutes they get a reply: "you're in, first digest <date> at <time>", or a
   list of what was wrong (bad key, feeds that don't load, bad time or timezone).

## Editing and leaving (all by email to the same address)

- A new `config.json` attachment replaces the old one. The reply lists which of their
  sources loaded on the last run.
- One-word subjects: `pause`, `resume`, `stop` (deletes all their data), `config` (mails
  their current file back).
- Every digest ends with a line explaining these.
- Mail is accepted only from the email address that signed up (for edits) or with a
  valid invite code (for sign-up).

## config.json

```json
{
  "name": "Ayush",
  "gemini_key": "AIza...",
  "send_time": "09:45",
  "timezone": "Asia/Kolkata",
  "counts": {"ships": 15, "industry": 10, "research": 5},
  "profile": "Plain paragraphs: what I build, my stack, what I care about, what I don't.",
  "sources": {
    "rss": ["https://example.com/feed.xml"],
    "github_repos": ["apache/airflow", "dbt-labs/dbt-core"],
    "hn_queries": ["data engineering", "dbt"],
    "arxiv_categories": ["cs.DB"],
    "arxiv_keywords": ["query optimization", "data lake"]
  }
}
```

- `send_time` is on a quarter hour. `timezone` is an IANA name.
- Everyone also gets the shared AI sources the current digest uses (feeds, Hacker News
  queries, arXiv, Hugging Face, Anthropic release feeds), since the brief is "own field,
  AI included". Repo discovery by GitHub topic stays, for the shared AI part only.
- The email address comes from the sender, not the file.

## Where it runs (AWS always-free allowances, verified 2026-09-29)

- **DynamoDB** (25 GB storage free):
  - `users`: one item per email, holding the config, status (active or paused),
    `last_sent_date` in their timezone, and the creation date.
  - `sent`: (email, link) pairs with a 60-day expiry, so nobody gets a repeat.
  - `cache`: the weekly GitHub repo-discovery result.
- **Lambda** (1M runs a month free): one Python function, standard library plus boto3
  (built into the Lambda runtime), so there are no dependencies to package. Two entry
  points:
  - `inbox`: reads the sign-up address over IMAP, validates, stores, and replies.
  - `tick`: sends digests that are due.
- **EventBridge Scheduler** (14M runs a month free): runs each entry point every 15
  minutes.
- **Gmail**, with the existing app password: IMAP to read the inbox, SMTP on port 465 to
  send (outbound 465 isn't restricted for Lambda outside a VPC). The personal account
  limit of about 500 recipients a day is far above need.
- Secrets: the Gmail app password lives in the Lambda's environment settings. Gemini keys
  live in DynamoDB. That was accepted as fine for a friends-only group.

## A digest run (`tick`)

1. Find active users whose local time is at or past their `send_time` and who haven't
   been sent today (local date).
2. For each one: gather the shared AI sources plus their own, using the existing
   `digest.py` fetchers; drop links already in `sent`; build the picking prompt; call
   Gemini with their key, using the existing newest-Flash-then-fallback logic and
   retries; render with the existing email layout; send; record the links and
   `last_sent_date`.
3. If Gemini is busy, leave them unsent and the next tick retries. If they're still
   unsent 3 hours after `send_time`, email them one short "Gemini was unavailable today"
   note and mark the day done.
4. One person's failure never stops the others.

## Changes to the existing code

- The picking instructions lose the owner-specific lines (speech and voice weighting,
  Indic languages, the 4-slot cap on coding tools). Those move into the owner's own
  profile.
- Fetchers are reused as they are. `collect()` gains a "these sources" parameter.
- Built on a `multiuser` branch. `main` is replaced at cutover, because the owner's
  Claude routine reads `main` every morning.

## Cutover

The owner's Claude routine keeps sending their digest until the new system has sent
everyone's for a week without trouble. Then the owner's config joins, the routine is
switched off, and `main` becomes the new code (the GitHub collect and send jobs are
removed).

## Testing

- An offline self-check, the same pattern as today: config validation, the time-due
  logic across timezones, email-command parsing, and prompt building with an owner-free
  instruction set.
- A local run mode with a fake event, before deploying.
- After deploying: the owner signs up as the first test user, with a send time a few
  minutes ahead.

## Out of scope (add only if asked)

Website, feedback buttons, history archive, a shared digest for people without a key,
more than one AI provider.
