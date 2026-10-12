"""Tests for the SDK can_use_tool gate."""

from pathlib import Path

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from src.claude.sdk_integration import _make_can_use_tool_callback


def make_callback(validator=None):
    return _make_can_use_tool_callback(
        security_validator=validator,
        working_directory=Path("/home"),
        approved_directory=Path("/home"),
    )


async def test_ask_user_question_is_denied_with_a_redirect():
    """It has no Telegram UI, so the answer would come back empty."""
    decision = await make_callback()("AskUserQuestion", {"questions": []}, None)

    assert isinstance(decision, PermissionResultDeny)
    assert "plain text" in decision.message


async def test_denial_does_not_interrupt_the_turn():
    """The model should read the reason and keep going, not abort."""
    decision = await make_callback()("AskUserQuestion", {}, None)
    assert not getattr(decision, "interrupt", False)


async def test_ordinary_tools_still_pass_without_a_validator():
    callback = make_callback()

    for tool in ("Read", "Bash", "Grep"):
        decision = await callback(tool, {"file_path": "/home/x", "command": "ls"}, None)
        assert isinstance(decision, PermissionResultAllow), tool


async def test_interactive_block_runs_before_path_validation():
    """A validator that rejects everything must not change the deny message."""

    class RejectAll:
        def validate_path(self, *a, **k):
            return False, None, "nope"

    decision = await make_callback(RejectAll())(
        "AskUserQuestion", {"file_path": "/etc/passwd"}, None
    )
    assert "plain text" in decision.message


async def test_validator_still_guards_file_paths():
    class RejectAll:
        def validate_path(self, *a, **k):
            return False, None, "outside the approved directory"

    decision = await make_callback(RejectAll())(
        "Read", {"file_path": "/etc/passwd"}, None
    )
    assert isinstance(decision, PermissionResultDeny)
    assert decision.message == "outside the approved directory"
