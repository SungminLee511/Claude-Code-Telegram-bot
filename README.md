# Claude Code Telegram Bot

Telegram bot for remote Claude Code access.

## Setup

```bash
pip install -e .
```

## Configure

Copy `.env.example` to `.env` and fill in values.

## Run

```bash
# Foreground
python -m src.main

# Background (nohup)
nohup python -m src.main > bot.log 2>&1 &

# Check it's running
ps aux | grep "src.main"

# View logs
tail -f bot.log
```

## .env Reference

See `.env.example` for all options. Required fields:

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From @BotFather |
| `TELEGRAM_BOT_USERNAME` | Bot username (no @) |
| `APPROVED_DIRECTORY` | Working directory for Claude |
| `ALLOWED_USERS` | Comma-separated Telegram user IDs |
