"""Asking the user a question mid-turn, with buttons.

`AskUserQuestion` was dead over Telegram: there was no UI to draw the choices,
so it was denied and the model had to ask in prose and end its turn, costing a
full round trip for every decision.

It works because `can_use_tool` is awaited *inside* the turn. The callback can
post a keyboard, block on a future, and return once a button is tapped — the
turn never ends, so the model keeps its context and carries straight on.

Every question carries an **Other** button, added here rather than by the
caller: the tool's contract is that the escape hatch is always present, and
leaving it to be remembered each time means it will sometimes be missing on
exactly the question that needed it.

Tapping Other does not collect text — it cannot, see below — it releases the
turn so the model asks the same question in prose, where any answer is
possible. One tap costs a round trip; being stuck with four wrong options
costs more.

*Typing works too.* A message sent while a question is waiting is taken as
the answer rather than queued behind the turn — see `answer_with_text` and
the priority path in `UpdateProcessor`. Without that bypass a typed reply
would sit behind the very lock this turn holds, and arrive minutes later as
a new request.

*No tool result.* `can_use_tool` may allow or deny, never hand back a result.
So the answer travels in the deny message, which the model reads as the
outcome of its call. Impure, but it is the only channel there is.

A question that is never answered must not wedge the turn forever, so every
one carries a deadline; when it passes, the model is told to ask in prose
instead and the conversation degrades to exactly what it did before.
"""

from __future__ import annotations

import asyncio
import html
import secrets
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

logger = structlog.get_logger()

CALLBACK_PREFIX = "ask:"
DEFAULT_TIMEOUT = 300.0

# Telegram caps callback data at 64 bytes, so the token is short and the
# options are addressed by index rather than by label.
_TOKEN_BYTES = 4
_MAX_OPTIONS = 12
_MAX_LABEL = 48

# Marks the always-present escape hatch, rather than an index into the options.
# It takes two taps: the button sits in the bottom row, right above the text
# field, so reaching for the keyboard lands on it — and a stray tap would
# otherwise throw the question away and cost a round trip. One confirmation is
# cheaper than that, for an action nobody takes often.
_OTHER = "x"
_OTHER_CONFIRM = "xx"
OTHER_LABEL = "✏️ Other — let me type"
_CONFIRM_LABEL = "✏️ Yes, let me type"
# What the model is told when Other is chosen. A sentence it can act on beats
# an empty result: the generic "could not ask" fallback reads like a failure,
# when in fact the user answered — they said "none of these".
OTHER_ANSWER = "Other: Ask again in plain text"
_BACK_LABEL = "↩︎ Back to the options"


@dataclass
class _Pending:
    labels: List[str]
    # int: an option index, or -1 for Other. str: the user typed an answer.
    future: "asyncio.Future" = field(repr=False)
    question: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Tap:
    """What a tapped button means, for the handler to act on.

    `kind` is `answered` when the question is settled, `confirm` when the
    keyboard should be redrawn to check the user meant Other, and `closed`
    for a button belonging to a question that is no longer waiting.
    """

    kind: str
    label: Optional[str] = None
    text: Optional[str] = None
    keyboard: Optional[InlineKeyboardMarkup] = None


_pending: Dict[str, _Pending] = {}


def _token() -> str:
    return secrets.token_hex(_TOKEN_BYTES)


def _labels_of(question: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for option in question.get("options") or []:
        if isinstance(option, dict):
            label = option.get("label")
        else:
            label = option
        if isinstance(label, str) and label.strip():
            out.append(label.strip())
    return out[:_MAX_OPTIONS]


def _keyboard(token: str, labels: List[str]) -> InlineKeyboardMarkup:
    """One button per line: option labels are sentences, not words.

    The last row is always Other, so none of the offered answers can become a
    trap when none of them fit.
    """
    rows = [
        [
            InlineKeyboardButton(
                label[:_MAX_LABEL], callback_data=f"{CALLBACK_PREFIX}{token}:{index}"
            )
        ]
        for index, label in enumerate(labels)
    ]
    rows.append(
        [
            InlineKeyboardButton(
                OTHER_LABEL, callback_data=f"{CALLBACK_PREFIX}{token}:{_OTHER}"
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def _prompt_text(question: Dict[str, Any], labels: List[str]) -> str:
    text = question.get("question") or question.get("header") or "Which one?"
    lines = [f"❓ <b>{html.escape(str(text))}</b>"]

    descriptions = []
    for option in question.get("options") or []:
        if isinstance(option, dict) and option.get("description"):
            descriptions.append(
                (option.get("label") or "", str(option["description"]))
            )
    if descriptions:
        lines.append("")
        for label, description in descriptions[:_MAX_OPTIONS]:
            lines.append(
                f"<b>{html.escape(str(label))}</b> — {html.escape(description)}"
            )
    if question.get("multiSelect"):
        lines.append("")
        lines.append("<i>Pick the single most important one.</i>")
    return "\n".join(lines)


def _confirm_keyboard(token: str) -> InlineKeyboardMarkup:
    """Back on top, so the undo is the one further from the text field."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    _BACK_LABEL, callback_data=f"{CALLBACK_PREFIX}{token}:back"
                )
            ],
            [
                InlineKeyboardButton(
                    _CONFIRM_LABEL,
                    callback_data=f"{CALLBACK_PREFIX}{token}:{_OTHER_CONFIRM}",
                )
            ],
        ]
    )


def tap(data: str) -> Tap:
    """Interpret a tapped button against the question still waiting."""
    if not data.startswith(CALLBACK_PREFIX):
        return Tap("closed")
    rest = data[len(CALLBACK_PREFIX) :]
    token, _, index_text = rest.partition(":")
    waiting = _pending.get(token)
    if waiting is None:
        return Tap("closed")

    if index_text == _OTHER:
        return Tap(
            "confirm",
            label="Sure?",
            text="❓ Answer in your own words instead?",
            keyboard=_confirm_keyboard(token),
        )

    if index_text == "back":
        return Tap(
            "confirm",
            label="Back",
            text=_prompt_text(waiting.question, waiting.labels),
            keyboard=_keyboard(token, waiting.labels),
        )

    if index_text == _OTHER_CONFIRM:
        if not waiting.future.done():
            waiting.future.set_result(-1)
        return Tap("answered", label=OTHER_LABEL)

    try:
        index = int(index_text)
    except ValueError:
        return Tap("closed")
    if not 0 <= index < len(waiting.labels):
        return Tap("closed")
    if not waiting.future.done():
        waiting.future.set_result(index)
    return Tap("answered", label=waiting.labels[index])


def resolve(data: str) -> Optional[str]:
    """Settle a question from a tapped button; the chosen label, or None."""
    result = tap(data)
    return result.label if result.kind == "answered" else None


async def ask(message, payload: Dict[str, Any], timeout: float = DEFAULT_TIMEOUT) -> Optional[str]:
    """Put each question to the user and return the answers as text.

    `message` is the Telegram message to reply under. None means the question
    could not be asked at all — no options, or nobody answered in time.
    """
    questions = payload.get("questions")
    if not isinstance(questions, list) or not questions:
        return None

    answers: List[str] = []
    for question in questions:
        if not isinstance(question, dict):
            continue
        labels = _labels_of(question)
        if not labels:
            continue
        chosen = await _ask_one(message, question, labels, timeout)
        if chosen is None:
            return None
        asked = question.get("question") or question.get("header") or "question"
        answers.append(f"{asked} → {chosen}")

    return "\n".join(answers) if answers else None


async def _ask_one(
    message, question: Dict[str, Any], labels: List[str], timeout: float
) -> Optional[str]:
    token = _token()
    loop = asyncio.get_running_loop()
    waiting = _Pending(labels=labels, future=loop.create_future(), question=question)
    _pending[token] = waiting

    sent = None
    try:
        sent = await message.reply_text(
            _prompt_text(question, labels),
            parse_mode="HTML",
            reply_markup=_keyboard(token, labels),
        )
        index = await asyncio.wait_for(waiting.future, timeout=timeout)
        if isinstance(index, str):
            # Typed rather than tapped: use the words as given.
            await _close(sent, f"✏️ <b>Answered:</b> {html.escape(index)}")
            return index
        if index < 0:
            # Other: none of these fit. Say so as the answer rather than
            # returning nothing, so the model re-asks instead of guessing why
            # its question vanished.
            logger.info("ask_user: user chose to answer in prose", token=token)
            await _close(sent, "✏️ Asked again in plain text.")
            return OTHER_ANSWER
        chosen = labels[index]
    except asyncio.TimeoutError:
        logger.info("ask_user: question timed out", token=token)
        await _close(sent, "⌛ No answer — carrying on.")
        return None
    except Exception as exc:
        logger.warning("ask_user: could not ask", error=str(exc))
        return None
    finally:
        _pending.pop(token, None)

    await _close(sent, f"❓ <b>Answered:</b> {html.escape(chosen)}")
    return chosen


async def _close(sent, text: str) -> None:
    """Replace the keyboard with the outcome, so it cannot be tapped twice."""
    if sent is None:
        return
    try:
        await sent.edit_text(text, parse_mode="HTML")
    except Exception:
        logger.debug("ask_user: could not clear the keyboard")


def answer_with_text(text: str) -> bool:
    """Take a typed message as the answer to the waiting question.

    Returns whether it was used. Only one question is ever outstanding — a
    multi-question ask puts them one at a time — so there is no ambiguity
    about which this belongs to.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return False
    for token, waiting in list(_pending.items()):
        if waiting.future.done():
            continue
        waiting.future.set_result(cleaned)
        logger.info("ask_user: answered by typing", token=token)
        return True
    return False


def pending_count() -> int:
    """How many questions are waiting — for tests and diagnostics."""
    return len(_pending)


def clear() -> None:
    """Drop every waiting question — for tests."""
    for waiting in _pending.values():
        if not waiting.future.done():
            waiting.future.cancel()
    _pending.clear()
