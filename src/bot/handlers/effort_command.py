"""`/effort` — choose how hard the model thinks in this session.

Same shape as `/model`: per session, a fixed list, remembered on disk, shown
in the banner. The one addition is `/effort auto`, which clears the choice and
lets the model decide for itself again — the state a session starts in, and
the one worth being able to get back to without starting a new session.
"""

from __future__ import annotations

import html
from typing import Optional

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from ..utils import efforts
from .session_choice import SessionSetting

logger = structlog.get_logger()

SETTING = SessionSetting("effort", "claude_effort")
PENDING_KEY = SETTING.pending_key


def get_store(context):
    return SETTING.store(context)


def current_effort(context) -> Optional[str]:
    return SETTING.current(context)


def bind_pending(context, session_id: Optional[str]) -> None:
    SETTING.bind_pending(context, session_id)


def _listing(context) -> str:
    """The menu, marking whichever level is in force right now."""
    active = SETTING.effective(context)
    lines = ["🎚 <b>Effort</b>", ""]
    auto_mark = "▸" if not active else " "
    lines.append(
        f"{auto_mark} <code>auto</code> — adaptive: the model decides how much "
        "to think, per turn (default)"
    )
    for choice in efforts.CHOICES:
        mark = "▸" if choice.alias == active else " "
        lines.append(
            f"{mark} <code>{choice.alias}</code> — {html.escape(choice.note)}"
        )
    lines.append("")
    lines.append("Send <code>/effort &lt;level&gt;</code> to switch this session.")
    return "\n".join(lines)


async def effort_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the effort levels, or set one for the current session."""
    args = context.args or []
    if not args:
        await update.message.reply_text(_listing(context), parse_mode="HTML")
        return

    wanted = args[0]

    if efforts.is_auto(wanted):
        SETTING.clear(context)
        logger.info(
            "Effort cleared",
            user_id=update.effective_user.id if update.effective_user else None,
        )
        await update.message.reply_text(
            "🎚 Effort back to <b>auto</b> — the model decides.",
            parse_mode="HTML",
        )
        return

    choice = efforts.resolve(wanted)
    if choice is None:
        await update.message.reply_text(
            f"❓ No effort level called <code>{html.escape(wanted)}</code>.\n\n"
            + _listing(context),
            parse_mode="HTML",
        )
        return

    session_id = SETTING.choose(context, choice.alias)
    where = (
        f"session <code>{html.escape(session_id[:8])}</code>"
        if session_id
        else "the session your next message starts"
    )

    logger.info(
        "Effort switched",
        effort=choice.alias,
        session_id=session_id,
        user_id=update.effective_user.id if update.effective_user else None,
    )
    await update.message.reply_text(
        f"🎚 Effort <b>{choice.alias}</b> for {where}.",
        parse_mode="HTML",
    )
