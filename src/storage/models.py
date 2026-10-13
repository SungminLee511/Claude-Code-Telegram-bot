"""Data models for storage.

Using dataclasses for simplicity and type safety.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Dict, Optional

import aiosqlite


def _parse_datetime(value: Any) -> Any:
    """Parse datetime values from SQLite rows.

    With sqlite3 converters enabled, values may already be datetime instances.
    Without converters, values may be ISO strings.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    return value


@dataclass
class UserModel:
    """User data model."""

    user_id: int
    telegram_username: Optional[str] = None
    first_seen: Optional[datetime] = None
    last_active: Optional[datetime] = None
    is_allowed: bool = False
    total_cost: float = 0.0
    message_count: int = 0
    session_count: int = 0

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "UserModel":
        """Create from database row."""
        data = dict(row)

        # Parse datetime fields
        for field in ["first_seen", "last_active"]:
            data[field] = _parse_datetime(data.get(field))

        return cls(**data)


@dataclass
class SessionModel:
    """Session data model."""

    session_id: str
    user_id: int
    project_path: str
    created_at: datetime
    last_used: datetime
    total_cost: float = 0.0
    total_turns: int = 0
    message_count: int = 0
    is_active: bool = True

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "SessionModel":
        """Create from database row."""
        data = dict(row)

        # Parse datetime fields
        for field in ["created_at", "last_used"]:
            data[field] = _parse_datetime(data.get(field))

        return cls(**data)

    def is_expired(self, timeout_hours: int) -> bool:
        """Check if session has expired."""
        if not self.last_used:
            return True

        age = datetime.now(UTC) - self.last_used
        return age.total_seconds() > (timeout_hours * 3600)


@dataclass
class MessageModel:
    """Message data model."""

    session_id: str
    user_id: int
    timestamp: datetime
    prompt: str
    message_id: Optional[int] = None
    response: Optional[str] = None
    cost: float = 0.0
    duration_ms: Optional[int] = None
    error: Optional[str] = None

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "MessageModel":
        """Create from database row."""
        data = dict(row)

        # Parse datetime fields
        data["timestamp"] = _parse_datetime(data.get("timestamp"))

        return cls(**data)


@dataclass
class ToolUsageModel:
    """Tool usage data model."""

    session_id: str
    tool_name: str
    timestamp: datetime
    id: Optional[int] = None
    message_id: Optional[int] = None
    tool_input: Optional[Dict[str, Any]] = None
    success: bool = True
    error_message: Optional[str] = None

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "ToolUsageModel":
        """Create from database row."""
        data = dict(row)

        # Parse datetime fields
        data["timestamp"] = _parse_datetime(data.get("timestamp"))

        # Parse JSON fields
        if data.get("tool_input"):
            try:
                data["tool_input"] = json.loads(data["tool_input"])
            except (json.JSONDecodeError, TypeError):
                data["tool_input"] = {}

        return cls(**data)


@dataclass
class AuditLogModel:
    """Audit log data model."""

    user_id: int
    event_type: str
    timestamp: datetime
    id: Optional[int] = None
    event_data: Optional[Dict[str, Any]] = None
    success: bool = True
    ip_address: Optional[str] = None

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "AuditLogModel":
        """Create from database row."""
        data = dict(row)

        # Parse datetime fields
        data["timestamp"] = _parse_datetime(data.get("timestamp"))

        # Parse JSON fields
        if data.get("event_data"):
            try:
                data["event_data"] = json.loads(data["event_data"])
            except (json.JSONDecodeError, TypeError):
                data["event_data"] = {}

        return cls(**data)


