"""`/model` — choose which model answers in this session.

Scope is the session, so switching to Haiku for a cheap chore does not quietly
follow you into the next piece of real work. The chosen model is remembered on
disk and comes back with the session after a restart.

The list is filtered by the Claude Code version this bot actually spawns: a
model the bundled CLI is too old for is shown with the reason rather than
silently missing.
"""

from __future__ import annotations

import html
from typing import Optional

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..utils import models
from .session_choice import SessionSetting

logger = structlog.get_logger()

SETTING = SessionSetting("model", "claude_model")
PENDING_KEY = SETTING.pending_key


def get_store(context):
    return SETTING.store(context)


def current_model_id(context) -> Optional[str]:
    return SETTING.current(context)


def bind_pending(context, session_id: Optional[str]) -> None:
    SETTING.bind_pending(context, session_id)


def _cli_version(context) -> Optional[str]:
    """The Claude Code this bot spawns — it decides what may be offered."""
    settings = context.bot_data.get("settings")
    path = getattr(settings, "claude_cli_path", None) if settings else None
    try:
        return models.cli_version(path)
    except Exception as exc:
        logger.debug("model: cli version lookup failed", error=str(exc))
        return None


def _listing(context) -> str:
    """The menu, marking whichever model is in force right now."""
    active = SETTING.effective(context)
    version = _cli_version(context)
    lines = ["🧠 <b>Model</b>", ""]
    for choice in models.available(version):
        mark = "▸" if choice.model_id == active else " "
        lines.append(
            f"{mark} <code>{choice.alias}</code> — {html.escape(choice.note)}"
        )
    blocked = models.unavailable(version)
    if blocked:
        lines.append("")
        lines.append("<i>Not selectable on this bot:</i>")
        for alias, reason in blocked.items():
            lines.append(f"  <code>{alias}</code> — {html.escape(reason)}")
    lines.append("")
    lines.append("Tap one, or send <code>/model &lt;name&gt;</code>.")
    return "\n".join(lines)


def _keyboard(context) -> InlineKeyboardMarkup:
    """One button per selectable model, the active one marked."""
    active = SETTING.effective(context)
    version = _cli_version(context)
    rows = []
    for choice in models.available(version):
        mark = "▸ " if choice.model_id == active else ""
        rows.append(
            [
                InlineKeyboardButton(
                    f"{mark}{choice.label}", callback_data=f"model:{choice.alias}"
                )
            ]
        )
    return InlineKeyboardMarkup(rows)


async def model_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Switch model from a tapped button."""
    query = update.callback_query
    if query is None or not query.data:
        return
    alias = query.data.split(":", 1)[1]
    choice = models.resolve(alias, _cli_version(context))
    if choice is None:
        await query.answer("Not available on this bot.")
        return

    session_id = SETTING.choose(context, choice.model_id)
    await query.answer(f"Model: {choice.label}")
    try:
        await query.edit_message_text(_listing(context), parse_mode="HTML",
                                      reply_markup=_keyboard(context))
    except Exception:
        logger.debug("model: could not redraw the list")
    logger.info("Model switched by button", model=choice.model_id,
                session_id=session_id)


async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the model list, or switch the current session to one of them."""
    args = context.args or []
    if not args:
        await update.message.reply_text(
            _listing(context), parse_mode="HTML", reply_markup=_keyboard(context)
        )
        return

    wanted = args[0]
    version = _cli_version(context)
    choice = models.resolve(wanted, version)
    if choice is None:
        reason = models.blocked_reason(wanted, version)
        if reason:
            text = (
                f"🚫 <code>{html.escape(wanted)}</code> is real but unusable here: "
                f"{html.escape(reason)}."
            )
        else:
            text = (
                f"❓ No model called <code>{html.escape(wanted)}</code>.\n\n"
                + _listing(context)
            )
        await update.message.reply_text(text, parse_mode="HTML")
        return

    session_id = SETTING.choose(context, choice.model_id)
    where = (
        f"session <code>{html.escape(session_id[:8])}</code>"
        if session_id
        else "the session your next message starts"
    )

    logger.info(
        "Model switched",
        model=choice.model_id,
        session_id=session_id,
        user_id=update.effective_user.id if update.effective_user else None,
    )
    await update.message.reply_text(
        f"🧠 Now <b>{html.escape(choice.label)}</b> for {where}.",
        parse_mode="HTML",
    )
