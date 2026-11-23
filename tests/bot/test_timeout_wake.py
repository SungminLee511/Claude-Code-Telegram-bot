"""A turn killed by the SDK request timeout triggers a short continue-wake."""

from types import SimpleNamespace

from src.bot import orchestrator as orch_mod
from src.bot.orchestrator import MessageOrchestrator
from src.claude.exceptions import ClaudeTimeoutError


class FakePopen:
    calls = []

    def __init__(self, args, **kwargs):
        FakePopen.calls.append((args, kwargs))


def make(monkeypatch):
    FakePopen.calls = []
    monkeypatch.setattr(orch_mod.subprocess, "Popen", FakePopen)
    orch = MessageOrchestrator.__new__(MessageOrchestrator)
    orch.settings = SimpleNamespace(bot_id="bot3", inject_dir="/tmp/claude_inject")
    context = SimpleNamespace(user_data={})
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=42))
    return orch, update, context


def timeout():
    return ClaudeTimeoutError("Claude SDK timed out after 7200s")


def test_a_timeout_schedules_a_five_second_continue_wake(monkeypatch):
    orch, update, context = make(monkeypatch)

    assert orch._wake_if_timeout(update, context, timeout()) is True

    (args, kwargs), = FakePopen.calls
    assert args[0] == "bash" and args[1].endswith("wake_after.sh")
    assert args[2] == "5"
    assert args[3].startswith("CONTINUE:")
    assert args[4] == "bot3"
    assert kwargs["env"]["WAKE_CHAT_ID"] == "42"


def test_the_timeout_is_recognised_by_its_message_too(monkeypatch):
    orch, update, context = make(monkeypatch)

    assert orch._wake_if_timeout(
        update, context, RuntimeError("Claude SDK timed out after 900s")
    )
    assert len(FakePopen.calls) == 1


def test_other_errors_schedule_nothing(monkeypatch):
    orch, update, context = make(monkeypatch)

    assert orch._wake_if_timeout(update, context, RuntimeError("boom")) is False
    assert FakePopen.calls == []


def test_a_finished_run_resets_the_count(monkeypatch):
    orch, update, context = make(monkeypatch)
    context.user_data["timeout_retries"] = 2

    assert orch._wake_if_timeout(update, context, None) is False

    assert FakePopen.calls == []
    assert context.user_data["timeout_retries"] == 0


def test_it_gives_up_after_the_cap_so_it_cannot_loop_forever(monkeypatch):
    orch, update, context = make(monkeypatch)

    for _ in range(orch_mod.MAX_TIMEOUT_RETRIES + 1):
        orch._wake_if_timeout(update, context, timeout())

    assert len(FakePopen.calls) == orch_mod.MAX_TIMEOUT_RETRIES
    assert context.user_data["timeout_retries"] == 0
