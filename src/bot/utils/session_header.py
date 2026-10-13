"""Session banner prepended to Claude's replies.

Purely cosmetic and purely bot-side: the banner is spliced in *after* Claude
has produced its answer, so it never enters the prompt, the transcript, or the
model's context. It exists so a human reading Telegram can tell which of the
many sessions (and which bot) a reply came from.

Shape (style A, as a footer):

    <claude's reply>
    ──────────────
    🧵 Analyze Claude code Telegram bot repo architecture
       27 msg · opus-5 · auto · bot2
       5h 8%: 13:40 (38%)
       week 25%: Sun 11:00 (28%)
"""

from __future__ import annotations

import html
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import structlog

from .durations import format_elapsed

logger = structlog.get_logger()

_RULE = "──────────────"
_MAX_TITLE = 58
# Telegram hard-caps a message at 4096 chars; the formatter already splits at
# 4000, so a banner only fits inline when there is headroom.
_TELEGRAM_LIMIT = 4096

_KST = timezone(timedelta(hours=9))
_MAX_WAKE_LINES = 3
_MAX_WAKE_MESSAGE = 44
# A wake whose timer has just run out is still a real process for a moment
# while it writes its file. Keep showing it rather than blinking it away.
_WAKE_GRACE = 5.0


def _esc(text: str) -> str:
    return html.escape(text or "")


def _info_for(context, session_id: str):
    """The indexed transcript record, or None if it cannot be looked up."""
    try:
        from ..handlers.session_picker import get_index

        return get_index(context).get(session_id)
    except Exception as exc:  # index is best-effort decoration
        logger.debug("session_header: index lookup failed", error=str(exc))
        return None


def _started_text(info, now: Optional[float] = None) -> Optional[str]:
    """When this session began, in the user's timezone.

    The date is added once the session is no longer from today — a bare
    `05:33` on a conversation three days old reads as this morning.
    """
    if info is None:
        return None
    try:
        from .session_start import started_at

        begun = started_at(getattr(info, "path", None))
    except Exception as exc:  # decoration only
        logger.debug("session_header: start lookup failed", error=str(exc))
        return None
    if begun is None:
        return None

    moment = datetime.fromtimestamp(begun, tz=_KST)
    today = datetime.fromtimestamp(
        time.time() if now is None else now, tz=_KST
    ).date()
    if moment.date() == today:
        return moment.strftime("%H:%M")
    return moment.strftime("%b %d %H:%M")


def _usage_line() -> Optional[str]:
    """How much of the 5-hour and weekly limits is spent, as meters.

    Account-wide rather than per-chat: the limit is on the subscription, so
    every bot and every session draws from the same pool.
    """
    try:
        from . import usage_limits

        return usage_limits.line()
    except Exception as exc:  # decoration only
        logger.debug("session_header: usage lookup failed", error=str(exc))
        return None


_TOKENS_KEY = "context_tokens"


def _context_tokens(context, claude_response=None) -> Optional[int]:
    """Context size for this reply, remembered for the ones in between.

    The count arrives with a reply, but the banner is also drawn for messages
    that did not come from Claude. Keeping the last known number means those
    show the same figure rather than silently dropping it.
    """
    tokens = getattr(claude_response, "context_tokens", None)
    if isinstance(tokens, int) and tokens > 0:
        context.user_data[_TOKENS_KEY] = tokens
        return tokens
    remembered = context.user_data.get(_TOKENS_KEY)
    return remembered if isinstance(remembered, int) and remembered > 0 else None


def _compaction_suffix(info) -> str:
    """` (3)` when the session has been summarised, empty when it has not.

    It rides on the token count because the two are the same story: this is
    how much context is held *now*, and that is how many times the rest was
    thrown away to make room for it.
    """
    if info is None:
        return ""
    try:
        from .compactions import count

        total = count(getattr(info, "path", None))
    except Exception as exc:  # decoration only
        logger.debug("session_header: compaction count failed", error=str(exc))
        return ""
    return f" ({total})" if total else ""


def format_tokens(tokens: int) -> str:
    """`441700` -> `442k tok`; below a thousand the exact number still fits.

    Shared with the session list so both places say the size the same way.
    """
    if tokens < 1000:
        return f"{tokens} tok"
    return f"{tokens / 1000:.0f}k tok"


_format_tokens = format_tokens


def started_suffix(info, now: Optional[float] = None) -> str:
    """` (14:33)` for a session's start, or empty when it cannot be read."""
    started = _started_text(info, now)
    return f" ({started})" if started else ""


def _effort_label(context) -> Optional[str]:
    """How hard this session thinks — always shown, `auto` included.

    An omitted setting is indistinguishable from a broken one, and `auto` is
    a real choice rather than the absence of one.
    """
    try:
        from ..handlers.effort_command import SETTING
        from .efforts import label_for

        return label_for(SETTING.effective(context))
    except Exception as exc:  # decoration only
        logger.debug("session_header: effort lookup failed", error=str(exc))
        return None


def _model_label(context) -> Optional[str]:
    """Which model answered, as a short name — the session's or the default."""
    try:
        from ..handlers.model_command import current_model_id

        settings = context.bot_data.get("settings")
        configured = getattr(settings, "claude_model", None) if settings else None
        from .models import label_for

        return label_for(current_model_id(context) or configured)
    except Exception as exc:  # decoration only
        logger.debug("session_header: model lookup failed", error=str(exc))
        return None


def build_header(context, claude_response=None) -> Optional[str]:
    """Build the HTML banner for the chat's current session, or None."""
    session_id = context.user_data.get("claude_session_id")
    if not session_id and claude_response is not None:
        session_id = getattr(claude_response, "session_id", None)
    if not session_id:
        return None

    settings = context.bot_data.get("settings")
    bot_id = getattr(settings, "bot_id", "main") if settings else "main"

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

    info = _info_for(context, session_id)
    title = (getattr(info, "display_name", None) if info else None) or (
        "(untitled session)"
    )
    if len(title) > _MAX_TITLE:
        title = title[: _MAX_TITLE - 1] + "…"
    title = f"{title}{started_suffix(info)}"

    # The session id and the working directory are deliberately absent: the
    # title already says which conversation this is, and the directory is
    # almost always the same one, so both were noise on every single reply.
    bits = []
    tokens = _context_tokens(context, claude_response)
    if tokens:
        bits.append(_format_tokens(tokens) + _compaction_suffix(info))
    elif message_count:
        # Before the first reply of a restarted process there is no token
        # count yet; the turn count is better than an empty space.
        bits.append(f"{message_count} msg")
    model = _model_label(context)
    if model:
        bits.append(_esc(model))
    effort = _effort_label(context)
    if effort:
        bits.append(_esc(effort))
    bits.append(_esc(bot_id))

    header = f"🧵 <b>{_esc(title)}</b>\n   {' · '.join(bits)}"
    usage = _usage_line()
    if usage:
        # One limit per line, each indented to sit under the detail line.
        body = "\n".join(f"   {_esc(line)}" for line in usage.splitlines())
        header = f"{header}\n{body}"
    try:
        wakes = _wake_lines(bot_id)
    except Exception as exc:  # the wake list must never cost us the banner
        logger.debug("session_header: wake scan failed", error=str(exc))
        wakes = None
    return f"{header}\n{wakes}" if wakes else header


def _wake_lines(bot_id: str, now: Optional[float] = None) -> Optional[str]:
    """The wake-ups this bot still has queued, or None if there are none.

    Only this bot's wakes: another bot's queue says nothing about what will
    happen in *this* chat, and the point of the line is to answer "is anything
    going to wake me, and when".
    """
    from . import pending_wakes

    now = time.time() if now is None else now
    found = pending_wakes.scan_quietly(bot_id)
    wakes = [w for w in found if w.seconds_left(now) > -_WAKE_GRACE]
    if not wakes:
        return None

    plural = "wake" if len(wakes) == 1 else "wakes"
    lines = [f"   ⏰ {len(wakes)} {plural}"]
    for wake in wakes[:_MAX_WAKE_LINES]:
        when = _kst_clock(wake.fires_at)
        left = format_elapsed(max(0, wake.seconds_left(now)))
        text = _one_line(wake.message)
        if len(text) > _MAX_WAKE_MESSAGE:
            text = text[: _MAX_WAKE_MESSAGE - 1] + "…"
        suffix = f" · {_esc(text)}" if text else ""
        lines.append(f"      {when} KST ({left}){suffix}")

    hidden = len(wakes) - _MAX_WAKE_LINES
    if hidden > 0:
        lines.append(f"      +{hidden} more")
    return "\n".join(lines)


def _one_line(text: str) -> str:
    """Collapse a multi-line wake prompt so it cannot break the banner."""
    return " ".join((text or "").split())


def _kst_clock(unix_seconds: float) -> str:
    """HH:MM in the user's timezone; the server runs on UTC."""
    moment = datetime.fromtimestamp(unix_seconds, tz=_KST)
    return moment.strftime("%H:%M")


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
