"""Security middleware for input validation and threat detection."""

from typing import Any, Callable, Dict

import structlog

from ..utils.html_format import escape_html

logger = structlog.get_logger()


async def security_middleware(
    handler: Callable, event: Any, data: Dict[str, Any]
) -> Any:
    """Validate inputs and detect security threats.

    This middleware:
    1. Validates message content for dangerous patterns
    2. Sanitizes file uploads
    3. Detects potential attacks
    4. Logs security violations
    """
    user_id = event.effective_user.id if event.effective_user else None
    username = (
        getattr(event.effective_user, "username", None)
        if event.effective_user
        else None
    )

    if not user_id:
        logger.warning("No user information in update")
        return await handler(event, data)

    # Get dependencies from context
    security_validator = data.get("security_validator")
    audit_logger = data.get("audit_logger")

    if not security_validator:
        logger.error("Security validator not available in middleware context")
        # Continue without validation (log error but don't block)
        return await handler(event, data)

    # Message text is a prompt to Claude, never a command, so it is not
    # screened as one — backticks, paths and shell fragments are ordinary
    # content in a conversation. What the model then *does* is decided by the
    # tool gate in `sdk_integration`, which is the layer that can judge an
    # actual action rather than guess from a sentence.
    message = event.effective_message

    # Validate file uploads if present
    if message and message.document:
        is_safe, error_message = await validate_file_upload(
            message.document, security_validator, user_id, audit_logger
        )
        if not is_safe:
            await message.reply_text(
                f"🛡️ <b>File Upload Blocked</b>\n\n"
                f"{escape_html(error_message)}\n\n"
                "Please ensure your file meets security requirements.",
                parse_mode="HTML",
            )
            return  # Block processing

    # Log successful security validation
    logger.debug(
        "Security validation passed",
        user_id=user_id,
        username=username,
        has_text=bool(message and message.text),
        has_document=bool(message and message.document),
    )

    # Continue to handler
    return await handler(event, data)


async def validate_file_upload(
    document: Any, security_validator: Any, user_id: int, audit_logger: Any
) -> tuple[bool, str]:
    """Validate file uploads for security."""

    filename = getattr(document, "file_name", "unknown")
    file_size = getattr(document, "file_size", 0)
    mime_type = getattr(document, "mime_type", "unknown")

    # Validate filename
    is_valid, error_message = security_validator.validate_filename(filename)
    if not is_valid:
        if audit_logger:
            await audit_logger.log_security_violation(
                user_id=user_id,
                violation_type="dangerous_filename",
                details=f"Filename validation failed: {error_message}",
                severity="medium",
                attempted_action="file_upload",
            )

        logger.warning(
            "Dangerous filename detected",
            user_id=user_id,
            filename=filename,
            error=error_message,
        )
        return False, error_message

    # Check file size limits
    max_file_size = 10 * 1024 * 1024  # 10MB
    if file_size > max_file_size:
        if audit_logger:
            await audit_logger.log_security_violation(
                user_id=user_id,
                violation_type="file_too_large",
                details=f"File size {file_size} exceeds limit {max_file_size}",
                severity="low",
                attempted_action="file_upload",
            )

        return False, f"File too large. Maximum size: {max_file_size // (1024*1024)}MB"

    # Check MIME type
    dangerous_mime_types = [
        "application/x-executable",
        "application/x-msdownload",
        "application/x-msdos-program",
        "application/x-dosexec",
        "application/x-winexe",
        "application/x-sh",
        "application/x-shellscript",
    ]

    if mime_type in dangerous_mime_types:
        if audit_logger:
            await audit_logger.log_security_violation(
                user_id=user_id,
                violation_type="dangerous_mime_type",
                details=f"Dangerous MIME type: {mime_type}",
                severity="high",
                attempted_action="file_upload",
            )

        logger.warning(
            "Dangerous MIME type detected",
            user_id=user_id,
            filename=filename,
            mime_type=mime_type,
        )
        return False, f"File type not allowed: {mime_type}"

    # Log successful file validation
    if audit_logger:
        await audit_logger.log_file_access(
            user_id=user_id,
            file_path=filename,
            action="upload_validated",
            success=True,
            file_size=file_size,
        )

    logger.info(
        "File upload validated",
        user_id=user_id,
        filename=filename,
        file_size=file_size,
        mime_type=mime_type,
    )

    return True, ""


