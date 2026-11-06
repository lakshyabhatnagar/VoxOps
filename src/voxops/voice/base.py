from typing import Protocol

from voxops.domain.models import VoiceTranscript


# Defines the interface for speech transcription providers.
# Accepts audio or development text and returns transcription details.
class SpeechToTextProvider(Protocol):
    # Transcribes audio or returns supplied development text.
    # Provides a shared interface for live providers and local test implementations.
    async def transcribe(
        self, *, audio: bytes | None = None, text: str | None = None, language: str | None = None
    ) -> VoiceTranscript: ...
