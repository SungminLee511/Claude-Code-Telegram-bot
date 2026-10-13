"""Tests for the rate-limit meters shown in the banner."""

import json

import pytest

from src.bot.utils import usage_limits as ul
from src.bot.utils.usage_limits import Usage


@pytest.fixture(autouse=True)
def clean_cache():
    ul.reset_cache()
    yield
    ul.reset_cache()


@pytest.fixture()
def creds(tmp_path):
    path = tmp_path / ".credentials.json"
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat01-x"}}))
    return path


def scoped_entry(name="Fable", percent=0, resets_at="2026-10-18T01:59:59Z"):
    return {
        "kind": "weekly_scoped",
        "group": "weekly",
        "percent": percent,
        "resets_at": resets_at,
        "scope": {"model": {"id": None, "display_name": name}, "surface": None},
    }


def answer(five_hour=18.0, weekly=22.0, limits=None, **extra):
    payload = {
        "five_hour": {"utilization": five_hour, "resets_at": "2026-10-12T16:39:59Z"},
        "seven_day": {"utilization": weekly, "resets_at": "2026-10-18T01:59:59Z"},
        "limits": [
            {"kind": "session", "group": "session", "percent": 18, "scope": None},
            {"kind": "weekly_all", "group": "weekly", "percent": 22, "scope": None},
        ]
        + (limits if limits is not None else [scoped_entry()]),
    }
    payload.update(extra)
    return payload


@pytest.fixture()
def endpoint(monkeypatch):
    """Answer the usage request without touching the network."""
    calls = []

    def install(payload, fail=None):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(payload).encode()

        def fake_urlopen(request, timeout=None):
            calls.append(request)
            if fail is not None:
                raise fail
            return FakeResponse()

        monkeypatch.setattr(ul.urllib.request, "urlopen", fake_urlopen)
        return calls

    return install


# --- rendering --------------------------------------------------------------

HOUR = 3600.0
NOW = 1_792_000_000.0  # a fixed moment to render against


def test_each_limit_gets_its_own_line():
    """They are read one at a time; a run-on line makes that a search."""
    rendered = ul.render(Usage(five_hour=18.0, weekly=22.0), now=NOW)

    assert rendered.splitlines() == ["5h 18%", "week 22%"]


def test_a_per_model_allowance_is_named_after_its_model():
    rendered = ul.render(
        Usage(five_hour=18.0, weekly=22.0, scoped=(("Fable", 3.0, None),)), now=NOW
    )

    assert rendered.splitlines() == ["5h 18%", "week 22%", "Fable 3%"]


def test_several_per_model_allowances_all_appear():
    rendered = ul.render(
        Usage(scoped=(("Fable", 3.0, None), ("Opus", 40.0, None))), now=NOW
    )

    assert rendered.splitlines() == ["Fable 3%", "Opus 40%"]


def test_percentages_are_shown_whole():
    assert "18%" in ul.render(Usage(five_hour=17.6, weekly=22.0), now=NOW)


def test_one_known_window_still_renders():
    assert ul.render(Usage(five_hour=5.0), now=NOW) == "5h 5%"


def test_nothing_known_renders_nothing():
    assert ul.render(Usage(), now=NOW) is None


# --- reset time and pace -----------------------------------------------------


def test_the_reset_time_follows_the_percentage():
    usage = Usage(five_hour=7.0, five_hour_resets=NOW + 3 * HOUR)

    assert ul.render(usage, now=NOW).startswith("5h 7%: ")


def test_the_reset_time_is_shown_in_the_users_timezone():
    """The server is on UTC; the banner is read in KST (UTC+9)."""
    import datetime

    moment = NOW + 3 * HOUR
    utc = datetime.datetime.fromtimestamp(moment, tz=datetime.timezone.utc)
    kst = utc.astimezone(datetime.timezone(datetime.timedelta(hours=9)))

    rendered = ul.render(Usage(five_hour=7.0, five_hour_resets=moment), now=NOW)

    assert kst.strftime("%H:%M") in rendered
    assert utc.strftime("%H:%M") not in rendered


def test_the_bracket_is_how_far_through_the_window_we_are():
    """Two hours into a five-hour window is 40% of it."""
    usage = Usage(five_hour=7.0, five_hour_resets=NOW + 3 * HOUR)

    assert "(40%)" in ul.render(usage, now=NOW)


def test_a_fresh_window_is_near_zero_percent_elapsed():
    usage = Usage(five_hour=0.0, five_hour_resets=NOW + 5 * HOUR)

    assert "(0%)" in ul.render(usage, now=NOW)


def test_a_window_about_to_roll_over_is_near_a_hundred():
    usage = Usage(five_hour=90.0, five_hour_resets=NOW + 1.0)

    assert "(100%)" in ul.render(usage, now=NOW)


def test_an_already_passed_reset_does_not_print_over_a_hundred():
    """A stale cached answer must not produce 104%."""
    usage = Usage(five_hour=90.0, five_hour_resets=NOW - HOUR)

    assert "(100%)" in ul.render(usage, now=NOW)


def test_a_clock_skew_does_not_print_a_negative_pace():
    usage = Usage(five_hour=1.0, five_hour_resets=NOW + 6 * HOUR)

    assert "(0%)" in ul.render(usage, now=NOW)


def test_the_weekly_window_names_its_day():
    """Days out, a bare clock time says nothing useful."""
    usage = Usage(weekly=25.0, weekly_resets=NOW + 5 * 24 * HOUR)

    rendered = ul.render(usage, now=NOW)
    assert ":" in rendered
    assert any(day in rendered for day in ("Mon Tue Wed Thu Fri Sat Sun".split()))


def test_the_five_hour_window_does_not_name_a_day():
    usage = Usage(five_hour=7.0, five_hour_resets=NOW + HOUR)

    rendered = ul.render(usage, now=NOW)
    assert not any(
        day in rendered for day in ("Mon Tue Wed Thu Fri Sat Sun".split())
    )


def test_the_weekly_pace_is_measured_over_seven_days():
    """Half a week left is half the window gone."""
    usage = Usage(weekly=25.0, weekly_resets=NOW + 3.5 * 24 * HOUR)

    assert "(50%)" in ul.render(usage, now=NOW)


def test_a_window_without_a_reset_time_still_shows_its_percentage():
    assert ul.render(Usage(five_hour=7.0), now=NOW) == "5h 7%"


def test_a_per_model_allowance_shows_its_own_reset_and_pace():
    usage = Usage(
        weekly=25.0,
        weekly_resets=NOW + 3.5 * 24 * HOUR,
        scoped=(("Fable", 0.0, NOW + 3.5 * 24 * HOUR),),
    )

    last = ul.render(usage, now=NOW).splitlines()[-1]

    assert last.startswith("Fable 0%: ")
    assert "(50%)" in last


def test_a_per_model_allowance_without_a_reset_still_shows_its_share():
    usage = Usage(scoped=(("Fable", 4.0, None),))

    assert ul.render(usage, now=NOW) == "Fable 4%"


# --- fetching ---------------------------------------------------------------


def test_the_two_windows_come_from_the_endpoint(creds, endpoint):
    endpoint(answer())

    usage = ul.fetch(creds)

    assert usage.five_hour == 18.0
    assert usage.weekly == 22.0


def test_the_request_carries_the_stored_token(creds, endpoint):
    calls = endpoint(answer())

    ul.fetch(creds)

    assert calls[0].headers["Authorization"] == "Bearer sk-ant-oat01-x"


def test_no_credentials_file_means_no_request(tmp_path, endpoint):
    calls = endpoint(answer())

    assert ul.fetch(tmp_path / "missing.json") == Usage()
    assert calls == []


def test_a_credentials_file_without_a_token_is_ignored(tmp_path, endpoint):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"something": "else"}))
    calls = endpoint(answer())

    assert ul.fetch(path) == Usage()
    assert calls == []


def test_a_corrupt_credentials_file_is_ignored(tmp_path):
    path = tmp_path / "c.json"
    path.write_text("{not json")

    assert ul.fetch(path) == Usage()


def test_a_failing_request_yields_nothing_rather_than_raising(creds, endpoint):
    endpoint(answer(), fail=OSError("network down"))

    assert ul.fetch(creds) == Usage()


def test_a_window_the_account_does_not_have_is_simply_absent(creds, endpoint):
    endpoint({"five_hour": {"utilization": 9.0}, "seven_day": None})

    usage = ul.fetch(creds)

    assert usage.five_hour == 9.0
    assert usage.weekly is None
    assert usage.scoped == ()


def test_the_per_model_allowance_comes_from_the_limits_list(creds, endpoint):
    endpoint(answer(limits=[scoped_entry("Fable", 7)]))

    (name, percent, resets_at), = ul.fetch(creds).scoped
    assert (name, percent) == ("Fable", 7.0)
    assert resets_at is not None


def test_a_model_added_to_the_plan_later_needs_no_code_change(creds, endpoint):
    """Nothing here names Fable; the endpoint decides what is scoped."""
    endpoint(answer(limits=[scoped_entry("Tangelo", 12)]))

    (name, percent, _), = ul.fetch(creds).scoped
    assert (name, percent) == ("Tangelo", 12.0)


def test_an_unnamed_scope_is_skipped(creds, endpoint):
    """A bare percentage with no label would mean nothing in the banner."""
    endpoint(answer(limits=[{"kind": "weekly_scoped", "percent": 5, "scope": None}]))

    assert ul.fetch(creds).scoped == ()


def test_unscoped_limits_are_not_mistaken_for_model_ones(creds, endpoint):
    endpoint(answer(limits=[]))

    assert ul.fetch(creds).scoped == ()


def test_a_missing_limits_list_is_survivable(creds, endpoint):
    endpoint({"five_hour": {"utilization": 9.0}})

    assert ul.fetch(creds).scoped == ()


def test_an_unexpected_body_shape_is_survivable(creds, endpoint):
    endpoint(["not", "a", "dict"])

    assert ul.fetch(creds) == Usage()


# --- caching ----------------------------------------------------------------


def test_the_answer_is_reused_within_the_cache_window(creds, endpoint):
    calls = endpoint(answer())

    ul.current(creds, now=1000.0)
    ul.current(creds, now=1030.0)

    assert len(calls) == 1


def test_a_stale_answer_is_refetched(creds, endpoint):
    calls = endpoint(answer())

    ul.current(creds, now=1000.0)
    ul.current(creds, now=1000.0 + ul._CACHE_SECONDS + 1)

    assert len(calls) == 2


def test_a_failure_is_cached_too(creds, endpoint):
    """Otherwise every message pays the timeout again."""
    calls = endpoint(answer(), fail=OSError("down"))

    ul.current(creds, now=1000.0)
    ul.current(creds, now=1005.0)

    assert len(calls) == 1


def test_the_reset_times_are_read_from_the_endpoint(creds, endpoint):
    endpoint(answer())

    usage = ul.fetch(creds)

    assert usage.five_hour_resets is not None
    assert usage.weekly_resets > usage.five_hour_resets


def test_a_missing_reset_time_is_not_an_error(creds, endpoint):
    endpoint({"five_hour": {"utilization": 9.0}})

    assert ul.fetch(creds).five_hour_resets is None


def test_an_unparsable_reset_time_is_ignored(creds, endpoint):
    endpoint({"five_hour": {"utilization": 9.0, "resets_at": "soon-ish"}})

    assert ul.fetch(creds).five_hour_resets is None


def test_the_banner_line_is_the_rendered_usage(creds, endpoint):
    endpoint(answer())

    first, second, third = ul.line(creds).splitlines()
    assert first.startswith("5h 18%")
    assert second.startswith("week 22%")
    assert third.startswith("Fable 0%")


def test_a_failed_fetch_is_retried_soon_and_serves_the_last_good(monkeypatch):
    from src.bot.utils import usage_limits as u

    answers = [u.Usage(five_hour=40.0), u.Usage(), u.Usage(five_hour=45.0)]
    calls = []

    def fake_fetch(path=u.CREDENTIALS_PATH):
        calls.append(1)
        return answers[len(calls) - 1]

    u.reset_cache()
    monkeypatch.setattr(u, "fetch", fake_fetch)
    now = [1000.0]
    monkeypatch.setattr(u.time, "time", lambda: now[0])

    assert u.current(max_age=60).five_hour == 40.0
    now[0] += 61
    # The refetch fails: the last good number stands in, not "unknown".
    assert u.current(max_age=60).five_hour == 40.0
    assert len(calls) == 2
    # Within the retry window the failure is not hammered...
    now[0] += 5
    assert u.current(max_age=60).five_hour == 40.0
    assert len(calls) == 2
    # ...but shortly after it is retried, long before max_age.
    now[0] += 12
    assert u.current(max_age=60).five_hour == 45.0
    assert len(calls) == 3
    u.reset_cache()


def test_with_nothing_good_yet_a_failure_is_unknown_and_retried_soon(monkeypatch):
    from src.bot.utils import usage_limits as u

    answers = [u.Usage(), u.Usage(five_hour=10.0)]
    calls = []

    def fake_fetch(path=u.CREDENTIALS_PATH):
        calls.append(1)
        return answers[len(calls) - 1]

    u.reset_cache()
    monkeypatch.setattr(u, "fetch", fake_fetch)
    now = [1000.0]
    monkeypatch.setattr(u.time, "time", lambda: now[0])

    assert u.current(max_age=295).five_hour is None
    now[0] += 16  # far less than 295s
    assert u.current(max_age=295).five_hour == 10.0
    u.reset_cache()
