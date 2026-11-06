import re
from uuid import uuid4

from voxops.core.errors import AppError
from voxops.domain.models import VoiceTranscript


# Checks development transcript text before returning it.
# Rejects obvious secret assignments and key or token patterns.
def validate_transcript(text: str | None) -> str:
    if not text or not text.strip() or len(text) > 2000:
        raise AppError(
            "invalid_transcript", "Provide a nonempty transcript of at most 2000 characters.", 422
        )
    if re.search(
        r"(?:password|secret|token|api[_ -]?key)\s*(?:=|:|is\b)|"
        r"\b(?:sk-|ghp_|AKIA)[A-Za-z0-9_-]{8,}|-----BEGIN",
        text,
        re.I,
    ):
        raise AppError("secret_input_forbidden", "Do not submit credentials in transcripts.", 422)
    return text.strip()


# Provides text input for development without speech recognition.
# Checks the supplied transcript and labels the result as a stub.
class TranscriptStub:
    # Returns a supplied development transcript.
    # Rejects audio and validates the text before creating the result.
    async def transcribe(
        self, *, audio: bytes | None = None, text: str | None = None, language: str | None = None
    ) -> VoiceTranscript:
        if audio is not None:
            raise AppError(
                "audio_unsupported", "Transcript development mode accepts text only.", 422
            )
        return VoiceTranscript(
            text=validate_transcript(text),
            language=language,
            provider="transcript_stub",
            request_id=uuid4().hex,
        )
