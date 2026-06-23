#!/usr/bin/env bash
# Restart a Claude Code Telegram bot — multi-bot aware.
#
# Usage:
#     ./restart_bot.sh            # restart the default 'main' bot (legacy)
#     ./restart_bot.sh work       # restart only the 'work' bot
#     BOT_ID=work ./restart_bot.sh
#
# Restarting one bot NEVER touches another:
#   - main  : matches a bare `-m src.main` process WITHOUT --config-file,
#             launches `-m src.main` (identical to legacy), log -> bot.log.
#   - other : matches `src.main ... --config-file <BOT_ID>.env`,
#             launches with that config file, log -> bot_<BOT_ID>.log.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || { echo "[restart_bot] ERROR: cannot cd to $SCRIPT_DIR"; exit 1; }

BOT_ID="${1:-${BOT_ID:-main}}"
PYTHON_BIN="${PYTHON_BIN:-/home/sky/miniconda3/envs/Telegram-bot-env/bin/python}"

if [ "$BOT_ID" = "main" ]; then
    LOG_FILE="$SCRIPT_DIR/bot.log"
    LAUNCH_ARGS=""               # bare main, no config file (legacy behaviour)
    ENV_FILE=".env"
else
    LOG_FILE="$SCRIPT_DIR/bot_${BOT_ID}.log"
    ENV_FILE="${BOT_ID}.env"
    LAUNCH_ARGS="--config-file ${ENV_FILE}"
    if [ ! -f "$SCRIPT_DIR/$ENV_FILE" ]; then
        echo "[restart_bot] ERROR: config file $ENV_FILE not found for bot '$BOT_ID'"
        exit 1
    fi
fi

# Return PIDs belonging ONLY to this bot.
matching_pids() {
    if [ "$BOT_ID" = "main" ]; then
        # bare `src.main`, excluding any --config-file process AND the codex
        # bot (`src.main_codex`), which would otherwise match the substring.
        pgrep -af "[s]rc\.main" 2>/dev/null \
            | grep -v -- "--config-file" \
            | grep -v "main_codex" \
            | awk '{print $1}'
    else
        pgrep -f "[s]rc\.main.*--config-file[ =]*${ENV_FILE}" 2>/dev/null || true
    fi
}

echo "[restart_bot] (bot=$BOT_ID) Looking for existing process..."
PIDS=$(matching_pids)

if [ -n "$PIDS" ]; then
    echo "[restart_bot] Killing PIDs: $PIDS"
    kill $PIDS 2>/dev/null || true

    for _ in $(seq 1 10); do
        [ -z "$(matching_pids)" ] && break
        sleep 1
    done

    REMAINING=$(matching_pids)
    if [ -n "$REMAINING" ]; then
        echo "[restart_bot] Force killing: $REMAINING"
        kill -9 $REMAINING 2>/dev/null || true
        sleep 1
    fi
else
    echo "[restart_bot] No existing '$BOT_ID' process found."
fi

echo "[restart_bot] Starting new '$BOT_ID' process..."
# shellcheck disable=SC2086
nohup "$PYTHON_BIN" -u -m src.main $LAUNCH_ARGS > "$LOG_FILE" 2>&1 &
NEW_PID=$!
disown $NEW_PID 2>/dev/null || true

sleep 2

if kill -0 "$NEW_PID" 2>/dev/null; then
    echo "[restart_bot] Bot '$BOT_ID' started. PID=$NEW_PID, log=$LOG_FILE"
else
    echo "[restart_bot] ERROR: bot '$BOT_ID' failed to start. Check $LOG_FILE"
    exit 1
fi
