#!/usr/bin/env bash
# One-shot: make the 'main' bot auto-resume session b60aa88d on next message.
#  1. kill main bot (this also kills the current Claude session)
#  2. rewrite sessions table so b60aa88d is the newest row
#  3. start main bot again (user_data is in-memory -> cleared -> auto-resume)
set -u
cd /home/TRASHCAN/telegram_bot || exit 1

TARGET="b60aa88d-75ef-4d09-b4fc-f06e13d56c3a"
DROP="86dd6077-c6c5-49b5-ad8c-2da171e00a53"

matching_pids() {
    pgrep -af "[s]rc\.main" 2>/dev/null \
        | grep -v -- "--config-file" \
        | grep -v "main_codex" \
        | awk '{print $1}'
}

PIDS=$(matching_pids)
echo "[become] killing main bot pids: $PIDS"
[ -n "$PIDS" ] && kill $PIDS 2>/dev/null
for _ in $(seq 1 10); do [ -z "$(matching_pids)" ] && break; sleep 1; done
REM=$(matching_pids); [ -n "$REM" ] && kill -9 $REM 2>/dev/null
sleep 2

python3 - "$TARGET" "$DROP" <<'PY'
import sqlite3, sys
from datetime import datetime, timezone
target, drop = sys.argv[1], sys.argv[2]
c = sqlite3.connect('data/bot.db')
c.execute("DELETE FROM sessions WHERE session_id=?", (drop,))
now = datetime.now(timezone.utc).isoformat()
c.execute("UPDATE sessions SET last_used=?, is_active=1 WHERE session_id=?", (now, target))
c.commit()
print("[become] rows now:")
for r in c.execute("SELECT session_id,last_used,message_count,is_active FROM sessions ORDER BY last_used DESC LIMIT 3"):
    print("   ", r)
PY

echo "[become] starting main bot..."
nohup /home/sky/miniconda3/envs/Telegram-bot-env/bin/python -u -m src.main > bot.log 2>&1 &
disown $! 2>/dev/null
sleep 3
matching_pids | sed 's/^/[become] new pid /'
echo "[become] done"
