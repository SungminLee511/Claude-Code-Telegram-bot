"""Give a session a name, the way `/rename` does.

`/rename` records the chosen name as a `custom-title` line in the transcript,
and the session index reads the last one. Writing that line directly names a
session without spending a whole turn on a command — which matters for a
branch, where the name is decided by the bot and the user never typed it.

The write is a single small append. The CLI is appending to the same file, and
`O_APPEND` makes one short write atomic against other appenders, so the line
cannot land interleaved with another.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger()


def set_title(transcript_path: Optional[str], session_id: str, name: str) -> bool:
    """Name a session. Returns whether the line was written."""
    cleaned = " ".join((name or "").split())
    if not transcript_path or not session_id or not cleaned:
        return False

    line = (
        json.dumps(
            {
                "type": "custom-title",
                "customTitle": cleaned,
                "sessionId": session_id,
            },
            ensure_ascii=False,
        )
        + "\n"
    )
    path = Path(transcript_path)
    # Append mode would happily create the file, leaving a transcript holding
    # nothing but a name — a session that never existed, listed as if it had.
    if not path.is_file():
        logger.debug("session_rename: no such transcript", path=transcript_path)
        return False

    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError as exc:
        logger.warning(
            "session_rename: could not write title",
            path=transcript_path,
            error=str(exc),
        )
        return False
    logger.info("Session renamed", session_id=session_id, name=cleaned)
    return True


def branch_name(original: str, label: str) -> str:
    """`telegram 마개조` + `fast test` -> `telegram 마개조_(branch: fast test)`.

    A branch of a branch keeps stacking, which is honest: the name says how
    far from the original you are.
    """
    label = " ".join((label or "").split()) or "temp"
    return f"{original}_(branch: {label})"
