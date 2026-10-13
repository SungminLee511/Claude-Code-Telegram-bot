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


def _listing(context) -> str:
    """The menu, marking whichever model is in force right now."""
    active = SETTING.effective(context)
    lines = ["🧠 <b>Model</b>", ""]
    for choice in models.CHOICES:
        mark = "▸" if choice.alias == active else " "
        lines.append(
            f"{mark} <code>{choice.alias}</code> — {html.escape(choice.note)}"
        )
    if active and active not in {c.alias for c in models.CHOICES}:
        lines.append(f"▸ <code>{html.escape(active)}</code> — typed name")
    lines.append("")
    lines.append(
        "Tap one for the newest of that family, or send "
        "<code>/model &lt;exact-name&gt;</code> for a specific model."
    )
    return "\n".join(lines)


def _keyboard(context) -> InlineKeyboardMarkup:
    """One button per family, the active one marked."""
    active = SETTING.effective(context)
    rows = []
    for choice in models.CHOICES:
        mark = "▸ " if choice.alias == active else ""
        rows.append(
            [
                InlineKeyboardButton(
                    f"{mark}{choice.alias}", callback_data=f"model:{choice.alias}"
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
    choice = models.resolve(alias)
    if choice is None:
        await query.answer("Unknown model.")
        return

    session_id = SETTING.choose(context, choice.alias)
    await query.answer(f"Model: {choice.alias}")
    try:
        await query.edit_message_text(_listing(context), parse_mode="HTML",
                                      reply_markup=_keyboard(context))
    except Exception:
        logger.debug("model: could not redraw the list")
    logger.info("Model switched by button", model=choice.alias,
                session_id=session_id)


async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the model list, or switch the current session to one of them."""
    args = context.args or []
    if not args:
        await update.message.reply_text(
            _listing(context), parse_mode="HTML", reply_markup=_keyboard(context)
        )
        return

    wanted = args[0].strip()
    checking = None
    choice = models.resolve(wanted)
    if choice is not None:
        model_id, label, warning = choice.alias, choice.alias, ""
    else:
        if not models.is_wellformed(wanted):
            await update.message.reply_text(
                f"❓ <code>{html.escape(wanted[:60])}</code> is not a model name.\n\n"
                + _listing(context),
                parse_mode="HTML",
            )
            return
        checking = await update.message.reply_text(
            f"🔎 Checking <code>{html.escape(wanted)}</code>…", parse_mode="HTML"
        )
        settings = context.bot_data.get("settings")
        ok, why = await models.probe(
            wanted, getattr(settings, "claude_cli_path", None)
        )
        if ok is False:
            await _say(
                checking, update,
                f"❌ <code>{html.escape(wanted)}</code>: {html.escape(why)}. "
                "Model unchanged.",
            )
            return
        model_id, label = wanted, models.label_for(wanted) or wanted
        warning = (
            f"\n⚠️ Could not verify it ({html.escape(why)}); "
            "if the next reply errors, pick another."
            if ok is None
            else ""
        )

    session_id = SETTING.choose(context, model_id)
    where = (
        f"session <code>{html.escape(session_id[:8])}</code>"
        if session_id
        else "the session your next message starts"
    )

    logger.info(
        "Model switched",
        model=model_id,
        session_id=session_id,
        user_id=update.effective_user.id if update.effective_user else None,
    )
    await _say(
        checking,
        update,
        f"🧠 Now <b>{html.escape(label)}</b> for {where}.{warning}",
    )


async def _say(placeholder, update: Update, text: str) -> None:
    """Edit the "Checking…" message if there is one, else reply."""
    if placeholder is not None:
        try:
            await placeholder.edit_text(text, parse_mode="HTML")
            return
        except Exception:
            pass
    await update.message.reply_text(text, parse_mode="HTML")
