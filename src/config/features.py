"""Feature flag management."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .settings import Settings


class FeatureFlags:
    """Feature flag management system."""

    def __init__(self, settings: "Settings"):
        """Initialize with settings."""
        self.settings = settings

    @property
    def mcp_enabled(self) -> bool:
        """Check if Model Context Protocol is enabled."""
        return self.settings.enable_mcp and self.settings.mcp_config_path is not None

    @property
    def file_uploads_enabled(self) -> bool:
        """Check if file uploads are enabled."""
        return self.settings.enable_file_uploads

    @property
    def token_auth_enabled(self) -> bool:
        """Check if token-based authentication is enabled."""
        return (
            self.settings.enable_token_auth
            and self.settings.auth_token_secret is not None
        )

    @property
    def webhook_enabled(self) -> bool:
        """Check if webhook mode is enabled."""
        return self.settings.webhook_url is not None

    @property
    def development_features_enabled(self) -> bool:
        """Check if development features are enabled."""
        return self.settings.development_mode

    @property
    def voice_messages_enabled(self) -> bool:
        """Check if voice message transcription is enabled."""
        if not self.settings.enable_voice_messages:
            return False
        if self.settings.voice_provider == "local":
            return True  # No API key needed for local whisper.cpp
        if self.settings.voice_provider == "openai":
            return self.settings.openai_api_key is not None
        return self.settings.mistral_api_key is not None

    @property
    def stream_drafts_enabled(self) -> bool:
        """Check if streaming drafts via sendMessageDraft is enabled."""
        return self.settings.enable_stream_drafts

    def get_enabled_features(self) -> list[str]:
        """Get list of all enabled features."""
        features = []
        if self.mcp_enabled:
            features.append("mcp")
        if self.file_uploads_enabled:
            features.append("file_uploads")
        if self.token_auth_enabled:
            features.append("token_auth")
        if self.webhook_enabled:
            features.append("webhook")
        if self.development_features_enabled:
            features.append("development")
        if self.voice_messages_enabled:
            features.append("voice_messages")
        if self.stream_drafts_enabled:
            features.append("stream_drafts")
        return features
