import asyncio

import pytest

from voxops.voice.gnani import GnaniNotConfigured, GnaniSpeechToText
from voxops.voice.transcript_stub import TranscriptStub


# Why: Distinguish development transcripts from genuine live Gnani recognition.
# How: Assert exact typed stub metadata and explicit configuration failure for live audio.
def test_voice_contract():
    transcript = asyncio.run(
        TranscriptStub().transcribe(text="analyze this repository", language="en-IN")
    )
    assert transcript.text == "analyze this repository"
    assert transcript.provider == "transcript_stub" and transcript.confidence is None
    with pytest.raises(GnaniNotConfigured) as failure:
        asyncio.run(GnaniSpeechToText().transcribe(audio=b"not persisted"))
    assert failure.value.category == "gnani_not_configured"


# Why: Keep obvious spoken credentials out of text responses and downstream workflows.
# How: Reject secret-shaped text instead of echoing it through the transcript model.
def test_transcript_rejects_secret_input():
    from voxops.core.errors import AppError

    with pytest.raises(AppError) as failure:
        asyncio.run(TranscriptStub().transcribe(text="my password is sentinel"))
    assert "sentinel" not in failure.value.message
