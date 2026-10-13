"""Discover scheduled-but-not-yet-fired wake-ups.

`wake_after.sh` schedules a wake by leaving a detached `bash -c "sleep N; ..."`
running. Nothing records that anywhere: the sleeping process *is* the schedule.
So this reads the processes rather than a registry file, which means a wake
killed by hand, or lost to a reboot, disappears from the list by itself and a
wake scheduled before this code existed still shows up.

Three facts are needed per wake and all three are readable from `/proc`:

    cmdline  the generated script, carrying `(bot=<id>)` and the delay
    stat     field 22, start time in clock ticks since boot -> when it began
    environ  WAKE_MESSAGE, the prompt the wake will send

The message is in the environment rather than the command line because
`wake_after.sh` exports it to keep quoting safe, so `ps` alone cannot see it.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import structlog

logger = structlog.get_logger()

# Emitted by the worker that wake_after.sh generates. Matching the echo line
# rather than the `sleep` keeps us from picking up unrelated sleeps.
_WAKE_RE = re.compile(r"firing wake after (\d+)s \(bot=([A-Za-z0-9_-]+)\)")

_PROC = Path("/proc")


@dataclass(frozen=True)
class PendingWake:
    """A wake that has been scheduled and has not fired yet."""

    pid: int
    bot_id: str
    message: str
    fires_at: float  # unix seconds

    def seconds_left(self, now: Optional[float] = None) -> float:
        return self.fires_at - (now if now is not None else time.time())


def _boot_time(proc: Path) -> Optional[float]:
    """Unix time the machine booted, to turn /proc ticks into wall clock."""
    try:
        for line in (proc / "stat").read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _process_start(stat_text: str, clock_ticks: int) -> Optional[float]:
    """Seconds after boot that this process started (stat field 22).

    Fields are counted from after the comm field, which is parenthesised and
    may itself contain spaces, so the split starts at the last ')'.
    """
    try:
        rest = stat_text[stat_text.rindex(")") + 2 :].split()
        return int(rest[19]) / clock_ticks
    except (ValueError, IndexError, ZeroDivisionError):
        return None


def _read_message(proc_dir: Path) -> str:
    try:
        raw = (proc_dir / "environ").read_bytes().decode("utf-8", "replace")
    except OSError:
        return ""
    for item in raw.split("\0"):
        key, sep, value = item.partition("=")
        if sep and key == "WAKE_MESSAGE":
            return value
    return ""


def scan(bot_id: Optional[str] = None, proc: Path = _PROC) -> List[PendingWake]:
    """Return pending wakes, soonest first.

    `bot_id` filters to one bot's wakes; None returns every bot's. Any process
    that vanishes mid-read is skipped — between listing /proc and reading a
    file the wake may simply have fired.
    """
    boot = _boot_time(proc)
    if boot is None:
        return []
    try:
        clock_ticks = os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError):
        clock_ticks = 100

    wakes: List[PendingWake] = []
    try:
        entries = list(proc.iterdir())
    except OSError:
        return []

    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().decode("utf-8", "replace")
        except OSError:
            continue  # exited, or not ours to read
        match = _WAKE_RE.search(cmdline)
        if not match:
            continue
        delay, found_bot = int(match.group(1)), match.group(2)
        if bot_id is not None and found_bot != bot_id:
            continue
        try:
            stat_text = (entry / "stat").read_text()
        except OSError:
            continue
        started = _process_start(stat_text, clock_ticks)
        if started is None:
            continue
        wakes.append(
            PendingWake(
                pid=int(entry.name),
                bot_id=found_bot,
                message=_read_message(entry),
                fires_at=boot + started + delay,
            )
        )

    wakes.sort(key=lambda w: w.fires_at)
    return wakes


def scan_quietly(bot_id: Optional[str] = None) -> List[PendingWake]:
    """`scan` that never raises — it only ever decorates a message."""
    try:
        return scan(bot_id)
    except Exception as exc:
        logger.debug("pending_wakes: scan failed", error=str(exc))
        return []
