#!/usr/bin/env bash
# The Claude routine's mechanical steps, so the session spends its turns only on picking.
# Every turn re-sends the whole conversation, so fewer turns is the main cost lever.
#   bash /tmp/routine.sh prepare  -> exit 3 if today's digest already went out;
#                                    otherwise leaves the picking prompt in /tmp/prompt.txt
#   bash /tmp/routine.sh finish   -> renders /tmp/picks.json, commits, pushes (push = email)
# Copy to /tmp before running: prepare checks out the claude/digest branch, which doesn't
# have this file. The reader's interests come from profile.md on main.
set -euo pipefail
REPO=ayushgodbole17/ai-news
TODAY=${ROUTINE_TODAY:-$(TZ=Asia/Kolkata date +%F)}   # override only for testing

case "${1:-}" in
prepare)
  git fetch -q origin main claude/digest
  git show origin/main:digest.py > /tmp/digest.py
  git checkout -q -B claude/digest origin/claude/digest
  if [ "$(cat state/last_sent.txt 2>/dev/null || true)" = "$TODAY" ]; then
    echo "already sent today"; exit 3
  fi
  BEFORE=$(git rev-parse HEAD)
  code=$(curl -sS -o /tmp/dispatch.txt -w '%{http_code}' -X POST \
    -H "Authorization: Bearer ${GITHUB_TOKEN:-}" -H 'Accept: application/vnd.github+json' \
    -H 'Content-Type: application/json' \
    "https://api.github.com/repos/$REPO/actions/workflows/collect.yml/dispatches" \
    -d '{"ref":"main"}' || echo 000)
  path="previous list"
  if [ "$code" = 204 ]; then
    for _ in $(seq 24); do   # up to 8 minutes; the job usually lands in under one
      sleep 20
      git fetch -q origin claude/digest
      if [ "$(git rev-parse origin/claude/digest)" != "$BEFORE" ]; then
        git reset -q --hard origin/claude/digest; path="fresh collect"; break
      fi
    done
  fi
  if [ "$path" != "fresh collect" ]; then
    stamp=$(git log -1 --format=%ct -- inbox/items.json 2>/dev/null || true)
    if [ -z "$stamp" ] || [ $(( $(date +%s) - stamp )) -gt 108000 ]; then   # older than 30h
      python /tmp/digest.py --collect-json inbox/items.json; path="sandbox collect"
    fi
  fi
  echo "items: $path (collect dispatch HTTP $code)"
  # profile.md on main is the one place the reader's interests live; edit it there.
  DIGEST_PROFILE="$(git show origin/main:profile.md)" python /tmp/digest.py --build-prompt inbox/items.json \
    > /tmp/prompt.txt
  echo "prompt ready: /tmp/prompt.txt, $(wc -c < /tmp/prompt.txt) bytes"
  ;;
finish)
  python /tmp/digest.py --render-picks /tmp/picks.json inbox/items.json
  git add outbox/digest.html state/ inbox/items.json
  git -c user.name='Ayush Godbole' -c user.email='godbole.ayush@gmail.com' \
    commit -q -m "Digest for $TODAY"
  git push -q origin claude/digest
  echo "pushed: the email is on its way"
  ;;
*)
  echo "usage: routine.sh prepare|finish" >&2; exit 2 ;;
esac
