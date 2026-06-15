#!/bin/bash
# Optional CLI helper: trigger the GitHub Action poller once from the terminal
# (handy if GitHub's scheduled cron is asleep). The dashboard's in-page
# "⏱ Auto-poll" toggle is the primary hands-off option; this is just a manual
# `gh workflow run` convenience. It only TRIGGERS the cloud job (no local work).
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
LOG="$HOME/Library/Logs/pmpoller.log"
REPO="PeeChug/polymarket-copy-signal"

ts() { date '+%Y-%m-%d %H:%M:%S'; }
if ! command -v gh >/dev/null 2>&1; then
  echo "$(ts) ERROR: gh not found on PATH" >> "$LOG"; exit 1
fi
if gh workflow run poller -R "$REPO" >>"$LOG" 2>&1; then
  echo "$(ts) poked poller OK" >> "$LOG"
else
  echo "$(ts) poke FAILED (see above)" >> "$LOG"
fi
