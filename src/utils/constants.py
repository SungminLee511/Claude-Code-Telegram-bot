"""Application-wide constants."""

# Version info

# Default limits
DEFAULT_CLAUDE_TIMEOUT_SECONDS = 7200
DEFAULT_CLAUDE_MAX_TURNS = 10
DEFAULT_CLAUDE_MAX_COST_PER_USER = 10.0
DEFAULT_CLAUDE_MAX_COST_PER_REQUEST = 5.0

DEFAULT_RATE_LIMIT_REQUESTS = 10
DEFAULT_RATE_LIMIT_WINDOW = 60
DEFAULT_RATE_LIMIT_BURST = 20

DEFAULT_SESSION_TIMEOUT_HOURS = 24
DEFAULT_MAX_SESSIONS_PER_USER = 5

# Message limits
TELEGRAM_MAX_MESSAGE_LENGTH = 4096

# Session limits

# Allowed file extensions

# Security patterns to block
DANGEROUS_PATTERNS = [
    r"\.\.",  # Parent directory
    r"~",  # Home directory
    r"\$",  # Variable expansion
    r"`",  # Command substitution
    r";",  # Command chaining
    r"&&",  # Command chaining
    r"\|\|",  # Command chaining
    r">",  # Redirection
    r"<",  # Redirection
    r"\|",  # Piping
]

# Database defaults
DEFAULT_DATABASE_URL = "sqlite:///data/bot.db"

# Claude Code defaults

# Retry defaults
DEFAULT_RETRY_MAX_ATTEMPTS = 3
DEFAULT_RETRY_BASE_DELAY = 1.0
DEFAULT_RETRY_BACKOFF_FACTOR = 3.0
DEFAULT_RETRY_MAX_DELAY = 30.0

# Logging
