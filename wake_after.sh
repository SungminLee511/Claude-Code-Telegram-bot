#!/bin/bash
# wake_after.sh — schedule a synthetic Telegram message after a delay.
#
# Usage:
#     ./wake_after.sh <delay_seconds> "<wake-up message>"
#     ./wake_after.sh 1500 "Check h12f_ising.log; push if done; queue Cycle 36."
#
# Runs under nohup so it outlives the calling shell. After `<delay_seconds>`,
# writes /tmp/claude_inject_message.json with chat_id=$CHAT_ID and the given
# text. The bot's inject_watcher (added in src/bot/inject_watcher.py) picks
# it up and routes the message to the agentic-text handler — resuming the
# Claude session and acting on the prompt.

set -euo pipefail

DELAY="${1:-300}"
MESSAGE="${2:-Wake up}"
# Default chat = the allowed user from .env (currently 8610757705).
CHAT_ID="${WAKE_CHAT_ID:-8610757705}"
INJECT_FILE="/tmp/claude_inject_message.json"

LOG_FILE="/tmp/wake_after.log"

nohup bash -c "
    sleep ${DELAY}
    echo \"[\$(date -u)] firing wake after ${DELAY}s\" >> ${LOG_FILE}
    cat > ${INJECT_FILE} <<EOF
{
  \"chat_id\": ${CHAT_ID},
  \"text\": $(printf '%s' "$MESSAGE" | python3 -c 'import json, sys; print(json.dumps(sys.stdin.read()))')
}
EOF
    echo \"[\$(date -u)] wrote ${INJECT_FILE}\" >> ${LOG_FILE}
" > /dev/null 2>&1 &

disown
echo "wake scheduled in ${DELAY}s (pid=$!)"
