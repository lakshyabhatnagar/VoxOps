import re
from uuid import uuid4

from voxops.core.errors import AppError
from voxops.domain.models import VoiceTranscript


# Why: Prevent the development transcript path from becoming a credential-entry workflow.
# How: Reject obvious secret assignments and key/token formats before returning text.
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


# Why: Provide an honest executable development mode without claiming live speech recognition.
# How: Validate supplied text and label every result transcript_stub with no synthetic confidence.
class TranscriptStub:
    # Why: Demonstrate the ordinary-text boundary without network or audio persistence.
    # How: Reject audio and pass validated text into the shared transcript model.
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
