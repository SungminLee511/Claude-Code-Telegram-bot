"""Unified storage interface.

Provides simple API for the rest of the application.
"""

from datetime import UTC, datetime
from typing import Optional

import structlog

from ..claude.sdk_integration import ClaudeResponse
from .database import DatabaseManager
from .models import (
    AuditLogModel,
    MessageModel,
    SessionModel,
    ToolUsageModel,
)
from .repositories import (
    AuditLogRepository,
    CostTrackingRepository,
    MessageRepository,
    SessionRepository,
    ToolUsageRepository,
    UserRepository,
)

logger = structlog.get_logger()


class Storage:
    """Main storage interface."""

    def __init__(self, database_url: str):
        """Initialize storage with database URL."""
        self.db_manager = DatabaseManager(database_url)
        self.users = UserRepository(self.db_manager)
        self.sessions = SessionRepository(self.db_manager)
        self.messages = MessageRepository(self.db_manager)
        self.tools = ToolUsageRepository(self.db_manager)
        self.audit = AuditLogRepository(self.db_manager)
        self.costs = CostTrackingRepository(self.db_manager)

    async def initialize(self):
        """Initialize storage system."""
        logger.info("Initializing storage system")
        await self.db_manager.initialize()
        logger.info("Storage system initialized")

    async def close(self):
        """Close storage connections."""
        logger.info("Closing storage system")
        await self.db_manager.close()

    async def health_check(self) -> bool:
        """Check storage system health."""
        return await self.db_manager.health_check()

    # High-level operations

    async def save_claude_interaction(
        self,
        user_id: int,
        session_id: str,
        prompt: str,
        response: ClaudeResponse,
        ip_address: Optional[str] = None,
    ):
        """Save complete Claude interaction."""
        logger.info(
            "Saving Claude interaction",
            user_id=user_id,
            session_id=session_id,
            cost=response.cost,
        )

        # Save message
        message = MessageModel(
            message_id=None,
            session_id=session_id,
            user_id=user_id,
            timestamp=datetime.now(UTC),
            prompt=prompt,
            response=response.content,
            cost=response.cost,
            duration_ms=response.duration_ms,
            error=response.error_type if response.is_error else None,
        )

        message_id = await self.messages.save_message(message)

        # Save tool usage
        if response.tools_used:
            for tool in response.tools_used:
                tool_usage = ToolUsageModel(
                    id=None,
                    session_id=session_id,
                    message_id=message_id,
                    tool_name=tool["name"],
                    tool_input=tool.get("input", {}),
                    timestamp=datetime.now(UTC),
                    success=not response.is_error,
                    error_message=response.error_type if response.is_error else None,
                )
                await self.tools.save_tool_usage(tool_usage)

        # Update cost tracking
        await self.costs.update_daily_cost(user_id, response.cost)

        # Update user stats
        user = await self.users.get_user(user_id)
        if user:
            user.total_cost += response.cost
            user.message_count += 1
            user.last_active = datetime.now(UTC)
            await self.users.update_user(user)

        # Update session stats
        session = await self.sessions.get_session(session_id)
        if session:
            session.total_cost += response.cost
            session.total_turns += response.num_turns
            session.message_count += 1
            session.last_used = datetime.now(UTC)
            await self.sessions.update_session(session)

        # Log audit event
        audit_event = AuditLogModel(
            id=None,
            user_id=user_id,
            event_type="claude_interaction",
            event_data={
                "session_id": session_id,
                "cost": response.cost,
                "duration_ms": response.duration_ms,
                "num_turns": response.num_turns,
                "is_error": response.is_error,
                "tools_used": [t["name"] for t in response.tools_used],
            },
            success=not response.is_error,
            timestamp=datetime.now(UTC),
            ip_address=ip_address,
        )
        await self.audit.log_event(audit_event)

    async def create_session(
        self, user_id: int, project_path: str, session_id: str
    ) -> SessionModel:
        """Create new session."""
        session = SessionModel(
            session_id=session_id,
            user_id=user_id,
            project_path=project_path,
            created_at=datetime.now(UTC),
            last_used=datetime.now(UTC),
        )

        await self.sessions.create_session(session)

        # Update user session count
        user = await self.users.get_user(user_id)
        if user:
            user.session_count += 1
            await self.users.update_user(user)

        return session

    # Convenience methods

