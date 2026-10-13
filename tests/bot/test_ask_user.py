"""Tests for asking the user a question mid-turn with buttons."""

import asyncio

import pytest

from src.bot.utils import ask_user


@pytest.fixture(autouse=True)
def clean():
    ask_user.clear()
    yield
    ask_user.clear()


class Sent:
    def __init__(self):
        self.text = None
        self.edited = None

    async def edit_text(self, text, **kwargs):
        self.edited = text


class Message:
    """Records the keyboard it was asked to show."""

    def __init__(self, fail=False):
        self.sent = []
        self.keyboards = []
        self.fail = fail

    async def reply_text(self, text, reply_markup=None, **kwargs):
        if self.fail:
            raise RuntimeError("telegram said no")
        self.sent.append(text)
        self.keyboards.append(reply_markup)
        return Sent()


def payload(*options, question="Which approach?", multi=False):
    return {
        "questions": [
            {
                "question": question,
                "header": "Approach",
                "multiSelect": multi,
                "options": [{"label": o, "description": f"about {o}"} for o in options],
            }
        ]
    }


def buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


async def wait_for(message, count):
    """Let the asker get as far as posting its `count`-th keyboard."""
    for _ in range(100):
        if len(message.keyboards) >= count:
            return message.keyboards[count - 1]
        await asyncio.sleep(0)
    raise AssertionError(f"keyboard {count} never appeared")


async def tap(message, count, index):
    """Press a button the way the callback handler would."""
    markup = await wait_for(message, count)
    return ask_user.resolve(buttons(markup)[index].callback_data)


# --- asking -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_answer_comes_back_as_text():
    message = Message()
    task = asyncio.create_task(ask_user.ask(message, payload("Rewrite", "Patch")))

    assert await tap(message, 1, 1) == "Patch"

    assert await task == "Which approach? → Patch"


@pytest.mark.asyncio
async def test_every_option_becomes_a_button():
    message = Message()
    task = asyncio.create_task(ask_user.ask(message, payload("A", "B", "C")))
    markup = await wait_for(message, 1)

    assert [b.text for b in buttons(markup)] == ["A", "B", "C"]

    await tap(message, 1, 0)
    await task


@pytest.mark.asyncio
async def test_the_question_and_descriptions_are_shown():
    message = Message()
    task = asyncio.create_task(ask_user.ask(message, payload("A", "B")))
    await wait_for(message, 1)

    assert "Which approach?" in message.sent[0]
    assert "about A" in message.sent[0]

    await tap(message, 1, 0)
    await task


@pytest.mark.asyncio
async def test_several_questions_are_asked_in_turn():
    message = Message()
    both = {
        "questions": [
            payload("A", "B", question="First?")["questions"][0],
            payload("C", "D", question="Second?")["questions"][0],
        ]
    }
    task = asyncio.create_task(ask_user.ask(message, both))

    await tap(message, 1, 0)
    await tap(message, 2, 1)

    assert await task == "First? → A\nSecond? → D"


@pytest.mark.asyncio
async def test_the_keyboard_is_replaced_by_the_answer():
    """So a stale question cannot be answered twice."""
    message = Message()
    sent_holder = []
    original = message.reply_text

    async def capture(*a, **k):
        sent = await original(*a, **k)
        sent_holder.append(sent)
        return sent

    message.reply_text = capture
    task = asyncio.create_task(ask_user.ask(message, payload("A", "B")))

    await tap(message, 1, 0)
    await task

    assert "Answered" in sent_holder[0].edited


# --- when it goes wrong -----------------------------------------------------


@pytest.mark.asyncio
async def test_an_unanswered_question_gives_up():
    """A blocked turn must not wait forever on a tap that never comes."""
    message = Message()

    assert await ask_user.ask(message, payload("A"), timeout=0.05) is None


@pytest.mark.asyncio
async def test_giving_up_forgets_the_question():
    message = Message()
    await ask_user.ask(message, payload("A"), timeout=0.05)

    assert ask_user.pending_count() == 0


@pytest.mark.asyncio
async def test_a_question_with_no_options_is_not_asked():
    assert await ask_user.ask(Message(), {"questions": [{"question": "?"}]}) is None


@pytest.mark.asyncio
async def test_an_empty_payload_is_not_asked():
    assert await ask_user.ask(Message(), {}) is None


@pytest.mark.asyncio
async def test_a_telegram_failure_is_not_fatal():
    assert await ask_user.ask(Message(fail=True), payload("A")) is None


def test_a_tap_on_a_closed_question_is_ignored():
    assert ask_user.resolve("ask:deadbeef:0") is None


def test_an_unrelated_callback_is_ignored():
    assert ask_user.resolve("stop:123") is None


@pytest.mark.asyncio
async def test_an_out_of_range_option_is_ignored():
    message = Message()
    task = asyncio.create_task(ask_user.ask(message, payload("A")))
    markup = await wait_for(message, 1)
    token = buttons(markup)[0].callback_data.split(":")[1]

    assert ask_user.resolve(f"ask:{token}:9") is None

    await tap(message, 1, 0)
    await task


@pytest.mark.asyncio
async def test_callback_data_stays_inside_telegrams_limit():
    """64 bytes, so options are addressed by index rather than by label."""
    message = Message()
    task = asyncio.create_task(
        ask_user.ask(message, payload("x" * 200, "y" * 200))
    )
    markup = await wait_for(message, 1)

    for button in buttons(markup):
        assert len(button.callback_data.encode()) <= 64

    await tap(message, 1, 0)
    await task
