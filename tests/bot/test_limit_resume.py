"""After a usage limit stops a run, resume automatically once it resets."""

import asyncio
import time
from types import SimpleNamespace

import pytest
from claude_agent_sdk.types import RateLimitEvent, RateLimitInfo

from src.bot import orchestrator as orch_mod
from src.bot.orchestrator import MessageOrchestrator
from src.claude.sdk_integration import _limit_hit


# --- reading the limit out of the CLI's stream --------------------------------


def event(status, resets_at=None, kind="five_hour"):
    return RateLimitEvent(
        rate_limit_info=RateLimitInfo(
            status=status, resets_at=resets_at, rate_limit_type=kind
        ),
        uuid="u",
        session_id="s",
    )


def test_a_rejected_event_is_a_limit_hit_with_its_reset_time():
    assert _limit_hit([event("rejected", 1_900_000_000)]) == (True, 1_900_000_000.0)


def test_allowed_events_are_not_a_limit_hit():
    assert _limit_hit([event("allowed", 1), event("allowed_warning", 2)]) == (
        False,
        None,
    )


def test_a_rejected_overage_pool_is_not_a_limit_hit():
    assert _limit_hit([event("rejected", 5, kind="overage")]) == (False, None)


def test_the_last_rejected_event_wins():
    hit = _limit_hit([event("rejected", 100), event("rejected", 200)])

    assert hit == (True, 200.0)


def test_an_error_result_that_reads_like_a_limit_counts_without_a_reset_time():
    assert _limit_hit([], True, "Claude AI usage limit reached") == (True, None)


def test_a_normal_reply_mentioning_limits_is_not_a_limit_hit():
    assert _limit_hit([], False, "the usage limit docs say...") == (False, None)


# --- scheduling the resume ----------------------------------------------------


class FakePopen:
    calls = []

    def __init__(self, args, **kwargs):
        FakePopen.calls.append((args, kwargs))


class Msg:
    def __init__(self):
        self.sent = []

    async def reply_text(self, text, **kwargs):
        self.sent.append(text)


def make(monkeypatch):
    FakePopen.calls = []
    monkeypatch.setattr(orch_mod.subprocess, "Popen", FakePopen)
    orch = MessageOrchestrator.__new__(MessageOrchestrator)
    orch.settings = SimpleNamespace(bot_id="bot2", inject_dir="/tmp/claude_inject")
    msg = Msg()
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=42), effective_message=msg
    )
    context = SimpleNamespace(user_data={})
    return orch, update, context, msg


def response(limit_hit=True, resets_at=None):
    return SimpleNamespace(
        limit_hit=limit_hit, limit_resets_at=resets_at, content="", no_text=True,
        interrupted=False,
    )


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_the_wake_fires_at_the_reset_plus_a_one_minute_buffer(monkeypatch):
    orch, update, context, msg = make(monkeypatch)
    monkeypatch.setattr(orch_mod.time, "time", lambda: 1_000_000.0)

    run(orch._schedule_limit_resume(update, context, response(resets_at=1_000_600)))

    (args, kwargs), = FakePopen.calls
    assert args[2] == "660"  # 600s to the reset + 60s buffer
    assert args[3] == orch_mod.LIMIT_RESUME_TEXT
    assert args[4] == "bot2"
    assert kwargs["env"]["WAKE_CHAT_ID"] == "42"
    assert "Resuming automatically" in msg.sent[0]
    assert "1 min buffer" in msg.sent[0]


def test_a_reset_already_in_the_past_still_waits_the_buffer(monkeypatch):
    orch, update, context, msg = make(monkeypatch)
    monkeypatch.setattr(orch_mod.time, "time", lambda: 1_000_000.0)
    monkeypatch.setattr(orch, "_reset_from_usage", lambda: None, raising=False)

    run(orch._schedule_limit_resume(update, context, response(resets_at=999_000)))

    # Stale reset and no usage data: nothing trustworthy to schedule.
    assert FakePopen.calls == []


def test_without_a_reset_from_the_cli_the_usage_endpoint_is_asked(monkeypatch):
    orch, update, context, msg = make(monkeypatch)
    monkeypatch.setattr(orch_mod.time, "time", lambda: 1_000_000.0)
    monkeypatch.setattr(
        MessageOrchestrator, "_reset_from_usage", staticmethod(lambda: 1_003_000.0)
    )

    run(orch._schedule_limit_resume(update, context, response(resets_at=None)))

    (args, _), = FakePopen.calls
    assert args[2] == "3060"


def test_no_known_reset_time_means_no_wake(monkeypatch):
    orch, update, context, msg = make(monkeypatch)
    monkeypatch.setattr(
        MessageOrchestrator, "_reset_from_usage", staticmethod(lambda: None)
    )

    run(orch._schedule_limit_resume(update, context, response(resets_at=None)))

    assert FakePopen.calls == []
    assert msg.sent == []


def test_a_normal_reply_schedules_nothing_and_clears_the_count(monkeypatch):
    orch, update, context, msg = make(monkeypatch)
    context.user_data["limit_resume_attempts"] = 2

    run(orch._schedule_limit_resume(update, context, response(limit_hit=False)))

    assert FakePopen.calls == []
    assert context.user_data["limit_resume_attempts"] == 0


def test_it_gives_up_after_the_cap(monkeypatch):
    orch, update, context, msg = make(monkeypatch)
    monkeypatch.setattr(orch_mod.time, "time", lambda: 1_000_000.0)

    for _ in range(orch_mod.MAX_LIMIT_RESUMES + 1):
        run(orch._schedule_limit_resume(update, context, response(resets_at=1_000_600)))

    assert len(FakePopen.calls) == orch_mod.MAX_LIMIT_RESUMES
    assert context.user_data["limit_resume_attempts"] == 0  # next message is fresh


def test_an_absurdly_distant_reset_is_not_scheduled(monkeypatch):
    orch, update, context, msg = make(monkeypatch)
    monkeypatch.setattr(orch_mod.time, "time", lambda: 1_000_000.0)

    run(orch._schedule_limit_resume(
        update, context, response(resets_at=1_000_000 + 30 * 24 * 3600)
    ))

    assert FakePopen.calls == []


def test_an_exception_that_says_usage_limit_is_handled_too(monkeypatch):
    orch, update, context, msg = make(monkeypatch)
    monkeypatch.setattr(orch_mod.time, "time", lambda: 1_000_000.0)
    monkeypatch.setattr(
        MessageOrchestrator, "_reset_from_usage", staticmethod(lambda: 1_000_500.0)
    )

    run(orch._schedule_limit_resume(
        update, context, None, RuntimeError("Claude AI usage limit reached")
    ))

    assert len(FakePopen.calls) == 1


def test_an_unrelated_exception_is_ignored(monkeypatch):
    orch, update, context, msg = make(monkeypatch)

    run(orch._schedule_limit_resume(update, context, None, RuntimeError("boom")))

    assert FakePopen.calls == []


def test_a_limit_hit_does_not_also_trigger_the_empty_reply_nudge(monkeypatch):
    orch, update, context, msg = make(monkeypatch)

    orch._wake_if_empty(update, context, response(limit_hit=True))

    assert FakePopen.calls == []


def test_the_failing_scheduler_never_raises(monkeypatch):
    orch, update, context, msg = make(monkeypatch)
    monkeypatch.setattr(orch_mod.time, "time", lambda: 1_000_000.0)

    def boom(*a, **k):
        raise OSError("no bash")

    monkeypatch.setattr(orch_mod.subprocess, "Popen", boom)

    run(orch._schedule_limit_resume(update, context, response(resets_at=1_000_600)))
