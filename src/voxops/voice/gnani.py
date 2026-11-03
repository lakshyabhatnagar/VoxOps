from voxops.core.errors import AppError
from voxops.domain.models import VoiceTranscript


# Why: Distinguish missing verified Gnani setup from transient transport or recognition failure.
# How: Preserve an explicit public configuration category and actionable local explanation.
class GnaniNotConfigured(AppError):
    # Why: Keep unsupported live STT from being reported as successful transcription.
    # How: Return a service-unavailable configuration error without contacting guessed endpoints.
    def __init__(self):
        super().__init__(
            "gnani_not_configured",
            "Live Gnani requires a verified account-specific "
            "API contract and credentials; use transcript mode. See docs/decisions.md.",
            503,
        )


# Why: Reserve a truthful Gnani transport boundary until its contract can be verified live.
# How: Implement the provider protocol with an explicit not-configured result for all live calls.
class GnaniSpeechToText:
    # Why: Avoid inventing endpoints, payload fields or WebSocket messages.
    # How: Refuse live requests before reading or transmitting any audio.
    async def transcribe(
        self, *, audio: bytes | None = None, text: str | None = None, language: str | None = None
    ) -> VoiceTranscript:
        raise GnaniNotConfigured()
