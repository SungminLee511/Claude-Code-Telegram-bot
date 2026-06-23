"""High-level Claude Code integration facade.

Provides simple interface for bot handlers.
"""

import asyncio
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import structlog

from ..config.settings import Settings
from .exceptions import ClaudeTimeoutError
from .sdk_integration import ClaudeResponse, ClaudeSDKManager, StreamUpdate
from .session import SessionManager

logger = structlog.get_logger()

# When the bundled `claude` CLI subprocess is killed by a signal it exits with
# 128+signal: SIGTERM=143, SIGKILL=137. This is a *transient* kill (host
# restart, OOM, an external `kill`, a deploy) — the on-disk session transcript
# is intact, so re-resuming the SAME session id recovers full context. This is
# NOT the same as a genuine "session gone" (the CLI exits 1 with a "No
# conversation found" message), and NOT the same as a user interrupt (handled
# in-process as a cancellation, never reaching this layer).
_SUBPROCESS_KILL_RE = re.compile(r"exit code (?:143|137)\b")

# Bounded same-id resume retries after a transient subprocess kill. Kept small
# and self-contained (no extra config plumbing): worst case after exhaustion is
# identical to the legacy fresh-session fallback.
_RESUME_AFTER_KILL_MAX_ATTEMPTS = 3
_RESUME_AFTER_KILL_BASE_DELAY = 1.0
_RESUME_AFTER_KILL_MAX_DELAY = 5.0


def _is_transient_subprocess_kill(error: BaseException) -> bool:
    """True when the Claude CLI subprocess was killed by a signal (exit
    143/137) rather than exiting because the session is unusable."""
    return bool(_SUBPROCESS_KILL_RE.search(str(error)))


class ClaudeIntegration:
    """Main integration point for Claude Code."""

    def __init__(
        self,
        config: Settings,
        sdk_manager: Optional[ClaudeSDKManager] = None,
        session_manager: Optional[SessionManager] = None,
    ):
        """Initialize Claude integration facade."""
        self.config = config
        self.sdk_manager = sdk_manager or ClaudeSDKManager(config)
        self.session_manager = session_manager

    async def run_command(
        self,
        prompt: str,
        working_directory: Path,
        user_id: int,
        session_id: Optional[str] = None,
        on_stream: Optional[Callable[[StreamUpdate], None]] = None,
        force_new: bool = False,
        interrupt_event: Optional["asyncio.Event"] = None,
        images: Optional[List[Dict[str, str]]] = None,
    ) -> ClaudeResponse:
        """Run Claude Code command with full integration."""
        logger.info(
            "Running Claude command",
            user_id=user_id,
            working_directory=str(working_directory),
            session_id=session_id,
            prompt_length=len(prompt),
            force_new=force_new,
        )

        # If no session_id provided, try to find an existing session for this
        # user+directory combination (auto-resume).
        # Skip auto-resume when force_new is set (e.g. after /new command).
        if not session_id and not force_new:
            existing_session = await self._find_resumable_session(
                user_id, working_directory
            )
            if existing_session:
                session_id = existing_session.session_id
                logger.info(
                    "Auto-resuming existing session for project",
                    session_id=session_id,
                    project_path=str(working_directory),
                    user_id=user_id,
                )

        # Get or create session
        session = await self.session_manager.get_or_create_session(
            user_id, working_directory, session_id
        )

        # Execute command
        try:
            # Continue session if we have an existing session with a real ID
            is_new = getattr(session, "is_new_session", False)
            should_continue = not is_new and bool(session.session_id)

            # For new sessions, don't pass session_id to Claude Code
            claude_session_id = session.session_id if should_continue else None

            try:
                response = await self._execute(
                    prompt=prompt,
                    working_directory=working_directory,
                    session_id=claude_session_id,
                    continue_session=should_continue,
                    stream_callback=on_stream,
                    interrupt_event=interrupt_event,
                    images=images,
                )
            except (asyncio.TimeoutError, ClaudeTimeoutError):
                # Wall-clock timeout — the session is still healthy on Claude's
                # side, just the user-configured timeout fired. Do NOT remove
                # the session; let the caller retry on the next message.
                logger.warning(
                    "Claude command timed out; preserving session for resume",
                    session_id=session.session_id,
                )
                raise
            except Exception as resume_error:
                # New sessions have no id to resume — nothing to recover.
                if not should_continue:
                    raise

                response = None

                # A transient subprocess kill (SIGTERM/SIGKILL, exit 143/137)
                # leaves the session transcript intact on disk. Re-resume the
                # SAME id with bounded backoff *before* discarding any context.
                # Previously this branch treated every resume failure as
                # "session gone" and started a fresh, empty session — turning a
                # momentary kill into permanent context loss.
                if _is_transient_subprocess_kill(resume_error):
                    last_kill_error: Exception = resume_error
                    for attempt in range(1, _RESUME_AFTER_KILL_MAX_ATTEMPTS + 1):
                        delay = min(
                            _RESUME_AFTER_KILL_BASE_DELAY * (2 ** (attempt - 1)),
                            _RESUME_AFTER_KILL_MAX_DELAY,
                        )
                        logger.warning(
                            "Claude subprocess killed (exit 143/137); "
                            "re-resuming same session to preserve context",
                            session_id=session.session_id,
                            attempt=attempt,
                            max_attempts=_RESUME_AFTER_KILL_MAX_ATTEMPTS,
                            delay_seconds=delay,
                            error=str(last_kill_error),
                        )
                        await asyncio.sleep(delay)
                        try:
                            response = await self._execute(
                                prompt=prompt,
                                working_directory=working_directory,
                                session_id=claude_session_id,
                                continue_session=True,
                                stream_callback=on_stream,
                                interrupt_event=interrupt_event,
                                images=images,
                            )
                            logger.info(
                                "Recovered session after subprocess kill",
                                session_id=session.session_id,
                                attempt=attempt,
                            )
                            break  # recovered with full context intact
                        except (asyncio.TimeoutError, ClaudeTimeoutError):
                            # Hard timeout — session still healthy; let caller
                            # retry on the next message. Do not discard.
                            raise
                        except Exception as retry_error:
                            last_kill_error = retry_error
                            if not _is_transient_subprocess_kill(retry_error):
                                # Turned into a real session error — stop
                                # retrying and fall through to fresh fallback.
                                break

                # Either the failure was not a transient kill (e.g. the session
                # is genuinely gone), or same-id resume retries were exhausted:
                # fall back to a fresh session so the bot keeps working.
                if response is None:
                    logger.warning(
                        "Session resume failed, starting fresh session",
                        failed_session_id=claude_session_id,
                        error=str(resume_error),
                    )
                    # Clean up the stale session
                    await self.session_manager.remove_session(session.session_id)

                    # Create a fresh session and retry
                    session = await self.session_manager.get_or_create_session(
                        user_id, working_directory
                    )
                    response = await self._execute(
                        prompt=prompt,
                        working_directory=working_directory,
                        session_id=None,
                        continue_session=False,
                        stream_callback=on_stream,
                        interrupt_event=interrupt_event,
                        images=images,
                    )

            # Update session (assigns real session_id for new sessions)
            await self.session_manager.update_session(session, response)

            # Ensure response has the session's final ID
            response.session_id = session.session_id

            if not response.session_id:
                logger.warning(
                    "No session_id after execution; session cannot be resumed",
                    user_id=user_id,
                )

            logger.info(
                "Claude command completed",
                session_id=response.session_id,
                cost=response.cost,
                duration_ms=response.duration_ms,
                num_turns=response.num_turns,
                is_error=response.is_error,
            )

            return response

        except Exception as e:
            logger.error(
                "Claude command failed",
                error=str(e),
                user_id=user_id,
                session_id=session.session_id,
            )
            raise

    async def _execute(
        self,
        prompt: str,
        working_directory: Path,
        session_id: Optional[str] = None,
        continue_session: bool = False,
        stream_callback: Optional[Callable] = None,
        interrupt_event: Optional[asyncio.Event] = None,
        images: Optional[List[Dict[str, str]]] = None,
    ) -> ClaudeResponse:
        """Execute command via SDK."""
        return await self.sdk_manager.execute_command(
            prompt=prompt,
            working_directory=working_directory,
            session_id=session_id,
            continue_session=continue_session,
            stream_callback=stream_callback,
            interrupt_event=interrupt_event,
            images=images,
        )

    async def _find_resumable_session(
        self,
        user_id: int,
        working_directory: Path,
    ) -> Optional["ClaudeSession"]:  # noqa: F821
        """Find the most recent resumable session for a user in a directory.

        Returns the session if one exists that is non-expired and has a real
        (non-temporary) session ID from Claude. Returns None otherwise.
        """

        sessions = await self.session_manager._get_user_sessions(user_id)

        matching_sessions = [
            s
            for s in sessions
            if s.project_path == working_directory
            and bool(s.session_id)
            and not s.is_expired(self.config.session_timeout_hours)
        ]

        if not matching_sessions:
            return None

        return max(matching_sessions, key=lambda s: s.last_used)

    async def continue_session(
        self,
        user_id: int,
        working_directory: Path,
        prompt: Optional[str] = None,
        on_stream: Optional[Callable[[StreamUpdate], None]] = None,
    ) -> Optional[ClaudeResponse]:
        """Continue the most recent session."""
        logger.info(
            "Continuing session",
            user_id=user_id,
            working_directory=str(working_directory),
            has_prompt=bool(prompt),
        )

        # Get user's sessions
        sessions = await self.session_manager._get_user_sessions(user_id)

        # Find most recent session in this directory (exclude sessions without IDs)
        matching_sessions = [
            s
            for s in sessions
            if s.project_path == working_directory and bool(s.session_id)
        ]

        if not matching_sessions:
            logger.info("No matching sessions found", user_id=user_id)
            return None

        # Get most recent
        latest_session = max(matching_sessions, key=lambda s: s.last_used)

        # Continue session with default prompt if none provided
        # Claude CLI requires a prompt, so we use a placeholder
        return await self.run_command(
            prompt=prompt or "Please continue where we left off",
            working_directory=working_directory,
            user_id=user_id,
            session_id=latest_session.session_id,
            on_stream=on_stream,
        )

    async def get_session_info(
        self, session_id: str, user_id: int
    ) -> Optional[Dict[str, Any]]:
        """Get session information (scoped to requesting user)."""
        return await self.session_manager.get_session_info(session_id, user_id)

    async def get_user_sessions(self, user_id: int) -> List[Dict[str, Any]]:
        """Get all sessions for a user."""
        sessions = await self.session_manager._get_user_sessions(user_id)
        return [
            {
                "session_id": s.session_id,
                "project_path": str(s.project_path),
                "created_at": s.created_at.isoformat(),
                "last_used": s.last_used.isoformat(),
                "total_cost": s.total_cost,
                "message_count": s.message_count,
                "tools_used": s.tools_used,
                "expired": s.is_expired(self.config.session_timeout_hours),
            }
            for s in sessions
        ]

    async def cleanup_expired_sessions(self) -> int:
        """Clean up expired sessions."""
        return await self.session_manager.cleanup_expired_sessions()

    async def get_user_summary(self, user_id: int) -> Dict[str, Any]:
        """Get comprehensive user summary."""
        session_summary = await self.session_manager.get_user_session_summary(user_id)

        return {
            "user_id": user_id,
            **session_summary,
        }

    async def shutdown(self) -> None:
        """Shutdown integration and cleanup resources."""
        logger.info("Shutting down Claude integration")

        await self.cleanup_expired_sessions()

        logger.info("Claude integration shutdown complete")
