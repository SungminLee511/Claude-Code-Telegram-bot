"""Synthetic-message injector for self-wake.

Watches a file on disk; when the file appears, reads JSON of the form
`{"chat_id": int, "text": str, "user_id": int (optional)}`, builds a
synthetic Telegram Update with that text, and pushes it into the bot's
update queue. The bot then processes it exactly as if a real user sent it.

Use case: a nohup'd bash script after a long-running experiment sleeps for
N minutes, then writes the trigger file. The bot picks it up, routes the
message to the agentic-text handler, which resumes the Claude session and
acts on the "wake up" prompt.

File path is `/tmp/claude_inject_message.json` by default (configurable).
After processing, the file is renamed to `<path>.processed-<timestamp>` so
it isn't re-fired and so audit history is preserved.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Optional

import structlog
from telegram import Update
from telegram.ext import Application

logger = structlog.get_logger()


DEFAULT_INJECT_PATH = Path("/tmp/claude_inject_message.json")
POLL_INTERVAL_SECONDS = 2.0


def _build_synthetic_update_payload(chat_id: int, text: str,
                                     user_id: Optional[int] = None,
                                     first_name: str = "AutoWake",
                                     message_id: Optional[int] = None) -> dict:
    """Construct a Telegram Update payload that looks like a private text message."""
    if user_id is None:
        user_id = chat_id
    if message_id is None:
        message_id = int(time.time() * 1000) % (2 ** 31)
    now = int(time.time())
    return {
        "update_id": int(time.time() * 1000) % (2 ** 31),
        "message": {
            "message_id": message_id,
            "date": now,
            "chat": {"id": chat_id, "type": "private"},
            "from": {
                "id": user_id,
                "is_bot": False,
                "first_name": first_name,
            },
            "text": text,
        },
    }


def _patch_message_replies(msg, bot) -> None:
    """No-op for now. The fix for `reply_to_message_id` failures on synthetic
    messages is handled globally by setting `reply_quote=False` in settings
    (telegram_bot/.env), which sets `Defaults(do_quote=False)` so reply_text
    no longer auto-attaches reply_to_message_id."""
    return None


async def inject_watcher_loop(app: Application,
                               inject_path: Path = DEFAULT_INJECT_PATH,
                               poll_seconds: float = POLL_INTERVAL_SECONDS,
                               stop_event: Optional[asyncio.Event] = None) -> None:
    """Background task: poll for the inject file, push synthetic Updates."""
    logger.info("inject_watcher started",
                 path=str(inject_path), poll_seconds=poll_seconds)

    bot = app.bot

    while True:
        if stop_event is not None and stop_event.is_set():
            logger.info("inject_watcher stopping (stop_event)")
            return
        try:
            if inject_path.exists():
                # Read and immediately rename so we never double-process.
                processed_path = inject_path.with_suffix(
                    f".processed-{int(time.time())}"
                )
                try:
                    raw = inject_path.read_text()
                    os.rename(inject_path, processed_path)
                except FileNotFoundError:
                    # Race condition: another loop picked it up.
                    raw = None

                if raw is not None:
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError as e:
                        logger.warning("inject_watcher: bad JSON",
                                        error=str(e), raw=raw[:200])
                        payload = None

                    if payload is not None:
                        chat_id = int(payload.get("chat_id", 0))
                        text = str(payload.get("text", "")).strip()
                        user_id = payload.get("user_id")
                        if user_id is not None:
                            user_id = int(user_id)
                        if not text or not chat_id:
                            logger.warning(
                                "inject_watcher: missing chat_id or text",
                                payload=payload,
                            )
                        else:
                            # Send a placeholder bot-message first to get a
                            # REAL message_id from Telegram. The orchestrator
                            # explicitly uses `update.message.message_id` as
                            # reply target in many places, so the synthetic
                            # message MUST carry a real message_id.
                            placeholder = await bot.send_message(
                                chat_id=chat_id,
                                text=f"[auto-wake: {text[:80]}]",
                            )
                            real_msg_id = placeholder.message_id

                            update_dict = _build_synthetic_update_payload(
                                chat_id=chat_id, text=text, user_id=user_id,
                                message_id=real_msg_id,
                            )
                            update = Update.de_json(update_dict, bot)
                            await app.update_queue.put(update)
                            logger.info(
                                "inject_watcher fired synthetic message",
                                chat_id=chat_id,
                                text_preview=text[:80],
                                real_msg_id=real_msg_id,
                            )
        except Exception as e:
            logger.error("inject_watcher loop error", error=str(e))

        await asyncio.sleep(poll_seconds)
