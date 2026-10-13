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


async def tap_other(message, count=1):
    """Press Other, then confirm it — the two taps the escape hatch needs."""
    markup = await wait_for(message, count)
    first = ask_user.tap(buttons(markup)[-1].callback_data)
    assert first.kind == "confirm"
    return ask_user.tap(buttons(first.keyboard)[-1].callback_data)


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

    assert [b.text for b in buttons(markup)] == [
        "A",
        "B",
        "C",
        ask_user.OTHER_LABEL,
    ]

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


# --- the escape hatch -------------------------------------------------------


@pytest.mark.asyncio
async def test_every_question_offers_other():
    """Four wrong options must never be the only way out."""
    message = Message()
    task = asyncio.create_task(ask_user.ask(message, payload("A")))
    markup = await wait_for(message, 1)

    assert buttons(markup)[-1].text == ask_user.OTHER_LABEL

    await tap(message, 1, 0)
    await task


@pytest.mark.asyncio
async def test_other_hands_the_question_back_as_prose():
    """None of these fit -> release the turn so it can be asked in words."""
    message = Message()
    task = asyncio.create_task(ask_user.ask(message, payload("A", "B")))

    confirmed = await tap_other(message)

    assert confirmed.label == ask_user.OTHER_LABEL
    assert await task == f"Which approach? → {ask_user.OTHER_ANSWER}"


@pytest.mark.asyncio
async def test_one_tap_on_other_only_asks_whether_you_meant_it():
    """Other sits above the text field; a stray tap must not cost the question."""
    message = Message()
    task = asyncio.create_task(ask_user.ask(message, payload("A", "B")))
    markup = await wait_for(message, 1)

    result = ask_user.tap(buttons(markup)[-1].callback_data)

    assert result.kind == "confirm"
    assert not task.done()

    await tap(message, 1, 0)
    assert await task == "Which approach? → A"


@pytest.mark.asyncio
async def test_backing_out_of_other_restores_the_options():
    message = Message()
    task = asyncio.create_task(ask_user.ask(message, payload("A", "B")))
    markup = await wait_for(message, 1)

    confirm = ask_user.tap(buttons(markup)[-1].callback_data)
    back = ask_user.tap(buttons(confirm.keyboard)[0].callback_data)

    assert [b.text for b in buttons(back.keyboard)] == [
        "A",
        "B",
        ask_user.OTHER_LABEL,
    ]

    assert ask_user.tap(buttons(back.keyboard)[1].callback_data).label == "B"
    await task


@pytest.mark.asyncio
async def test_other_is_not_mistaken_for_an_option():
    message = Message()
    task = asyncio.create_task(ask_user.ask(message, payload("A", "B")))
    markup = await wait_for(message, 1)

    others = [b for b in buttons(markup) if b.callback_data.endswith(":x")]
    assert len(others) == 1

    await tap(message, 1, 0)
    assert await task == "Which approach? → A"


@pytest.mark.asyncio
async def test_choosing_other_forgets_the_question():
    message = Message()
    task = asyncio.create_task(ask_user.ask(message, payload("A")))
    await tap_other(message)
    await task

    assert ask_user.pending_count() == 0


# --- answering by typing ----------------------------------------------------


@pytest.mark.asyncio
async def test_a_typed_message_answers_the_question():
    """The text box is the obvious place to answer; make it work."""
    message = Message()
    task = asyncio.create_task(ask_user.ask(message, payload("A", "B")))
    await wait_for(message, 1)

    assert ask_user.answer_with_text("neither, do C") is True

    assert await task == "Which approach? → neither, do C"


@pytest.mark.asyncio
async def test_typing_clears_the_question():
    message = Message()
    task = asyncio.create_task(ask_user.ask(message, payload("A")))
    await wait_for(message, 1)
    ask_user.answer_with_text("something else")
    await task

    assert ask_user.pending_count() == 0


def test_typing_with_nothing_pending_is_left_alone():
    """Otherwise an ordinary message would be swallowed as an answer."""
    assert ask_user.answer_with_text("just talking") is False


@pytest.mark.asyncio
async def test_an_empty_message_is_not_an_answer():
    message = Message()
    task = asyncio.create_task(ask_user.ask(message, payload("A")))
    await wait_for(message, 1)

    assert ask_user.answer_with_text("   ") is False
    assert not task.done()

    await tap(message, 1, 0)
    await task


@pytest.mark.asyncio
async def test_a_typed_answer_is_used_verbatim():
    message = Message()
    task = asyncio.create_task(ask_user.ask(message, payload("A", "B")))
    await wait_for(message, 1)
    ask_user.answer_with_text("  option B but only on Tuesdays  ")

    assert await task == "Which approach? → option B but only on Tuesdays"
