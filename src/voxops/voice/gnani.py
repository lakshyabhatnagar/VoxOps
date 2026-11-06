from voxops.core.errors import AppError
from voxops.domain.models import VoiceTranscript


# Represents a missing Gnani speech configuration.
# Provides a public error explaining why live transcription is unavailable.
class GnaniNotConfigured(AppError):
    # Creates the error for unavailable Gnani transcription.
    # Reports a configuration problem without contacting a service.
    def __init__(self):
        super().__init__(
            "gnani_not_configured",
            "Live Gnani requires a verified account-specific "
            "API contract and credentials; use transcript mode. See docs/decisions.md.",
            503,
        )


# Provides the Gnani speech interface while live setup is unavailable.
# Reports the missing configuration instead of claiming to transcribe audio.
class GnaniSpeechToText:
    # Handles a request for live Gnani transcription.
    # Reports the missing configuration without sending the audio.
    async def transcribe(
        self, *, audio: bytes | None = None, text: str | None = None, language: str | None = None
    ) -> VoiceTranscript:
        raise GnaniNotConfigured()
