"""The five-hour bar under the "Working..." message."""

from src.bot.orchestrator import MessageOrchestrator


def fmt(log, level=1, pct=None):
    orch = MessageOrchestrator.__new__(MessageOrchestrator)
    return orch._format_verbose_progress(log, level, 0.0, pct)


def test_no_bar_when_usage_unknown():
    assert fmt([]) == "Working..."


def test_bar_under_a_bare_working_message():
    assert fmt([], pct=31) == "Working...\n\n5h ▰▰▰▱▱▱▱▱▱▱ 31%"


def test_bar_under_the_activity_list():
    text = fmt([{"kind": "tool", "name": "Read", "detail": ""}], pct=50)
    assert text.splitlines()[-1] == "5h ▰▰▰▰▰▱▱▱▱▱ 50%"
    assert "Read" in text
