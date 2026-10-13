"""Telegram bot authentication middleware."""

from typing import Any, Callable, Dict

import structlog

logger = structlog.get_logger()


async def auth_middleware(handler: Callable, event: Any, data: Dict[str, Any]) -> Any:
    """Check authentication before processing messages.

    This middleware:
    1. Checks if user is authenticated
    2. Attempts authentication if not authenticated
    3. Updates session activity
    4. Logs authentication events
    """
    # Extract user information
    user_id = event.effective_user.id if event.effective_user else None
    username = (
        getattr(event.effective_user, "username", None)
        if event.effective_user
        else None
    )

    if not user_id:
        logger.warning("No user information in update")
        return

    # Get dependencies from context
    auth_manager = data.get("auth_manager")
    audit_logger = data.get("audit_logger")

    if not auth_manager:
        logger.error("Authentication manager not available in middleware context")
        if event.effective_message:
            await event.effective_message.reply_text(
                "🔒 Authentication system unavailable. Please try again later."
            )
        return

    # Check if user is already authenticated
    if auth_manager.is_authenticated(user_id):
        # Update session activity
        if auth_manager.refresh_session(user_id):
            session = auth_manager.get_session(user_id)
            logger.debug(
                "Session refreshed",
                user_id=user_id,
                username=username,
                auth_provider=session.auth_provider if session else None,
            )

        # Continue to handler
        return await handler(event, data)

    # User not authenticated - attempt authentication
    logger.info(
        "Attempting authentication for user", user_id=user_id, username=username
    )

    # Try to authenticate (providers will check whitelist and tokens)
    authentication_successful = await auth_manager.authenticate_user(user_id)

    # Log authentication attempt
    if audit_logger:
        await audit_logger.log_auth_attempt(
            user_id=user_id,
            success=authentication_successful,
            method="automatic",
            reason="message_received",
        )

    if authentication_successful:
        session = auth_manager.get_session(user_id)
        logger.info(
            "User authenticated successfully",
            user_id=user_id,
            username=username,
            auth_provider=session.auth_provider if session else None,
        )

        # Deliberately silent. Auth sessions live in memory only, so every bot
        # restart (and the 24h idle timeout) re-authenticates — a greeting here
        # fires on the first message after each restart, which is noise for a
        # whitelist bot whose user is always authorized. The log line above is
        # the record; only *failed* auth is worth telling the chat about.

        # Continue to handler
        return await handler(event, data)

    else:
        # Authentication failed
        logger.warning("Authentication failed", user_id=user_id, username=username)

        if event.effective_message:
            await event.effective_message.reply_text(
                "🔒 <b>Authentication Required</b>\n\n"
                "You are not authorized to use this bot.\n"
                "Please contact the administrator for access.\n\n"
                f"Your Telegram ID: <code>{user_id}</code>\n"
                "Share this ID with the administrator to request access.",
                parse_mode="HTML",
            )
        return  # Stop processing


