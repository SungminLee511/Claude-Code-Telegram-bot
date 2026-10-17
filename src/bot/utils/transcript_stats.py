"""Context size of a session the bot is not currently running.

For the live session the count arrives with the reply. For every *other*
session — the ones listed by `/sessions` — nobody is holding that number, so
it has to come from the transcript.

Only the tail is read. The last assistant message carries the input counts for
the whole conversation as it stood then, so the answer is always near the end
of the file; reading a few hundred kilobytes finds it without touching the
thirty megabytes in front.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional, Tuple

import structlog

logger = structlog.get_logger()

# Enough to hold the last few turns even when one of them is a large tool
# result. A transcript whose final turns exceed this reports nothing rather
# than a wrong number.
_TAIL_BYTES = 512 * 1024

_INPUT_KEYS = (
    "input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)

# path -> (size when read, answer)
_cache: Dict[str, Tuple[int, Optional[int]]] = {}


def _from_usage(usage) -> Optional[int]:
    if not isinstance(usage, dict):
        return None
    total = 0
    for key in _INPUT_KEYS:
        value = usage.get(key)
        if isinstance(value, (int, float)):
            total += int(value)
    return total or None


def context_tokens(transcript_path: Optional[str]) -> Optional[int]:
    """Context the model last held in this session, or None if unknown.

    Cached against the file size, so a session that has not moved is answered
    without a read and one that has grown is looked at again.
    """
    if not transcript_path:
        return None

    try:
        path = Path(transcript_path)
        size = path.stat().st_size
    except OSError:
        return None

    cached = _cache.get(transcript_path)
    if cached is not None and cached[0] == size:
        return cached[1]

    found: Optional[int] = None
    try:
        with path.open("rb") as handle:
            if size > _TAIL_BYTES:
                handle.seek(size - _TAIL_BYTES)
                handle.readline()  # drop the partial line we landed inside
            lines = handle.read().splitlines()
    except OSError as exc:
        logger.debug(
            "transcript_stats: unreadable", path=transcript_path, error=str(exc)
        )
        _cache[transcript_path] = (size, None)
        return None

    for line in reversed(lines):
        if b'"usage"' not in line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(entry, dict):
            continue
        message = entry.get("message")
        # Assistant turns only: a result line sums the whole turn, which is
        # several times the context when tools were used.
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        found = _from_usage(message.get("usage"))
        if found is not None:
            break

    _cache[transcript_path] = (size, found)
    return found


# path -> (size when read, answer)
_model_cache: Dict[str, Tuple[int, Optional[str]]] = {}


def last_model(transcript_path: Optional[str]) -> Optional[str]:
    """The model that wrote the session's last reply, or None if unknown.

    This is what the live banner reports for a fresh reply, so the session list
    can say the same thing for sessions nobody is running. Sub-agent turns and
    the CLI's own placeholder entries (`<synthetic>`) are skipped: neither is
    the model answering you.
    """
    if not transcript_path:
        return None
    try:
        path = Path(transcript_path)
        size = path.stat().st_size
    except OSError:
        return None

    cached = _model_cache.get(transcript_path)
    if cached is not None and cached[0] == size:
        return cached[1]

    found: Optional[str] = None
    try:
        with path.open("rb") as handle:
            if size > _TAIL_BYTES:
                handle.seek(size - _TAIL_BYTES)
                handle.readline()
            lines = handle.read().splitlines()
    except OSError:
        _model_cache[transcript_path] = (size, None)
        return None

    for line in reversed(lines):
        if b'"model"' not in line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(entry, dict) or entry.get("isSidechain"):
            continue
        message = entry.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        model = message.get("model")
        if isinstance(model, str) and model and not model.startswith("<"):
            found = model
            break

    _model_cache[transcript_path] = (size, found)
    return found


def reset_cache() -> None:
    """Forget every remembered answer — for tests."""
    _cache.clear()
    _model_cache.clear()
