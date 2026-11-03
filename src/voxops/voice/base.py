from typing import Protocol

from voxops.domain.models import VoiceTranscript


# Why: Separate speech transport from text-based analysis orchestration.
# How: Define a replaceable asynchronous transcription interface without retaining audio.
class SpeechToTextProvider(Protocol):
    # Why: Allow real transport adapters and explicit local fakes to share typed output.
    # How: Accept optional in-memory audio or transcript and return plain text metadata.
    async def transcribe(
        self, *, audio: bytes | None = None, text: str | None = None, language: str | None = None
    ) -> VoiceTranscript: ...
