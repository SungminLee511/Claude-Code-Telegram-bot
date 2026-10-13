"""`/branch` — continue this conversation on a copy, leaving the original be.

The CLI can replay a resumed session into a *new* session id instead of
appending to the old one. The copy has the whole history; the original stops
where it was. That turns an experiment you might regret into something you can
walk away from, rather than a permanent part of the transcript.

The fork happens on the **next message**, not on the command, because forking
is something the CLI does while handling a turn. Deliberate: a fork re-sends
the entire history, so doing it on a message you were going to send anyway
costs nothing extra, while doing it immediately would spend a full-context
turn on the word "branched".

So `/branch` only arms a one-shot flag. It lives in `user_data` rather than on
disk precisely because it should not survive a restart: a fork armed hours ago
and forgotten would silently split the conversation.
"""

from __future__ import annotations

import html
from typing import Optional

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from ..utils.session_rename import branch_name

logger = structlog.get_logger()

PENDING_KEY = "pending_branch"
DEFAULT_LABEL = "temp"


def pending_label(context) -> Optional[str]:
    """The branch label waiting to be used, if `/branch` was just run."""
    label = context.user_data.get(PENDING_KEY)
    return label if isinstance(label, str) and label else None


def is_armed(context) -> bool:
    return pending_label(context) is not None


def disarm(context) -> Optional[str]:
    """Take the label and clear the flag, so one command forks once."""
    label = pending_label(context)
    context.user_data[PENDING_KEY] = None
    return label


def finish(context, label: str, old_id: Optional[str], new_id: Optional[str]) -> Optional[str]:
    """Name the fork and carry the old session's settings onto it.

    Returns a note for the chat, or None if no fork actually happened — the
    CLI decides that, and a turn that reused the old id is not a branch no
    matter what was asked for.
    """
    if not new_id or not old_id or new_id == old_id:
        return None

    from ..utils.session_rename import set_title
    from .session_picker import get_index

    original = "session"
    name = None
    try:
        index = get_index(context)
        old = index.get(old_id, refresh=False)
        if old is not None:
            original = old.display_name
        name = branch_name(original, label)
        new = index.get(new_id)
        if new is not None:
            set_title(new.path, new_id, name)
    except Exception as exc:  # the fork succeeded; only the name failed
        logger.warning("branch: could not name the fork", error=str(exc))

    _carry_settings(context, old_id, new_id)

    logger.info("Branched", old_session=old_id, new_session=new_id, label=label)
    shown = name or label
    return (
        f"🌿 Branched to <b>{html.escape(shown)}</b>\n"
        f"<code>{html.escape(old_id[:8])}</code> is untouched — "
        "<code>/sessions</code> to go back."
    )


def _carry_settings(context, old_id: str, new_id: str) -> None:
    """Start the branch configured the way its original was.

    A fork that silently reverted to the default model would be a surprise in
    the one place you least want one: a copy you made to try something.
    """
    from .effort_command import SETTING as EFFORT
    from .model_command import SETTING as MODEL

    for setting in (MODEL, EFFORT):
        try:
            chosen = setting.store(context).get(old_id)
            if chosen:
                setting.store(context).set(new_id, chosen)
        except Exception as exc:
            logger.debug(
                f"branch: could not carry {setting.name}", error=str(exc)
            )


async def branch_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Arm a fork for the next message, or cancel one already armed."""
    args = context.args or []
    label = " ".join(args).strip()

    if label.lower() in ("cancel", "off", "abort"):
        if is_armed(context):
            disarm(context)
            await update.message.reply_text("🌿 Branch cancelled.")
        else:
            await update.message.reply_text("🌿 Nothing was waiting to branch.")
        return

    if not context.user_data.get("claude_session_id"):
        await update.message.reply_text(
            "🌿 Nothing to branch from yet — this chat has no session."
        )
        return

    label = label or DEFAULT_LABEL
    context.user_data[PENDING_KEY] = label

    from .session_picker import get_index

    original = "this session"
    try:
        info = get_index(context).get(context.user_data["claude_session_id"])
        if info is not None:
            original = info.display_name
    except Exception as exc:  # naming is decoration; the fork still works
        logger.debug("branch: could not read the original name", error=str(exc))

    logger.info(
        "Branch armed",
        label=label,
        user_id=update.effective_user.id if update.effective_user else None,
    )
    await update.message.reply_text(
        "🌿 Next message starts a branch.\n"
        f"It will be called <b>{html.escape(branch_name(original, label))}</b>.\n"
        "The original is left exactly as it is — <code>/sessions</code> to go "
        "back, <code>/branch cancel</code> to call it off.",
        parse_mode="HTML",
    )
