import asyncio

import pytest

from voxops.voice.gnani import GnaniNotConfigured, GnaniSpeechToText
from voxops.voice.transcript_stub import TranscriptStub


# Request a sample transcript and try the live voice service without configuration.
# Check the transcript details and the error for the unavailable service.
def test_voice_contract():
    transcript = asyncio.run(
        TranscriptStub().transcribe(text="analyze this repository", language="en-IN")
    )
    assert transcript.text == "analyze this repository"
    assert transcript.provider == "transcript_stub" and transcript.confidence is None
    with pytest.raises(GnaniNotConfigured) as failure:
        asyncio.run(GnaniSpeechToText().transcribe(audio=b"not persisted"))
    assert failure.value.category == "gnani_not_configured"


# Send text that looks like a credential to the transcript service.
# Check that the service rejects it rather than returning the secret.
def test_transcript_rejects_secret_input():
    from voxops.core.errors import AppError

    with pytest.raises(AppError) as failure:
        asyncio.run(TranscriptStub().transcribe(text="my password is sentinel"))
    assert "sentinel" not in failure.value.message
