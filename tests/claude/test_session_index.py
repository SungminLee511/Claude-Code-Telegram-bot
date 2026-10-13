"""Tests for the on-disk session index."""

import json
import time
from pathlib import Path

import pytest

from src.claude.session_index import SessionIndex, tail_messages


def _write_transcript(project_dir: Path, session_id: str, records):
    path = project_dir / f"{session_id}.jsonl"
    with open(path, "a") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    return path


def _user(text, cwd="/home"):
    return {
        "type": "user",
        "cwd": cwd,
        "timestamp": "2026-10-12T00:00:00Z",
        "message": {"role": "user", "content": text},
    }


def _assistant(text, cwd="/home"):
    return {
        "type": "assistant",
        "cwd": cwd,
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _title(name, session_id):
    return {"type": "ai-title", "aiTitle": name, "sessionId": session_id}


@pytest.fixture()
def projects(tmp_path):
    root = tmp_path / "projects"
    (root / "-home").mkdir(parents=True)
    return root


def test_scan_extracts_title_turns_and_cwd(projects, tmp_path):
    sid = "aaaaaaaa-1111-2222-3333-444444444444"
    _write_transcript(
        projects / "-home",
        sid,
        [
            _user("first question"),
            _title("A fine session", sid),
            _assistant("an answer"),
            _user("second question"),
        ],
    )

    index = SessionIndex(tmp_path / "cache.json", projects_dir=projects)
    sessions = index.refresh()

    assert len(sessions) == 1
    info = sessions[0]
    assert info.session_id == sid
    assert info.title == "A fine session"
    assert info.display_name == "A fine session"
    assert info.user_turns == 2
    assert info.first_prompt == "first question"
    assert info.last_prompt == "second question"
    assert info.cwd == "/home"


def test_tool_results_and_sidechains_are_not_user_turns(projects, tmp_path):
    sid = "bbbbbbbb-1111-2222-3333-444444444444"
    _write_transcript(
        projects / "-home",
        sid,
        [
            _user("real question"),
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "content": "stdout"}],
                },
            },
            {
                "type": "user",
                "isSidechain": True,
                "message": {"role": "user", "content": "subagent prompt"},
            },
        ],
    )

    info = SessionIndex(tmp_path / "cache.json", projects_dir=projects).refresh()[0]
    assert info.user_turns == 1


def test_rescan_is_incremental_and_picks_up_appends(projects, tmp_path):
    sid = "cccccccc-1111-2222-3333-444444444444"
    path = _write_transcript(projects / "-home", sid, [_user("one")])

    index = SessionIndex(tmp_path / "cache.json", projects_dir=projects)
    first = index.refresh()[0]
    assert first.user_turns == 1
    offset_after_first = first.offset
    assert offset_after_first == path.stat().st_size

    time.sleep(0.01)
    _write_transcript(projects / "-home", sid, [_user("two"), _title("Named", sid)])

    second = index.refresh()[0]
    assert second.user_turns == 2  # counted once, not recounted from zero
    assert second.title == "Named"
    assert second.offset == path.stat().st_size


def test_partial_trailing_line_is_not_consumed(projects, tmp_path):
    sid = "dddddddd-1111-2222-3333-444444444444"
    path = projects / "-home" / f"{sid}.jsonl"
    path.write_text(json.dumps(_user("complete")) + "\n" + '{"type":"user","mess')

    index = SessionIndex(tmp_path / "cache.json", projects_dir=projects)
    info = index.refresh()[0]
    assert info.user_turns == 1
    assert info.offset < path.stat().st_size

    # Completing the line later must make it count.
    with open(path, "a") as handle:
        handle.write('age": {"role": "user", "content": "late"}}\n')
    info = index.refresh()[0]
    assert info.user_turns == 2
    assert info.last_prompt == "late"


def test_truncated_file_is_rescanned_from_scratch(projects, tmp_path):
    sid = "eeeeeeee-1111-2222-3333-444444444444"
    path = _write_transcript(projects / "-home", sid, [_user("one"), _user("two")])

    index = SessionIndex(tmp_path / "cache.json", projects_dir=projects)
    assert index.refresh()[0].user_turns == 2

    path.write_text(json.dumps(_user("only one left")) + "\n")
    info = index.refresh()[0]
    assert info.user_turns == 1
    assert info.last_prompt == "only one left"


def test_cache_survives_a_new_index_instance(projects, tmp_path):
    sid = "ffffffff-1111-2222-3333-444444444444"
    _write_transcript(projects / "-home", sid, [_user("hello"), _title("Cached", sid)])
    cache = tmp_path / "cache.json"

    SessionIndex(cache, projects_dir=projects).refresh()
    reopened = SessionIndex(cache, projects_dir=projects).refresh()[0]

    assert reopened.title == "Cached"
    assert reopened.user_turns == 1


def test_deleted_transcripts_leave_the_index(projects, tmp_path):
    sid = "99999999-1111-2222-3333-444444444444"
    path = _write_transcript(projects / "-home", sid, [_user("hi")])
    index = SessionIndex(tmp_path / "cache.json", projects_dir=projects)
    assert len(index.refresh()) == 1

    path.unlink()
    assert index.refresh() == []


def test_search_matches_title_prompt_and_id(projects, tmp_path):
    sid_a = "11111111-aaaa-2222-3333-444444444444"
    sid_b = "22222222-bbbb-2222-3333-444444444444"
    _write_transcript(
        projects / "-home", sid_a, [_user("about pandas"), _title("Zoo trip", sid_a)]
    )
    _write_transcript(
        projects / "-home", sid_b, [_user("about rockets"), _title("Launch", sid_b)]
    )

    index = SessionIndex(tmp_path / "cache.json", projects_dir=projects)
    assert [s.session_id for s in index.search("zoo")] == [sid_a]
    assert [s.session_id for s in index.search("rockets")] == [sid_b]
    assert [s.session_id for s in index.search("22222222")] == [sid_b]
    assert len(index.search("")) == 2


def test_sessions_are_returned_newest_first(projects, tmp_path):
    older = "33333333-1111-2222-3333-444444444444"
    newer = "44444444-1111-2222-3333-444444444444"
    path_old = _write_transcript(projects / "-home", older, [_user("old")])
    path_new = _write_transcript(projects / "-home", newer, [_user("new")])

    import os

    now = time.time()
    os.utime(path_old, (now - 10_000, now - 10_000))
    os.utime(path_new, (now, now))

    sessions = SessionIndex(tmp_path / "cache.json", projects_dir=projects).refresh()
    assert [s.session_id for s in sessions] == [newer, older]


def test_untitled_session_falls_back_to_first_prompt(projects, tmp_path):
    sid = "55555555-1111-2222-3333-444444444444"
    _write_transcript(projects / "-home", sid, [_user("what is the airspeed velocity")])
    info = SessionIndex(tmp_path / "cache.json", projects_dir=projects).refresh()[0]
    assert info.title is None
    assert info.display_name.startswith("what is the airspeed")


def test_tail_messages_returns_last_turns(projects, tmp_path):
    sid = "66666666-1111-2222-3333-444444444444"
    path = _write_transcript(
        projects / "-home",
        sid,
        [_user(f"q{i}") for i in range(10)] + [_assistant("final answer")],
    )

    turns = tail_messages(path, limit=3)
    assert len(turns) == 3
    assert turns[-1]["role"] == "assistant"
    assert turns[-1]["text"] == "final answer"


def test_get_refreshes_single_session(projects, tmp_path):
    sid = "77777777-1111-2222-3333-444444444444"
    _write_transcript(projects / "-home", sid, [_user("one")])
    index = SessionIndex(tmp_path / "cache.json", projects_dir=projects)

    assert index.get(sid).user_turns == 1

    time.sleep(0.01)
    _write_transcript(projects / "-home", sid, [_user("two")])
    assert index.get(sid).user_turns == 2
    assert index.get("not-a-session") is None


# --- names set by hand with /rename -----------------------------------------


def _write(tmp_path, *lines):
    import json as _json

    d = tmp_path / "projects" / "-home"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "aaaaaaaa-1111-2222-3333-444444444444.jsonl"
    p.write_text("\n".join(_json.dumps(x) for x in lines) + "\n")
    return p


def _index(tmp_path):
    from src.claude.session_index import SessionIndex

    idx = SessionIndex(cache_path=tmp_path / "cache.json")
    idx.projects_dir = tmp_path / "projects"
    return idx


def _ai(name):
    return {"type": "ai-title", "aiTitle": name}


def _custom(name):
    return {"type": "custom-title", "customTitle": name}


def test_a_renamed_session_shows_its_chosen_name(tmp_path):
    _write(tmp_path, _ai("Generated name"), _custom("telegram 마개조"))

    info = _index(tmp_path).get("aaaaaaaa-1111-2222-3333-444444444444")

    assert info.display_name == "telegram 마개조"


def test_the_generated_name_is_kept_underneath(tmp_path):
    """So clearing the custom one falls back instead of leaving nothing."""
    _write(tmp_path, _ai("Generated name"), _custom("chosen"))

    info = _index(tmp_path).get("aaaaaaaa-1111-2222-3333-444444444444")

    assert info.title == "Generated name"


def test_a_later_retitle_does_not_undo_the_rename(tmp_path):
    """Claude rewrites ai-title as the conversation evolves."""
    _write(tmp_path, _custom("chosen"), _ai("Something else entirely"))

    info = _index(tmp_path).get("aaaaaaaa-1111-2222-3333-444444444444")

    assert info.display_name == "chosen"


def test_the_latest_rename_wins(tmp_path):
    _write(tmp_path, _custom("first"), _custom("second"))

    info = _index(tmp_path).get("aaaaaaaa-1111-2222-3333-444444444444")

    assert info.display_name == "second"


def test_an_empty_rename_falls_back_to_the_generated_name(tmp_path):
    _write(tmp_path, _ai("Generated name"), _custom("   "))

    info = _index(tmp_path).get("aaaaaaaa-1111-2222-3333-444444444444")

    assert info.display_name == "Generated name"
