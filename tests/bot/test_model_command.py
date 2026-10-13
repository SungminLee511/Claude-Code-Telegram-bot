"""Tests for /model: the choice list, the store, and the per-session binding."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.bot.handlers import model_command as mc
from src.bot.utils import models
from src.bot.utils.choice_store import ChoiceStore


# --- the list ---------------------------------------------------------------


def test_the_buttons_are_the_four_families():
    assert [c.alias for c in models.CHOICES] == ["fable", "opus", "sonnet", "haiku"]


def test_an_alias_resolves_case_and_space_insensitively():
    assert models.resolve("  OPUS  ").alias == "opus"


def test_a_pinned_id_is_not_a_button():
    assert models.resolve("claude-opus-5") is None


def test_wellformed_names():
    assert models.is_wellformed("claude-opus-5-5")
    assert models.is_wellformed("opus[1m]")
    assert not models.is_wellformed("rm -rf /")
    assert not models.is_wellformed("")
    assert not models.is_wellformed("-flag")


def test_label_drops_prefix_and_snapshot_date():
    assert models.label_for("claude-opus-5") == "opus-5"
    assert models.label_for("claude-sonnet-4-5-20250929") == "sonnet-4-5"
    assert models.label_for(None) is None


# --- the store --------------------------------------------------------------


@pytest.fixture()
def store(tmp_path):
    return ChoiceStore(tmp_path / "models.json")


def test_an_unset_session_has_no_model(store):
    assert store.get("abc") is None


def test_a_choice_survives_a_new_store_on_the_same_file(store):
    """The whole reason it is on disk: restarts."""
    store.set("abc", "sonnet")

    assert ChoiceStore(store.path).get("abc") == "sonnet"


def test_sessions_do_not_share_a_choice(store):
    store.set("abc", "sonnet")
    store.set("def", "haiku")

    assert store.get("abc") == "sonnet"
    assert store.get("def") == "haiku"


def test_setting_again_replaces(store):
    store.set("abc", "sonnet")
    store.set("abc", "haiku")

    assert store.get("abc") == "haiku"


def test_clearing_returns_a_session_to_the_default(store):
    store.set("abc", "sonnet")
    store.clear("abc")

    assert store.get("abc") is None


def test_an_empty_session_id_is_not_stored(store):
    store.set("", "sonnet")

    assert store.get("") is None


def test_the_file_is_trimmed_but_keeps_the_recent_ones(store, monkeypatch):
    monkeypatch.setattr("src.bot.utils.choice_store._MAX_ENTRIES", 3)
    for i in range(5):
        store.set(f"s{i}", "haiku")

    assert store.get("s0") is None
    assert store.get("s4") == "haiku"


def test_a_corrupt_file_reads_as_empty_rather_than_raising(store):
    store.path.write_text("{not json")

    assert store.get("abc") is None


def test_touching_a_session_keeps_it_from_being_trimmed(store, monkeypatch):
    monkeypatch.setattr("src.bot.utils.choice_store._MAX_ENTRIES", 2)
    store.set("old", "haiku")
    store.set("other", "haiku")
    store.set("old", "sonnet")  # used again -> now the newest
    store.set("new", "haiku")

    assert store.get("old") == "sonnet"
    assert store.get("other") is None


# --- the handler ------------------------------------------------------------


class Message:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)
        return SimpleNamespace(message_id=1)


def make_context(tmp_path, session_id=None, args=None, claude_model="claude-opus-5"):
    context = SimpleNamespace(
        args=args or [],
        user_data={"claude_session_id": session_id},
        bot_data={
            "settings": SimpleNamespace(bot_id="main", claude_model=claude_model),
            "_model_store": ChoiceStore(Path(tmp_path) / "models.json"),
        },
    )
    return context


def make_update(message):
    return SimpleNamespace(message=message, effective_user=SimpleNamespace(id=7))


@pytest.mark.asyncio
async def test_bare_model_lists_the_choices(tmp_path):
    msg = Message()
    await mc.model_command(make_update(msg), make_context(tmp_path))

    (text,) = msg.replies
    for choice in models.CHOICES:
        assert choice.alias in text


@pytest.mark.asyncio
async def test_the_listing_marks_the_model_in_force(tmp_path):
    msg = Message()
    context = make_context(tmp_path, claude_model="sonnet")
    await mc.model_command(make_update(msg), context)

    marked = [ln for ln in msg.replies[0].splitlines() if ln.startswith("▸")]
    assert len(marked) == 1
    assert "sonnet" in marked[0]


@pytest.mark.asyncio
async def test_choosing_pins_the_current_session(tmp_path):
    context = make_context(tmp_path, session_id="abcdef12-3456", args=["sonnet"])
    msg = Message()

    await mc.model_command(make_update(msg), context)

    assert context.bot_data["_model_store"].get("abcdef12-3456") == "sonnet"
    assert "sonnet" in msg.replies[0]


@pytest.mark.asyncio
async def test_another_session_is_unaffected(tmp_path):
    context = make_context(tmp_path, session_id="one", args=["haiku"])
    await mc.model_command(make_update(Message()), context)

    context.user_data["claude_session_id"] = "two"
    assert mc.current_model_id(context) is None


@pytest.mark.asyncio
async def test_choosing_before_a_session_exists_waits_for_one(tmp_path):
    context = make_context(tmp_path, session_id=None, args=["haiku"])

    await mc.model_command(make_update(Message()), context)

    assert context.user_data[mc.PENDING_KEY] == "haiku"
    assert mc.current_model_id(context) == "haiku"


@pytest.mark.asyncio
async def test_the_waiting_choice_is_bound_once_the_session_has_an_id(tmp_path):
    context = make_context(tmp_path, session_id=None, args=["haiku"])
    await mc.model_command(make_update(Message()), context)

    mc.bind_pending(context, "fresh-session")

    assert context.bot_data["_model_store"].get("fresh-session") == "haiku"
    assert not context.user_data[mc.PENDING_KEY]


@pytest.mark.asyncio
async def test_binding_without_a_waiting_choice_does_nothing(tmp_path):
    context = make_context(tmp_path, session_id="abc")

    mc.bind_pending(context, "abc")

    assert context.bot_data["_model_store"].get("abc") is None


def probe_returns(monkeypatch, result):
    async def fake(name, cli_path=None):
        return result

    monkeypatch.setattr(mc.models, "probe", fake)


@pytest.mark.asyncio
async def test_a_malformed_name_is_refused_without_a_probe(tmp_path, monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("must not probe")

    monkeypatch.setattr(mc.models, "probe", boom)
    msg = Message()
    context = make_context(tmp_path, session_id="abc", args=["rm;-rf"])

    await mc.model_command(make_update(msg), context)

    assert "not a model name" in msg.replies[0]
    assert context.bot_data["_model_store"].get("abc") is None


@pytest.mark.asyncio
async def test_a_typed_name_the_cli_rejects_changes_nothing(tmp_path, monkeypatch):
    probe_returns(monkeypatch, (False, "the CLI does not recognise it"))
    msg = Message()
    context = make_context(tmp_path, session_id="abc", args=["gpt-9"])

    await mc.model_command(make_update(msg), context)

    assert "unchanged" in msg.replies[-1].lower() or any(
        "unchanged" in r.lower() for r in msg.replies
    )
    assert context.bot_data["_model_store"].get("abc") is None


@pytest.mark.asyncio
async def test_a_typed_name_the_cli_accepts_is_pinned(tmp_path, monkeypatch):
    probe_returns(monkeypatch, (True, ""))
    msg = Message()
    context = make_context(tmp_path, session_id="abc", args=["claude-opus-5"])

    await mc.model_command(make_update(msg), context)

    assert context.bot_data["_model_store"].get("abc") == "claude-opus-5"
    assert "opus-5" in msg.replies[-1]


@pytest.mark.asyncio
async def test_an_unverifiable_name_is_pinned_with_a_warning(tmp_path, monkeypatch):
    probe_returns(monkeypatch, (None, "check timed out"))
    msg = Message()
    context = make_context(tmp_path, session_id="abc", args=["claude-opus-5"])

    await mc.model_command(make_update(msg), context)

    assert context.bot_data["_model_store"].get("abc") == "claude-opus-5"
    assert "verify" in msg.replies[-1]


@pytest.mark.asyncio
async def test_an_alias_needs_no_probe(tmp_path, monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("must not probe")

    monkeypatch.setattr(mc.models, "probe", boom)
    context = make_context(tmp_path, session_id="abc", args=["fable"])

    await mc.model_command(make_update(Message()), context)

    assert context.bot_data["_model_store"].get("abc") == "fable"


@pytest.mark.asyncio
async def test_switching_clears_a_stale_waiting_choice(tmp_path):
    context = make_context(tmp_path, session_id=None, args=["haiku"])
    await mc.model_command(make_update(Message()), context)

    context.user_data["claude_session_id"] = "now-exists"
    context.args = ["sonnet"]
    await mc.model_command(make_update(Message()), context)

    assert not context.user_data[mc.PENDING_KEY]
    assert mc.current_model_id(context) == "sonnet"


def test_the_store_is_created_once_per_bot(tmp_path):
    context = make_context(tmp_path)
    del context.bot_data["_model_store"]

    first = mc.get_store(context)

    assert mc.get_store(context) is first
    assert first.path.name == "session_models_main.json"


def test_a_broken_store_does_not_block_the_request(tmp_path):
    context = make_context(tmp_path, session_id="abc")

    class Boom:
        def get(self, *a, **k):
            raise RuntimeError("disk on fire")

    context.bot_data["_model_store"] = Boom()

    assert mc.current_model_id(context) is None
