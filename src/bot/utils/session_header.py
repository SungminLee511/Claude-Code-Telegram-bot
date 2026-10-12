"""Session banner prepended to Claude's replies.

Purely cosmetic and purely bot-side: the banner is spliced in *after* Claude
has produced its answer, so it never enters the prompt, the transcript, or the
model's context. It exists so a human reading Telegram can tell which of the
many sessions (and which bot) a reply came from.

Shape (style A, as a footer):

    <claude's reply>
    ──────────────
    🧵 Analyze Claude code Telegram bot repo architecture
       49303422 · 27 msg · /home · bot main
"""

from __future__ import annotations

import html
from pathlib import Path
from typing import List, Optional

import structlog

logger = structlog.get_logger()

_RULE = "──────────────"
_MAX_TITLE = 58
# Telegram hard-caps a message at 4096 chars; the formatter already splits at
# 4000, so a banner only fits inline when there is headroom.
_TELEGRAM_LIMIT = 4096


def _esc(text: str) -> str:
    return html.escape(text or "")


def _title_for(context, session_id: str) -> Optional[str]:
    """Claude's own `ai-title` for this session, if the transcript has one."""
    try:
        from ..handlers.session_picker import get_index

        info = get_index(context).get(session_id)
    except Exception as exc:  # index is best-effort decoration
        logger.debug("session_header: title lookup failed", error=str(exc))
        return None
    if info is None:
        return None
    return info.display_name


def build_header(context, claude_response=None) -> Optional[str]:
    """Build the HTML banner for the chat's current session, or None."""
    session_id = context.user_data.get("claude_session_id")
    if not session_id and claude_response is not None:
        session_id = getattr(claude_response, "session_id", None)
    if not session_id:
        return None

    settings = context.bot_data.get("settings")
    bot_id = getattr(settings, "bot_id", "main") if settings else "main"

    cwd = context.user_data.get("current_directory")
    if cwd is None and settings is not None:
        cwd = settings.approved_directory
    cwd_text = str(cwd) if cwd else "?"

    # Message count lives on the SessionManager's cached session. Cost is
    # deliberately not shown: the SDK only reports the cost of turns this bot
    # process ran, so for a resumed session it is wrong by an unknown margin.
    message_count = None
    claude_integration = context.bot_data.get("claude_integration")
    manager = getattr(claude_integration, "session_manager", None)
    session = None
    if manager is not None:
        session = manager.active_sessions.get(session_id)
    if session is not None:
        message_count = session.message_count

    title = _title_for(context, session_id) or "(untitled session)"
    if len(title) > _MAX_TITLE:
        title = title[: _MAX_TITLE - 1] + "…"

    bits = [f"<code>{_esc(session_id[:8])}</code>"]
    if message_count:
        bits.append(f"{message_count} msg")
    bits.append(f"<code>{_esc(_shorten_path(cwd_text))}</code>")
    bits.append(f"bot {_esc(bot_id)}")

    return f"🧵 <b>{_esc(title)}</b>\n   {' · '.join(bits)}"


def _shorten_path(path_text: str) -> str:
    """`/home/RESEARCH/iasbs` -> `…/RESEARCH/iasbs` when long."""
    if len(path_text) <= 32:
        return path_text
    parts = Path(path_text).parts
    return "…/" + "/".join(parts[-2:]) if len(parts) > 2 else path_text


def attach_header(messages: List, context, claude_response=None) -> List:
    """Append the banner to the last message of a formatted reply.

    It sits at the end so the answer itself is what you read first; the
    banner is a footer answering "which session was that?". Only the final
    part of a multi-part reply carries it. If that part is already close to
    Telegram's limit the banner is sent as its own short message instead of
    risking a send failure.
    """
    if not messages:
        return messages

    settings = context.bot_data.get("settings")
    if settings is not None and not getattr(settings, "session_header", True):
        return messages

    try:
        header = build_header(context, claude_response)
    except Exception as exc:  # never let decoration break a reply
        logger.debug("session_header: build failed", error=str(exc))
        return messages
    if not header:
        return messages

    footer = f"\n\n{_RULE}\n{header}"
    last = messages[-1]
    if last.parse_mode != "HTML":
        # The formatter emits HTML; anything else is unexpected, so keep the
        # banner separate rather than mixing parse modes.
        return _as_separate_message(messages, header)

    if len(last.text) + len(footer) <= _TELEGRAM_LIMIT:
        last.text = last.text + footer
        return messages

    return _as_separate_message(messages, header)


def _as_separate_message(messages: List, header: str) -> List:
    from .formatting import FormattedMessage

    return messages + [FormattedMessage(header, parse_mode="HTML")]
