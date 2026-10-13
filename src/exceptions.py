"""Custom exceptions for Claude Code Telegram Bot."""


class ClaudeCodeTelegramError(Exception):
    """Base exception for Claude Code Telegram Bot."""


class ConfigurationError(ClaudeCodeTelegramError):
    """Configuration-related errors."""


class InvalidConfigError(ConfigurationError):
    """Configuration is invalid."""


class SecurityError(ClaudeCodeTelegramError):
    """Security-related errors."""


class AuthenticationError(SecurityError):
    """Authentication failed."""


class ClaudeError(ClaudeCodeTelegramError):
    """Claude Code-related errors."""


class ClaudeTimeoutError(ClaudeError):
    """Claude Code operation timed out."""


class ClaudeProcessError(ClaudeError):
    """Claude Code process execution failed."""


class ClaudeParsingError(ClaudeError):
    """Failed to parse Claude Code output."""


class TelegramError(ClaudeCodeTelegramError):
    """Telegram API-related errors."""


class RateLimitError(TelegramError):
    """Rate limit exceeded."""


class RateLimitExceeded(RateLimitError):
    """Rate limit exceeded (alias for compatibility)."""
