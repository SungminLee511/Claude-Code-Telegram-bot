"""Input validation and security checks.

Features:
- Path traversal prevention
- Command injection prevention
- File type validation
- Input sanitization
"""

import re
from pathlib import Path
from typing import Optional, Tuple

import structlog

# from src.exceptions import SecurityError  # Future use

logger = structlog.get_logger()


class SecurityValidator:
    """Security validation for user inputs."""

    # Dangerous patterns for path traversal and injection
    DANGEROUS_PATTERNS = [
        r"\.\.",  # Parent directory
        r"~",  # Home directory expansion
        r"\$\{",  # Variable expansion ${...}
        r"\$\(",  # Command substitution $(...)
        r"\$[A-Za-z_]",  # Environment variable expansion $VAR
        r"`",  # Command substitution with backticks
        r";",  # Command chaining
        r"&&",  # Command chaining (AND)
        r"\|\|",  # Command chaining (OR)
        r">",  # Output redirection
        r"<",  # Input redirection
        r"\|(?!\|)",  # Piping (but not ||)
        r"&(?!&)",  # Background execution (but not &&)
        r"#.*",  # Comments (potential for injection)
        r"\x00",  # Null byte
    ]

    # Allowed file extensions for uploads
    ALLOWED_EXTENSIONS = {
        ".py",
        ".js",
        ".ts",
        ".jsx",
        ".tsx",
        ".java",
        ".cpp",
        ".c",
        ".h",
        ".hpp",
        ".cs",
        ".go",
        ".rs",
        ".rb",
        ".php",
        ".swift",
        ".kt",
        ".md",
        ".txt",
        ".json",
        ".yml",
        ".yaml",
        ".toml",
        ".xml",
        ".html",
        ".css",
        ".scss",
        ".less",
        ".sql",
        ".sh",
        ".bash",
        ".zsh",
        ".fish",
        ".ps1",
        ".bat",
        ".cmd",
        ".r",
        ".scala",
        ".clj",
        ".hs",
        ".elm",
        ".vue",
        ".svelte",
        ".lock",
    }

    # Forbidden filenames and patterns
    FORBIDDEN_FILENAMES = {
        ".env",
        ".env.local",
        ".env.production",
        ".env.development",
        ".ssh",
        ".aws",
        ".docker",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "shadow",
        "passwd",
        "hosts",
        "sudoers",
        ".bash_history",
        ".zsh_history",
        ".mysql_history",
        ".psql_history",
    }

    # Dangerous file patterns
    DANGEROUS_FILE_PATTERNS = [
        r".*\.key$",  # Key files
        r".*\.pem$",  # Certificate files
        r".*\.p12$",  # Certificate files
        r".*\.pfx$",  # Certificate files
        r".*\.crt$",  # Certificate files
        r".*\.cer$",  # Certificate files
        r".*_rsa$",  # SSH keys
        r".*_dsa$",  # SSH keys
        r".*_ecdsa$",  # SSH keys
        r".*\.exe$",  # Executables
        r".*\.dll$",  # Windows libraries
        r".*\.so$",  # Shared objects
        r".*\.dylib$",  # macOS libraries
        r".*\.bat$",  # Batch files
        r".*\.cmd$",  # Command files
        r".*\.msi$",  # Installers
        r".*\.rar$",  # Archives (potentially dangerous)
    ]

    def __init__(
        self, approved_directory: Path, disable_security_patterns: bool = False
    ):
        """Initialize validator with approved directory."""
        self.approved_directory = approved_directory.resolve()
        self.disable_security_patterns = disable_security_patterns
        logger.info(
            "Security validator initialized",
            approved_directory=str(self.approved_directory),
            disable_security_patterns=self.disable_security_patterns,
        )

    def validate_path(
        self, user_path: str, current_dir: Optional[Path] = None
    ) -> Tuple[bool, Optional[Path], Optional[str]]:
        """Validate and resolve user-provided path.

        Returns:
            Tuple of (is_valid, resolved_path, error_message)
        """
        try:
            # Basic input validation
            if not user_path or not user_path.strip():
                return False, None, "Empty path not allowed"

            user_path = user_path.strip()

            # Check for dangerous patterns (unless explicitly disabled)
            if not self.disable_security_patterns:
                for pattern in self.DANGEROUS_PATTERNS:
                    if re.search(pattern, user_path, re.IGNORECASE):
                        logger.warning(
                            "Dangerous pattern detected in path",
                            path=user_path,
                            pattern=pattern,
                        )
                        return (
                            False,
                            None,
                            f"Invalid path: contains forbidden pattern '{pattern}'",
                        )

            # Handle path resolution
            current_dir = current_dir or self.approved_directory

            if user_path.startswith("/"):
                # Absolute path - use as-is
                target = Path(user_path)
            else:
                # Relative path
                target = current_dir / user_path

            # Resolve path and check boundaries
            target = target.resolve()

            # Ensure target is within approved directory
            if not self._is_within_directory(target, self.approved_directory):
                logger.warning(
                    "Path traversal attempt detected",
                    requested_path=user_path,
                    resolved_path=str(target),
                    approved_directory=str(self.approved_directory),
                )
                return False, None, "Access denied: path outside approved directory"

            logger.debug(
                "Path validation successful",
                original_path=user_path,
                resolved_path=str(target),
            )
            return True, target, None

        except Exception as e:
            logger.error("Path validation error", path=user_path, error=str(e))
            return False, None, f"Invalid path: {str(e)}"

    def _is_within_directory(self, path: Path, directory: Path) -> bool:
        """Check if path is within directory."""
        try:
            path.relative_to(directory)
            return True
        except ValueError:
            return False

    def validate_filename(self, filename: str) -> Tuple[bool, Optional[str]]:
        """Validate uploaded filename.

        Returns:
            Tuple of (is_valid, error_message)
        """
        # Basic checks
        if not filename or not filename.strip():
            return False, "Empty filename not allowed"

        filename = filename.strip()

        # Check for path separators in filename
        if "/" in filename or "\\" in filename:
            logger.warning("Path separator in filename", filename=filename)
            return False, "Invalid filename: contains path separators"

        # Check for forbidden patterns
        for pattern in self.DANGEROUS_PATTERNS:
            if re.search(pattern, filename, re.IGNORECASE):
                logger.warning(
                    "Dangerous pattern in filename", filename=filename, pattern=pattern
                )
                return False, "Invalid filename: contains forbidden pattern"

        # Check for forbidden filenames
        if filename.lower() in {name.lower() for name in self.FORBIDDEN_FILENAMES}:
            logger.warning("Forbidden filename", filename=filename)
            return False, f"Forbidden filename: {filename}"

        # Check for dangerous file patterns
        for pattern in self.DANGEROUS_FILE_PATTERNS:
            if re.match(pattern, filename, re.IGNORECASE):
                logger.warning(
                    "Dangerous file pattern", filename=filename, pattern=pattern
                )
                return False, f"File type not allowed: {filename}"

        # Check extension
        path_obj = Path(filename)
        ext = path_obj.suffix.lower()

        if ext and ext not in self.ALLOWED_EXTENSIONS:
            logger.warning(
                "File extension not allowed", filename=filename, extension=ext
            )
            return False, f"File type not allowed: {ext}"

        # Check for hidden files (starting with .)
        if filename.startswith(".") and filename not in {".gitignore", ".gitkeep"}:
            logger.warning("Hidden file upload attempt", filename=filename)
            return False, "Hidden files not allowed"

        # Check filename length
        if len(filename) > 255:
            return False, "Filename too long (max 255 characters)"

        logger.debug("Filename validation successful", filename=filename)
        return True, None

