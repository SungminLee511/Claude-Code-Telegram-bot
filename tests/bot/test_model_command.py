"""Tests for /model: the choice list, the store, and the per-session binding."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.bot.handlers import model_command as mc
from src.bot.utils import models
from src.bot.utils.choice_store import ChoiceStore


# --- the fixed list ---------------------------------------------------------


def test_an_alias_resolves_to_its_model_id():
    assert models.resolve("sonnet").model_id == "sonnet"
    assert models.resolve("opus5").model_id == "claude-opus-5"


def test_a_full_model_id_also_resolves():
    assert models.resolve("claude-opus-5").alias == "opus5"


def test_resolution_ignores_case_and_padding():
    assert models.resolve("  OPUS  ").alias == "opus"


def test_an_unknown_name_resolves_to_nothing():
    assert models.resolve("gpt-9") is None


def test_a_model_the_cli_is_too_old_for_says_so():
    reason = models.blocked_reason("opus5.5", "2.1.139")
    assert "2.1.280" in reason
    assert "2.1.139" in reason


def test_a_model_the_cli_is_too_old_for_is_not_selectable():
    assert models.resolve("opus5.5", "2.1.139") is None


def test_a_new_enough_cli_can_select_it():
    assert models.resolve("opus5.5", "2.1.296").model_id == "claude-opus-5-5"


def test_the_exact_required_version_counts_as_new_enough():
    assert models.resolve("fable", "2.1.251") is not None


def test_a_version_number_compares_numerically_not_as_text():
    """2.1.99 is older than 2.1.251 even though '9' > '2'."""
    assert models.resolve("fable", "2.1.99") is None


def test_an_unreadable_version_does_not_hide_everything():
    """Better to let the API answer than to refuse on a failed lookup."""
    assert models.resolve("opus5.5", None) is not None


def test_a_prerelease_suffix_is_ignored():
    assert models.resolve("fable", "2.1.296-beta.1") is not None


def test_an_old_cli_still_offers_the_unversioned_models():
    aliases = [c.alias for c in models.available("2.1.139")]
    assert "opus" in aliases and "opus5" in aliases
    assert "opus5.5" not in aliases


def test_a_new_cli_offers_everything():
    assert len(models.available("2.1.296")) == len(models.CHOICES)


def test_the_blocked_list_is_empty_on_a_new_cli():
    assert models.unavailable("2.1.296") == {}


def test_an_unknown_name_has_no_block_reason():
    """It is not refused for being old; it simply does not exist."""
    assert models.blocked_reason("gpt-9", "2.1.139") is None


def test_the_bundled_cli_version_is_reported_when_no_path_is_set():
    assert models.cli_version(None).startswith("2.")


def test_the_banner_label_for_a_known_model():
    assert models.label_for("claude-opus-5") == "opus-5"


def test_an_unlisted_model_id_is_still_labelled():
    """The point is to report what is running, not to validate it."""
    assert models.label_for("claude-something-7") == "something-7"


def test_no_model_means_no_label():
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
    await mc.model_command(make_update(msg), make_context(tmp_path))

    marked = [ln for ln in msg.replies[0].splitlines() if ln.startswith("▸")]
    assert len(marked) == 1
    assert "opus5" in marked[0]


@pytest.mark.asyncio
async def test_the_listing_explains_what_cannot_be_chosen(tmp_path):
    msg = Message()
    await mc.model_command(make_update(msg), make_context(tmp_path))

    assert "2.1.280" in msg.replies[0]


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


@pytest.mark.asyncio
async def test_an_unknown_name_is_refused_with_the_list(tmp_path):
    msg = Message()
    context = make_context(tmp_path, session_id="abc", args=["gpt-9"])

    await mc.model_command(make_update(msg), context)

    assert "No model called" in msg.replies[0]
    assert "sonnet" in msg.replies[0]
    assert context.bot_data["_model_store"].get("abc") is None


@pytest.mark.asyncio
async def test_a_blocked_name_is_refused_with_the_reason(tmp_path):
    msg = Message()
    context = make_context(tmp_path, session_id="abc", args=["fable"])

    await mc.model_command(make_update(msg), context)

    assert "2.1.251" in msg.replies[0]
    assert context.bot_data["_model_store"].get("abc") is None


@pytest.mark.asyncio
async def test_switching_clears_a_stale_waiting_choice(tmp_path):
    context = make_context(tmp_path, session_id=None, args=["haiku"])
    await mc.model_command(make_update(Message()), context)

    context.user_data["claude_session_id"] = "now-exists"
    context.args = ["sonnet"]
    await mc.model_command(make_update(Message()), context)

    assert not context.user_data[mc.PENDING_KEY]
    assert mc.current_model_id(context) == "sonnet"


@pytest.mark.asyncio
async def test_a_bot_on_the_new_cli_can_pick_the_new_models(tmp_path, monkeypatch):
    monkeypatch.setattr(mc.models, "cli_version", lambda path=None: "2.1.296")
    context = make_context(tmp_path, session_id="abc", args=["opus5.5"])
    msg = Message()

    await mc.model_command(make_update(msg), context)

    assert context.bot_data["_model_store"].get("abc") == "claude-opus-5-5"
    assert "opus-5.5" in msg.replies[0]


@pytest.mark.asyncio
async def test_the_listing_on_a_new_cli_blocks_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(mc.models, "cli_version", lambda path=None: "2.1.296")
    msg = Message()

    await mc.model_command(make_update(msg), make_context(tmp_path))

    assert "Not selectable" not in msg.replies[0]
    assert "opus5.5" in msg.replies[0]


@pytest.mark.asyncio
async def test_a_failing_version_probe_does_not_break_the_command(
    tmp_path, monkeypatch
):
    def boom(path=None):
        raise RuntimeError("binary is gone")

    monkeypatch.setattr(mc.models, "cli_version", boom)
    msg = Message()

    await mc.model_command(make_update(msg), make_context(tmp_path))

    assert "sonnet" in msg.replies[0]


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
