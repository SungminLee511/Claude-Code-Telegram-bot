"""Tests for the display-only session banner."""

from types import SimpleNamespace

import pytest

from src.bot.utils.formatting import FormattedMessage
from src.bot.utils.session_header import attach_header, build_header
from src.claude.session_index import SessionInfo


class FakeIndex:
    def __init__(self, info=None):
        self._info = info

    def get(self, session_id, refresh=True):
        return self._info


def make_context(
    session_id="abcdef12-3456-7890-aaaa-bbbbccccdddd",
    title="A tidy session",
    message_count=27,
    total_cost=1.42,
    bot_id="main",
    header_enabled=True,
    cwd="/home",
):
    settings = SimpleNamespace(
        bot_id=bot_id,
        approved_directory="/home",
        session_header=header_enabled,
    )
    info = (
        SessionInfo(session_id=session_id, path="/x.jsonl", title=title)
        if title is not None
        else None
    )
    session = (
        SimpleNamespace(message_count=message_count, total_cost=total_cost)
        if message_count is not None
        else None
    )
    manager = SimpleNamespace(
        active_sessions={session_id: session} if session else {}
    )
    return SimpleNamespace(
        user_data={"claude_session_id": session_id, "current_directory": cwd},
        bot_data={
            "settings": settings,
            "claude_integration": SimpleNamespace(session_manager=manager),
            "_session_index": FakeIndex(info),
        },
    )


def test_header_contains_name_id_count_cwd_and_bot():
    header = build_header(make_context())

    assert "A tidy session" in header
    assert "abcdef12" in header
    assert "27 msg" in header
    assert "/home" in header
    assert "bot main" in header


def test_cost_is_never_shown():
    """The SDK only reports this process's spend, so the number would mislead."""
    assert "$" not in build_header(make_context())


def test_no_session_means_no_header():
    context = make_context()
    context.user_data["claude_session_id"] = None
    assert build_header(context) is None


def test_session_id_from_response_when_chat_has_none():
    context = make_context()
    context.user_data["claude_session_id"] = None
    response = SimpleNamespace(session_id="feedface-0000-1111-2222-333344445555", cost=0.5)

    header = build_header(context, response)
    assert "feedface" in header


def test_untitled_session_still_renders():
    context = make_context(title=None)
    assert "(untitled session)" in build_header(context)


def test_long_title_is_truncated():
    context = make_context(title="x" * 200)
    header = build_header(context)
    assert "…" in header
    assert len(header) < 400


def test_long_cwd_is_shortened():
    context = make_context(cwd="/home/some/deeply/nested/project/path/that/is/long")
    header = build_header(context)
    assert "…/is/long" in header


def test_html_in_title_is_escaped():
    context = make_context(title="<b>not bold</b>")
    header = build_header(context)
    assert "&lt;b&gt;" in header
    assert "<b>not bold</b>" not in header


def test_banner_is_appended_to_the_last_message_only():
    context = make_context()
    messages = [
        FormattedMessage("first part", parse_mode="HTML"),
        FormattedMessage("second part", parse_mode="HTML"),
    ]

    out = attach_header(messages, context)

    assert out[0].text == "first part"
    assert out[1].text.startswith("second part")
    assert out[1].text.rstrip().endswith("bot main")
    assert "🧵" in out[1].text


def test_banner_becomes_its_own_message_when_body_is_near_the_limit():
    context = make_context()
    messages = [FormattedMessage("y" * 4090, parse_mode="HTML")]

    out = attach_header(messages, context)

    assert len(out) == 2
    assert out[0].text == "y" * 4090
    assert out[1].text.startswith("🧵")
    assert all(len(m.text) <= 4096 for m in out)


def test_disabled_by_setting():
    context = make_context(header_enabled=False)
    messages = [FormattedMessage("body", parse_mode="HTML")]
    assert attach_header(messages, context) == messages


def test_empty_message_list_is_untouched():
    assert attach_header([], make_context()) == []


def test_index_failure_does_not_break_the_reply():
    context = make_context()

    class Boom:
        def get(self, *a, **k):
            raise RuntimeError("index exploded")

    context.bot_data["_session_index"] = Boom()
    messages = [FormattedMessage("body", parse_mode="HTML")]

    out = attach_header(messages, context)
    # Title lookup failed, but the banner (and the reply) still render.
    assert out[0].text.startswith("body")
    assert "(untitled session)" in out[0].text


def test_missing_session_manager_still_renders():
    context = make_context()
    context.bot_data["claude_integration"] = None
    context.bot_data["_session_index"] = FakeIndex(
        SessionInfo(session_id="x", path="/x", title="T")
    )
    header = build_header(context, SimpleNamespace(session_id="x", cost=0.25))
    assert "T" in header
    assert "msg" not in header  # count unknown without the manager
    assert "$" not in header
