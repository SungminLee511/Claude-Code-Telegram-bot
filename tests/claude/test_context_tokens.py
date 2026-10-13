"""Tests for working out how much context a turn was holding."""

from types import SimpleNamespace

from claude_agent_sdk.types import AssistantMessage, ResultMessage

from src.claude.sdk_integration import _context_tokens


def assistant(**usage):
    return AssistantMessage(content=[], model="claude-opus-5", usage=usage)


def result(**usage):
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="x",
        usage=usage,
    )


def test_the_three_input_buckets_are_added_up():
    messages = [
        assistant(
            input_tokens=2,
            cache_creation_input_tokens=598,
            cache_read_input_tokens=457_985,
        )
    ]

    assert _context_tokens(messages) == 458_585


def test_output_tokens_are_not_counted():
    """They belong to the next turn's input, not this one's."""
    messages = [assistant(input_tokens=100, output_tokens=5_000)]

    assert _context_tokens(messages) == 100


def test_the_turn_total_is_not_mistaken_for_the_context():
    """ResultMessage.usage sums every call in the turn — three tool calls
    would otherwise read as three times the context."""
    messages = [
        assistant(input_tokens=2, cache_read_input_tokens=458_000),
        assistant(input_tokens=2, cache_read_input_tokens=458_000),
        result(input_tokens=4, cache_read_input_tokens=916_000),
    ]

    assert _context_tokens(messages) == 458_002


def test_the_latest_assistant_message_wins():
    """An earlier one in the same turn had seen less of the conversation."""
    messages = [
        assistant(input_tokens=10, cache_read_input_tokens=100),
        assistant(input_tokens=10, cache_read_input_tokens=900),
    ]

    assert _context_tokens(messages) == 910


def test_no_usage_anywhere_means_no_answer():
    assert _context_tokens([SimpleNamespace(content=[])]) is None


def test_an_empty_turn_is_survivable():
    assert _context_tokens([]) is None


def test_a_non_numeric_count_is_ignored():
    assert _context_tokens([assistant(input_tokens="lots")]) is None


def test_last_model_ignores_subagent_turns():
    from claude_agent_sdk.types import AssistantMessage

    from src.claude.sdk_integration import _last_model

    main = AssistantMessage(content=[], model="claude-opus-5-5")
    sub = AssistantMessage(
        content=[], model="claude-haiku-5-5", parent_tool_use_id="toolu_1"
    )

    assert _last_model([main, sub]) == "claude-opus-5-5"
