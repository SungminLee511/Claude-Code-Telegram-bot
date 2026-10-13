"""On-disk Claude Code session index.

The bot's SQLite DB only knows about sessions *this* bot process created.
The real transcripts live in `~/.claude/projects/<slug>/<session_id>.jsonl`
and are shared by every bot on the host, so a disk scan is the only way to
offer a complete session list ("every session", not "this bot's sessions").

Each transcript is an append-only JSONL stream. Useful record types:

* ``ai-title``  -> ``{"type": "ai-title", "aiTitle": "...", "sessionId": ...}``
  Claude Code's own generated session name. Rewritten as the conversation
  evolves, so the *last* one is the current name.
* ``user``      -> a real user turn (or a tool_result echo, which we skip).
* ``assistant`` -> model turn; carries ``cwd`` so we can recover the project.

Because the files are append-only, the scan is **incremental**: the cache
stores the byte offset consumed so far per file and only parses the tail on
rescan. Without that, the 80 MB+ transcripts would be re-parsed on every
`/sessions` invocation.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional

import structlog

logger = structlog.get_logger()

DEFAULT_PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Only lines whose record type is interesting are worth json-parsing; the
# attachment / queue-operation records dominate the file volume. Matching on
# raw bytes avoids decoding multi-MB lines we are about to discard anyway.
# Whitespace-tolerant because the writer's JSON style is not guaranteed.
_TYPE_RE = re.compile(rb'"type"\s*:\s*"(ai-title|custom-title|user|assistant)"')

# Never json-parse a single line larger than this (pathological attachments).
_MAX_LINE_BYTES = 2 * 1024 * 1024

_PREVIEW_CHARS = 300


@dataclass
class SessionInfo:
    """One session transcript on disk."""

    session_id: str
    path: str
    title: Optional[str] = None
    # A name set by hand with `/rename`; outranks the generated one.
    custom_title: Optional[str] = None
    first_prompt: str = ""
    last_prompt: str = ""
    user_turns: int = 0
    cwd: str = ""
    size: int = 0
    mtime: float = 0.0
    # Bytes already consumed by the incremental parser.
    offset: int = 0
    # Filled in at list time, not persisted meaningfully.
    bot_id: str = ""

    @property
    def short_id(self) -> str:
        return self.session_id[:8]

    @property
    def display_name(self) -> str:
        """Human label: a chosen name, else Claude's ai-title, else the
        first prompt."""
        if self.custom_title:
            return self.custom_title
        if self.title:
            return self.title
        if self.first_prompt:
            return self.first_prompt[:60]
        return f"(untitled {self.short_id})"

    def age_text(self, now: Optional[float] = None) -> str:
        now = now if now is not None else time.time()
        delta = max(0.0, now - self.mtime)
        if delta < 60:
            return "just now"
        if delta < 3600:
            return f"{int(delta // 60)}m ago"
        if delta < 86400:
            return f"{int(delta // 3600)}h ago"
        return f"{int(delta // 86400)}d ago"

    def size_text(self) -> str:
        if self.size < 1024:
            return f"{self.size} B"
        if self.size < 1024 * 1024:
            return f"{self.size / 1024:.0f} KB"
        return f"{self.size / 1024 / 1024:.1f} MB"


def _extract_text(content) -> str:
    """Pull plain text out of an SDK message content field."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def _is_real_user_turn(record: dict) -> bool:
    """True for an actual human message, not a tool_result echo or sidechain."""
    if record.get("isSidechain"):
        return False
    message = record.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if isinstance(content, list):
        # tool_result-only payloads are the SDK echoing tool output back in
        # the user role; they are not turns the human typed.
        has_text = any(
            isinstance(b, dict) and b.get("type") == "text" for b in content
        )
        if not has_text:
            return False
    return True


def _clean_preview(text: str) -> str:
    text = text.strip()
    # Strip the harness's system-reminder / caveat wrappers so previews read
    # like what the human actually typed.
    if text.startswith("<"):
        closing = text.find(">")
        if 0 < closing < 120:
            text = text[closing + 1 :].strip()
    return " ".join(text.split())[:_PREVIEW_CHARS]


def _consume(path: Path, info: SessionInfo) -> SessionInfo:
    """Parse the unread tail of `path` into `info` (mutates and returns it)."""
    try:
        stat = path.stat()
    except OSError:
        return info

    if stat.st_size < info.offset:
        # File shrank -> not append-only anymore (rotation/truncation).
        # Drop what we knew and rescan from scratch.
        info.offset = 0
        info.user_turns = 0
        info.first_prompt = ""

    try:
        with open(path, "rb") as handle:
            handle.seek(info.offset)
            data = handle.read()
    except OSError as exc:
        logger.debug("session_index: unreadable transcript", path=str(path), error=str(exc))
        return info

    if data:
        lines = data.split(b"\n")
        if data.endswith(b"\n"):
            complete = lines[:-1]
            consumed = len(data)
        else:
            # Trailing partial line: leave it for the next pass.
            complete = lines[:-1]
            consumed = len(data) - len(lines[-1])

        for raw in complete:
            if not raw or not _TYPE_RE.search(raw):
                continue
            if len(raw) > _MAX_LINE_BYTES:
                continue
            try:
                record = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue

            rtype = record.get("type")
            if rtype == "ai-title":
                title = record.get("aiTitle")
                if isinstance(title, str) and title.strip():
                    info.title = title.strip()
            elif rtype == "custom-title":
                # What `/rename` writes. Kept apart from the generated title
                # rather than overwriting it, because Claude rewrites
                # `ai-title` as the conversation evolves and would otherwise
                # silently undo the rename a few turns later.
                chosen = record.get("customTitle")
                info.custom_title = (
                    chosen.strip()
                    if isinstance(chosen, str) and chosen.strip()
                    else None
                )
            elif rtype in ("user", "assistant"):
                cwd = record.get("cwd")
                # First cwd wins: that is the workspace the session was
                # started in. Later records drift as the agent `cd`s around,
                # which would make the session look like it lives somewhere
                # it does not.
                if isinstance(cwd, str) and cwd and not info.cwd:
                    info.cwd = cwd
                if rtype == "user" and _is_real_user_turn(record):
                    text = _clean_preview(
                        _extract_text(record.get("message", {}).get("content"))
                    )
                    if text:
                        info.user_turns += 1
                        if not info.first_prompt:
                            info.first_prompt = text
                        info.last_prompt = text

        info.offset += consumed

    info.size = stat.st_size
    info.mtime = stat.st_mtime
    return info


class SessionIndex:
    """Disk-backed, incrementally-updated index of Claude Code transcripts."""

    def __init__(
        self,
        cache_path: Path,
        projects_dir: Path = DEFAULT_PROJECTS_DIR,
    ):
        self.cache_path = Path(cache_path)
        self.projects_dir = Path(projects_dir)
        self._cache: Dict[str, SessionInfo] = {}
        self._loaded = False

    # --- cache persistence -------------------------------------------------

    def _load_cache(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            raw = json.loads(self.cache_path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        for sid, payload in (raw.get("sessions") or {}).items():
            try:
                self._cache[sid] = SessionInfo(**payload)
            except TypeError:
                continue

    def _save_cache(self) -> None:
        payload = {
            "version": 1,
            "sessions": {sid: asdict(info) for sid, info in self._cache.items()},
        }
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload))
            os.replace(tmp, self.cache_path)
        except OSError as exc:
            logger.debug("session_index: cache write failed", error=str(exc))

    # --- scanning ----------------------------------------------------------

    def _transcript_paths(self) -> List[Path]:
        if not self.projects_dir.is_dir():
            return []
        return sorted(self.projects_dir.glob("*/*.jsonl"))

    def refresh(self) -> List[SessionInfo]:
        """Rescan transcripts (incrementally) and return them, newest first."""
        self._load_cache()
        seen = set()
        changed = False

        for path in self._transcript_paths():
            session_id = path.stem
            seen.add(session_id)
            try:
                stat = path.stat()
            except OSError:
                continue

            info = self._cache.get(session_id)
            if info is None:
                info = SessionInfo(session_id=session_id, path=str(path))
                self._cache[session_id] = info
                changed = True
            else:
                info.path = str(path)
                if stat.st_size == info.size and stat.st_mtime == info.mtime:
                    continue  # unchanged since last scan

            _consume(path, info)
            changed = True

        # Forget transcripts that disappeared.
        for stale in [sid for sid in self._cache if sid not in seen]:
            del self._cache[stale]
            changed = True

        if changed:
            self._save_cache()

        return sorted(self._cache.values(), key=lambda i: i.mtime, reverse=True)

    def get(self, session_id: str, refresh: bool = True) -> Optional[SessionInfo]:
        """Look up one session, refreshing just that transcript."""
        self._load_cache()
        info = self._cache.get(session_id)
        if info is None:
            matches = list(self.projects_dir.glob(f"*/{session_id}.jsonl"))
            if not matches:
                return None
            info = SessionInfo(session_id=session_id, path=str(matches[0]))
            self._cache[session_id] = info
            _consume(matches[0], info)
            self._save_cache()
            return info

        if refresh:
            path = Path(info.path)
            try:
                stat = path.stat()
            except OSError:
                return info
            if stat.st_size != info.size or stat.st_mtime != info.mtime:
                _consume(path, info)
                self._save_cache()
        return info

    def search(self, query: str) -> List[SessionInfo]:
        """Case-insensitive match over title and prompt previews."""
        sessions = self.refresh()
        if not query:
            return sessions
        needle = query.lower()
        return [
            s
            for s in sessions
            if needle in (s.title or "").lower()
            or needle in s.first_prompt.lower()
            or needle in s.last_prompt.lower()
            or needle in s.session_id.lower()
        ]


def tail_messages(path: Path, limit: int = 10) -> List[Dict[str, str]]:
    """Return the last `limit` user/assistant text turns of a transcript.

    Reads only the final chunk of the file so an 80 MB transcript previews as
    fast as a small one.
    """
    chunk = 512 * 1024
    try:
        size = path.stat().st_size
        with open(path, "rb") as handle:
            handle.seek(max(0, size - chunk))
            data = handle.read()
    except OSError:
        return []

    out: List[Dict[str, str]] = []
    for raw in data.split(b"\n"):
        if not raw or len(raw) > _MAX_LINE_BYTES:
            continue
        if not _TYPE_RE.search(raw):
            continue
        try:
            record = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        rtype = record.get("type")
        if rtype not in ("user", "assistant"):
            continue
        if rtype == "user" and not _is_real_user_turn(record):
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        text = _clean_preview(_extract_text(message.get("content")))
        if not text:
            continue
        out.append({"role": rtype, "text": text, "ts": record.get("timestamp", "")})

    return out[-limit:]
