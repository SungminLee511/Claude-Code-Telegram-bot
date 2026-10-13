#!/usr/bin/env bash
# Emergency revert: put all 3 bots back on claude-opus-5 and restart them.
# Usage (via SSH if Telegram is dead):
#   bash <bot_repo>/revert_model.sh
# Optional: pass a different model, e.g. `bash revert_model.sh claude-opus-4-8`
set -euo pipefail
cd "$(dirname "$0")"

MODEL="${1:-claude-opus-5}"

for f in .env bot2.env bot3.env; do
  sed -i "s/^CLAUDE_MODEL=.*/CLAUDE_MODEL=${MODEL}/" "$f"
  echo "[revert] $f -> CLAUDE_MODEL=${MODEL}"
done

for b in bot2 bot3 main; do
  echo "[revert] restarting $b ..."
  bash restart_bot.sh "$b" || echo "[revert] WARN: $b restart returned nonzero"
done

echo "[revert] done. Model=${MODEL}. Send a Telegram message to verify."
