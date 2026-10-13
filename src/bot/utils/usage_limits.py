"""How much of the subscription's rate limits is already spent.

Claude Code's own `/usage` screen reads this from the account endpoint, using
the OAuth token the CLI stores on disk. The bot has no session of its own — it
spawns that same CLI — so it reads the same file and asks the same question,
and the number in the banner is the number `/usage` would show.

Three kinds of window are reported: the rolling five hours, the week across all
models, and a per-model weekly allowance (Fable has its own). The per-model
ones are read from the endpoint's `limits` list rather than named here, so a
model added to the plan later shows up without a code change.

The answer is cached for a minute. A banner is rendered after every reply, and
these windows move in hours and days; refetching per message would add a
network round trip to every answer for no new information.

Nothing here is allowed to fail loudly. A missing credentials file, an expired
token, a sulking network — each simply means no usage line this time.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple

import structlog

logger = structlog.get_logger()


def _default_credentials_path() -> Path:
    """Where the CLI's OAuth credentials live for whoever is running the bot.

    An explicit `CLAUDE_CREDENTIALS_PATH` wins — some setups keep this file
    outside `$HOME` or want to point at a different account for testing.
    Otherwise it is `~/.claude/.credentials.json`, same place the CLI writes
    it. A previous version hardcoded `/root/.claude/...` and so the usage line
    silently went missing for any bot not running as root.
    """
    override = os.environ.get("CLAUDE_CREDENTIALS_PATH")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude" / ".credentials.json"


CREDENTIALS_PATH = _default_credentials_path()
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

_TIMEOUT = 5.0
_CACHE_SECONDS = 60.0

# The server runs on UTC; the person reading the banner does not.
_DISPLAY_TZ = timezone(timedelta(hours=9))  # KST

# Window lengths, needed to say how far through one we are. The endpoint
# reports only when a window ends, and these two are what their names say.
FIVE_HOURS = 5 * 60 * 60
SEVEN_DAYS = 7 * 24 * 60 * 60


@dataclass(frozen=True)
class Usage:
    """Percentages of each window that are spent, 0-100, and when they reset.

    The reset moments are unix seconds. They turn the raw percentage into
    something you can act on: 40% of the budget gone is fine a day into the
    week and alarming an hour in, so the line shows how far through the
    *window* you are next to how far through the *budget*.
    """

    five_hour: Optional[float] = None
    weekly: Optional[float] = None
    # (model name, percent, resets_at) for allowances scoped to one model,
    # e.g. Fable. Its own reset is carried rather than assumed to match the
    # overall weekly one — the endpoint reports them separately.
    scoped: Tuple[Tuple[str, float, Optional[float]], ...] = field(
        default_factory=tuple
    )
    five_hour_resets: Optional[float] = None
    weekly_resets: Optional[float] = None

    @property
    def known(self) -> bool:
        return (
            self.five_hour is not None or self.weekly is not None or bool(self.scoped)
        )


_cache: Optional[Tuple[float, Usage]] = None


def _token(path: Path) -> Optional[str]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        return None
    token = oauth.get("accessToken")
    return token if isinstance(token, str) and token else None


def _percent(payload: dict, key: str) -> Optional[float]:
    window = payload.get(key)
    if not isinstance(window, dict):
        return None
    value = window.get("utilization")
    return float(value) if isinstance(value, (int, float)) else None


def _resets(payload: dict, key: str) -> Optional[float]:
    """When this window rolls over, as unix seconds.

    The endpoint sends an ISO timestamp with an offset; everything downstream
    wants a number, and the banner renders it in the user's timezone.
    """
    window = payload.get(key)
    if not isinstance(window, dict):
        return None
    return _iso(window.get("resets_at"))


def _iso(raw) -> Optional[float]:
    """An ISO timestamp from the endpoint, as unix seconds."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _scoped(payload: dict) -> Tuple[Tuple[str, float, Optional[float]], ...]:
    """Weekly allowances that apply to a single model, newest API shape.

    Entries without a model name are skipped: a percentage with nothing to
    label it would be worse than leaving it out.
    """
    entries = payload.get("limits")
    if not isinstance(entries, list):
        return ()

    found: List[Tuple[str, float, Optional[float]]] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("kind") != "weekly_scoped":
            continue
        scope = entry.get("scope")
        model = scope.get("model") if isinstance(scope, dict) else None
        name = model.get("display_name") if isinstance(model, dict) else None
        percent = entry.get("percent")
        if isinstance(name, str) and name and isinstance(percent, (int, float)):
            found.append((name, float(percent), _iso(entry.get("resets_at"))))
    return tuple(found)


def fetch(path: Path = CREDENTIALS_PATH) -> Usage:
    """Ask the account endpoint, uncached. Never raises."""
    token = _token(path)
    if not token:
        return Usage()

    request = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # network, auth, malformed body — all the same here
        logger.debug("usage_limits: fetch failed", error=str(exc))
        return Usage()

    if not isinstance(payload, dict):
        return Usage()
    return Usage(
        five_hour=_percent(payload, "five_hour"),
        weekly=_percent(payload, "seven_day"),
        scoped=_scoped(payload),
        five_hour_resets=_resets(payload, "five_hour"),
        weekly_resets=_resets(payload, "seven_day"),
    )


def current(
    path: Path = CREDENTIALS_PATH,
    now: Optional[float] = None,
    max_age: float = _CACHE_SECONDS,
) -> Usage:
    """The cached usage, refetched when the cache is older than `max_age`.

    A failed fetch is cached too, briefly: if the token is gone, every message
    would otherwise pay the timeout again.
    """
    global _cache
    now = time.time() if now is None else now
    if _cache is not None and now - _cache[0] < max_age:
        return _cache[1]
    usage = fetch(path)
    _cache = (now, usage)
    return usage


def five_hour_percent(max_age: float = 5.0) -> Optional[float]:
    """The five-hour spend, 0-100, no older than `max_age` seconds, or None.

    For the live bar under a running request, which cannot use the banner's
    one-minute cache. Blocking (a short HTTP call): run it in a thread.
    """
    try:
        return current(max_age=max_age).five_hour
    except Exception as exc:  # decoration must never cost a reply
        logger.debug("usage_limits: five_hour_percent failed", error=str(exc))
        return None


BAR_CELLS = 10


def bar(percent: float) -> str:
    """`▰▰▰▱▱▱▱▱▱▱ 31%` — ten cells, filled to the nearest tenth."""
    clamped = max(0.0, min(100.0, percent))
    filled = round(clamped / 100 * BAR_CELLS)
    return f"{'▰' * filled}{'▱' * (BAR_CELLS - filled)} {clamped:.0f}%"


def reset_cache() -> None:
    """Forget the cached answer — for tests, and after an auth change."""
    global _cache
    _cache = None


def _clock(unix_seconds: float, with_weekday: bool) -> str:
    """The reset moment in the user's timezone.

    A weekday is worth the characters only when the reset is days away; for
    the five-hour window it would be noise.
    """
    moment = datetime.fromtimestamp(unix_seconds, tz=_DISPLAY_TZ)
    return moment.strftime("%a %H:%M" if with_weekday else "%H:%M")


def _elapsed_percent(
    resets_at: float, window_seconds: float, now: float
) -> Optional[float]:
    """How far through the window we are, 0-100.

    Read next to the usage percentage this is a pace: 40% of the budget at 20%
    of the window means the budget runs out early. Clamped, because a clock
    skew or a window that has just rolled over should not print -4%.
    """
    if window_seconds <= 0:
        return None
    started = resets_at - window_seconds
    fraction = (now - started) / window_seconds
    return max(0.0, min(1.0, fraction)) * 100.0


def _window(
    label: str,
    percent: Optional[float],
    resets_at: Optional[float],
    window_seconds: float,
    with_weekday: bool,
    now: float,
) -> Optional[str]:
    """`5h 7%: 13:39 (36%)` — spend, reset time, and pace through the window."""
    if percent is None:
        return None
    text = f"{label} {percent:.0f}%"
    if resets_at is None:
        return text
    elapsed = _elapsed_percent(resets_at, window_seconds, now)
    tail = f" ({elapsed:.0f}%)" if elapsed is not None else ""
    return f"{text}: {_clock(resets_at, with_weekday)}{tail}"


def render(usage: Usage, now: Optional[float] = None) -> Optional[str]:
    """One limit per line, each with its reset time and pace::

        5h 7%: 13:39 (36%)
        week 25%: Sun 10:59 (29%)
        Fable 0%: Sun 10:59 (29%)

    A line apiece because the three are read one at a time — "am I about to
    run out of *this* one" — and a single run-on line makes that a search.
    The bracketed number is the share of the *window* that has gone by, not
    of the budget; the two together say whether the pace is sustainable.

    None if nothing is known.
    """
    if not usage.known:
        return None
    now = time.time() if now is None else now

    lines: List[str] = []
    first = _window(
        "5h", usage.five_hour, usage.five_hour_resets, FIVE_HOURS, False, now
    )
    if first:
        lines.append(first)
    second = _window(
        "week", usage.weekly, usage.weekly_resets, SEVEN_DAYS, True, now
    )
    if second:
        lines.append(second)
    for name, percent, resets_at in usage.scoped:
        line = _window(name, percent, resets_at, SEVEN_DAYS, True, now)
        if line:
            lines.append(line)
    return "\n".join(lines)


def line(path: Path = CREDENTIALS_PATH) -> Optional[str]:
    """The banner's usage line, or None when it cannot be worked out."""
    try:
        return render(current(path))
    except Exception as exc:  # decoration must never cost a reply
        logger.debug("usage_limits: render failed", error=str(exc))
        return None
