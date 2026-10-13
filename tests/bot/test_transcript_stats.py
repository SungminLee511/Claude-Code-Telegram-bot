"""Tests for reading a dormant session's context size off its transcript."""

import json

import pytest

from src.bot.utils import transcript_stats


@pytest.fixture(autouse=True)
def clean_cache():
    transcript_stats.reset_cache()
    yield
    transcript_stats.reset_cache()


def assistant(**usage):
    return json.dumps({"type": "assistant", "message": {"role": "assistant", "usage": usage}})


def plain():
    return json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}})


def transcript(tmp_path, *lines, name="s.jsonl"):
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n")
    return str(path)


def test_the_input_buckets_are_added_up(tmp_path):
    path = transcript(
        tmp_path,
        assistant(
            input_tokens=2,
            cache_creation_input_tokens=598,
            cache_read_input_tokens=457_985,
        ),
    )

    assert transcript_stats.context_tokens(path) == 458_585


def test_the_last_assistant_turn_is_the_current_size(tmp_path):
    path = transcript(
        tmp_path,
        assistant(input_tokens=100),
        plain(),
        assistant(input_tokens=900),
    )

    assert transcript_stats.context_tokens(path) == 900


def test_a_turn_total_line_is_not_mistaken_for_an_assistant_turn(tmp_path):
    """A result line sums the whole turn — several times the real context."""
    path = transcript(
        tmp_path,
        assistant(input_tokens=500),
        json.dumps({"type": "result", "usage": {"input_tokens": 5_000}}),
    )

    assert transcript_stats.context_tokens(path) == 500


def test_a_transcript_with_no_usage_gives_nothing(tmp_path):
    assert transcript_stats.context_tokens(transcript(tmp_path, plain())) is None


def test_a_missing_file_is_not_an_error(tmp_path):
    assert transcript_stats.context_tokens(str(tmp_path / "gone.jsonl")) is None


def test_no_path_means_no_answer():
    assert transcript_stats.context_tokens(None) is None


def test_broken_lines_are_skipped(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text('{"usage": broken\n' + assistant(input_tokens=7) + "\n")

    assert transcript_stats.context_tokens(str(path)) == 7


def test_only_the_tail_is_read(tmp_path, monkeypatch):
    """The answer is always near the end; the front may be 30 MB."""
    monkeypatch.setattr(transcript_stats, "_TAIL_BYTES", 200)
    path = transcript(
        tmp_path,
        assistant(input_tokens=111),
        plain() + " " + "x" * 400,
        assistant(input_tokens=222),
    )

    assert transcript_stats.context_tokens(path) == 222


def test_an_unchanged_file_is_answered_without_reading_it(tmp_path):
    path = transcript(tmp_path, assistant(input_tokens=7))
    assert transcript_stats.context_tokens(path) == 7

    # Same length, different content: a re-read would see 9, the cache sees 7.
    (tmp_path / "s.jsonl").write_text(
        assistant(input_tokens=9).ljust(len(assistant(input_tokens=7))) + "\n"
    )

    assert transcript_stats.context_tokens(path) == 7


def test_a_grown_file_is_looked_at_again(tmp_path):
    path = transcript(tmp_path, assistant(input_tokens=7))
    assert transcript_stats.context_tokens(path) == 7

    with open(path, "a") as handle:
        handle.write(assistant(input_tokens=1234) + "\n")

    assert transcript_stats.context_tokens(path) == 1234
