#!/bin/bash
# wake_after.sh — schedule a synthetic Telegram message after a delay.
#
# Usage:
#     ./wake_after.sh <delay_seconds> "<wake-up message>" [BOT_ID]
#     ./wake_after.sh 1500 "Check h12f_ising.log; push if done."
#     ./wake_after.sh 30   "RELAY: next step" work        # target the 'work' bot
#
# Runs under nohup so it outlives the calling shell. After `<delay_seconds>`,
# drops a wake file the bot's inject_watcher picks up and routes to the
# agentic-text handler — resuming the Claude session and acting on the prompt.
#
# Multi-bot:
#   BOT_ID (3rd arg or $BOT_ID env, default "main") selects which bot wakes.
#   - main : writes the legacy single file /tmp/claude_inject_message.json
#            (back-compat; the 'main' bot watches both this and its spool).
#   - other: writes a UNIQUE file into /tmp/claude_inject/<BOT_ID>/ via an
#            atomic write-tmp-then-mv, so concurrent wakes never clobber.
#   CHAT_ID resolves from $WAKE_CHAT_ID_<BOT_ID>, then $WAKE_CHAT_ID, then the
#   default allowed user.

set -euo pipefail

DELAY="${1:-300}"
MESSAGE="${2:-Wake up}"
BOT_ID="${3:-${BOT_ID:-main}}"

# Per-bot chat-id override: WAKE_CHAT_ID_<BOT_ID> > WAKE_CHAT_ID > default.
_perbot_var="WAKE_CHAT_ID_${BOT_ID}"
CHAT_ID="${!_perbot_var:-${WAKE_CHAT_ID:-8610757705}}"

INJECT_BASE="${INJECT_DIR:-/tmp/claude_inject}"
SPOOL_DIR="${INJECT_BASE}/${BOT_ID}"
LEGACY_FILE="/tmp/claude_inject_message.json"
LOG_FILE="/tmp/wake_after.log"

# JSON-escape the message once, up front.
MSG_JSON="$(printf '%s' "$MESSAGE" | python3 -c 'import json, sys; print(json.dumps(sys.stdin.read()))')"

nohup bash -c "
    sleep ${DELAY}
    echo \"[\$(date -u)] firing wake after ${DELAY}s (bot=${BOT_ID})\" >> ${LOG_FILE}
    PAYLOAD='{ \"chat_id\": ${CHAT_ID}, \"text\": ${MSG_JSON} }'
    if [ \"${BOT_ID}\" = \"main\" ]; then
        # Legacy single file (atomic via tmp+mv to avoid half-written reads).
        TMP=\"${LEGACY_FILE}.tmp.\$\$\"
        printf '%s' \"\$PAYLOAD\" > \"\$TMP\"
        mv -f \"\$TMP\" \"${LEGACY_FILE}\"
        echo \"[\$(date -u)] wrote ${LEGACY_FILE}\" >> ${LOG_FILE}
    else
        # Per-bot spool: unique filename, atomic mv into place.
        mkdir -p \"${SPOOL_DIR}\"
        NAME=\"\$(date +%s%N)-\$RANDOM\"
        TMP=\"${SPOOL_DIR}/.\${NAME}.tmp\"
        DST=\"${SPOOL_DIR}/\${NAME}.json\"
        printf '%s' \"\$PAYLOAD\" > \"\$TMP\"
        mv -f \"\$TMP\" \"\$DST\"
        echo \"[\$(date -u)] wrote \$DST\" >> ${LOG_FILE}
    fi
" > /dev/null 2>&1 &

disown
echo "wake scheduled in ${DELAY}s (pid=$!, bot=${BOT_ID})"
