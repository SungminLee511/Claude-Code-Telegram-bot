"""Tests for /branch: arming a fork, naming it, and carrying settings over."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.bot.handlers import branch_command as bc
from src.bot.utils.choice_store import ChoiceStore
from src.bot.utils.session_rename import branch_name, set_title
from src.claude.session_index import SessionInfo


# --- the name ---------------------------------------------------------------


def test_the_branch_is_named_after_its_original():
    assert branch_name("telegram 마개조", "fast test") == (
        "telegram 마개조_(branch: fast test)"
    )


def test_an_unnamed_branch_is_called_temp():
    assert branch_name("thing", "") == "thing_(branch: temp)"
    assert branch_name("thing", "   ") == "thing_(branch: temp)"


def test_branching_a_branch_keeps_stacking():
    """The name should say how far from the original you have wandered."""
    once = branch_name("thing", "a")
    assert branch_name(once, "b") == "thing_(branch: a)_(branch: b)"


# --- writing the name -------------------------------------------------------


def test_the_name_is_appended_as_a_custom_title(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text('{"type":"user"}\n')

    assert set_title(str(path), "abc-123", "new name") is True

    last = json.loads(path.read_text().splitlines()[-1])
    assert last == {
        "type": "custom-title",
        "customTitle": "new name",
        "sessionId": "abc-123",
    }


def test_the_existing_transcript_is_not_disturbed(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text('{"type":"user"}\n')

    set_title(str(path), "abc", "n")

    assert path.read_text().splitlines()[0] == '{"type":"user"}'


def test_an_empty_name_is_not_written(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text("")

    assert set_title(str(path), "abc", "  ") is False
    assert path.read_text() == ""


def test_a_missing_transcript_is_not_an_error(tmp_path):
    assert set_title(str(tmp_path / "gone.jsonl"), "abc", "n") is False


# --- arming -----------------------------------------------------------------


class Message:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)
        return SimpleNamespace(message_id=1)


class FakeIndex:
    def __init__(self, infos=None):
        self._infos = infos or {}

    def get(self, session_id, refresh=True):
        return self._infos.get(session_id)


def make_context(tmp_path, session_id="old-session", infos=None):
    return SimpleNamespace(
        args=[],
        user_data={"claude_session_id": session_id},
        bot_data={
            "settings": SimpleNamespace(bot_id="main"),
            "_session_index": FakeIndex(infos),
            "_model_store": ChoiceStore(Path(tmp_path) / "m.json"),
            "_effort_store": ChoiceStore(Path(tmp_path) / "e.json"),
        },
    )


def update_for(message):
    return SimpleNamespace(message=message, effective_user=SimpleNamespace(id=7))


@pytest.mark.asyncio
async def test_branch_arms_the_next_message(tmp_path):
    context = make_context(tmp_path)

    await bc.branch_command(update_for(Message()), context)

    assert bc.is_armed(context)
    assert bc.pending_label(context) == "temp"


@pytest.mark.asyncio
async def test_a_label_is_remembered(tmp_path):
    context = make_context(tmp_path)
    context.args = ["fast", "test"]

    await bc.branch_command(update_for(Message()), context)

    assert bc.pending_label(context) == "fast test"


@pytest.mark.asyncio
async def test_the_reply_shows_the_name_it_will_use(tmp_path):
    infos = {"old-session": SessionInfo(session_id="old-session", path="/x", title="Orig")}
    context = make_context(tmp_path, infos=infos)
    msg = Message()

    await bc.branch_command(update_for(msg), context)

    assert "Orig_(branch: temp)" in msg.replies[0]


@pytest.mark.asyncio
async def test_branching_without_a_session_is_refused(tmp_path):
    context = make_context(tmp_path, session_id=None)
    msg = Message()

    await bc.branch_command(update_for(msg), context)

    assert not bc.is_armed(context)
    assert "no session" in msg.replies[0]


@pytest.mark.asyncio
async def test_a_branch_can_be_called_off(tmp_path):
    context = make_context(tmp_path)
    await bc.branch_command(update_for(Message()), context)

    context.args = ["cancel"]
    msg = Message()
    await bc.branch_command(update_for(msg), context)

    assert not bc.is_armed(context)
    assert "cancelled" in msg.replies[0]


def test_disarming_yields_the_label_once(tmp_path):
    """One command forks one message, not every message after it."""
    context = make_context(tmp_path)
    context.user_data[bc.PENDING_KEY] = "x"

    assert bc.disarm(context) == "x"
    assert bc.disarm(context) is None


# --- finishing --------------------------------------------------------------


def test_a_turn_that_did_not_fork_is_not_announced(tmp_path):
    """The CLI decides; a reused id is not a branch however it was asked for."""
    context = make_context(tmp_path)

    assert bc.finish(context, "temp", "same", "same") is None
    assert bc.finish(context, "temp", "old", None) is None


def test_the_fork_is_named_after_the_original(tmp_path):
    path = tmp_path / "new.jsonl"
    path.write_text("")
    infos = {
        "old": SessionInfo(session_id="old", path="/x", title="Orig"),
        "new": SessionInfo(session_id="new", path=str(path)),
    }
    context = make_context(tmp_path, infos=infos)

    note = bc.finish(context, "try it", "old", "new")

    assert "Orig_(branch: try it)" in note
    written = json.loads(path.read_text().splitlines()[-1])
    assert written["customTitle"] == "Orig_(branch: try it)"


def test_the_branch_starts_with_the_originals_model_and_effort(tmp_path):
    """A copy that silently reverted to defaults would be a nasty surprise."""
    path = tmp_path / "new.jsonl"
    path.write_text("")
    infos = {
        "old": SessionInfo(session_id="old", path="/x", title="Orig"),
        "new": SessionInfo(session_id="new", path=str(path)),
    }
    context = make_context(tmp_path, infos=infos)
    context.bot_data["_model_store"].set("old", "claude-opus-5-5")
    context.bot_data["_effort_store"].set("old", "xhigh")

    bc.finish(context, "temp", "old", "new")

    assert context.bot_data["_model_store"].get("new") == "claude-opus-5-5"
    assert context.bot_data["_effort_store"].get("new") == "xhigh"


def test_a_naming_failure_still_reports_the_branch(tmp_path):
    """The fork happened; only the label did not stick."""
    context = make_context(tmp_path)

    class Boom:
        def get(self, *a, **k):
            raise RuntimeError("index gone")

    context.bot_data["_session_index"] = Boom()

    assert bc.finish(context, "temp", "old", "new") is not None
