from pathlib import Path

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import FileResponse

from voxops import __version__
from voxops.api.schemas import (
    AnalyzeRequest,
    HealthResponse,
    ReadyResponse,
    TranscribeRequest,
    WorktreeRequest,
)
from voxops.domain.models import AnalysisResult, VoiceTranscript, WorktreeRecord
from voxops.voice.gnani import GnaniSpeechToText
from voxops.voice.transcript_stub import TranscriptStub, validate_transcript

router = APIRouter()


@router.get("/", response_class=FileResponse, include_in_schema=False)
# Why: Make local API testing accessible without hand-authoring requests in Swagger.
# How: Serve the packaged buildless test page under the existing loopback security boundary.
def api_tester() -> FileResponse:
    return FileResponse(Path(__file__).resolve().parents[1] / "static" / "index.html")


@router.get("/health", response_model=HealthResponse)
# Why: Offer a cheap liveness probe independent of filesystem and cloud services.
# How: Return the running package version in a typed fixed-status response.
def health() -> HealthResponse:
    return HealthResponse(version=__version__)


@router.get("/ready", response_model=ReadyResponse)
# Why: Confirm deterministic analysis prerequisites without requiring model or STT credentials.
# How: Probe local storage and configured roots, using 503 only for necessary local failures.
def ready(request: Request, response: Response) -> ReadyResponse:
    storage = request.app.state.store.ready()
    workspaces = all(root.is_dir() for root in request.app.state.settings.allowed_roots)
    available = storage and workspaces
    response.status_code = 200 if available else 503
    return ReadyResponse(ready=available, storage=storage, workspaces=workspaces)


@router.post("/api/v1/repositories/analyze", response_model=AnalysisResult)
# Why: Analyze local repositories through the bounded Stage 1 graph.
# How: Validate optional text without storing it and delegate only the path/model option.
async def analyze(body: AnalyzeRequest, request: Request) -> AnalysisResult:
    if body.transcript is not None:
        validate_transcript(body.transcript)
    return await request.app.state.workflow.analyze(body.path, body.use_llm)


@router.post("/api/v1/worktrees", response_model=WorktreeRecord, status_code=201)
# Why: Make temporary committed-content isolation explicit and reviewable.
# How: Delegate source validation and branch ownership to the safe local manager.
def create_worktree(body: WorktreeRequest, request: Request) -> WorktreeRecord:
    return request.app.state.worktrees.create(body.path)


@router.get("/api/v1/worktrees", response_model=list[WorktreeRecord])
# Why: Let developers inspect the application's owned isolation workspaces.
# How: List only recorded worktrees with current Git status.
def list_worktrees(request: Request) -> list[WorktreeRecord]:
    return request.app.state.worktrees.list()


@router.get("/api/v1/worktrees/{identifier}", response_model=WorktreeRecord)
# Why: Report one worktree's source revision and live lifecycle metadata.
# How: Verify its ownership record before inspecting Git status.
def inspect_worktree(identifier: str, request: Request) -> WorktreeRecord:
    return request.app.state.worktrees.inspect(identifier)


@router.delete("/api/v1/worktrees/{identifier}", response_model=WorktreeRecord)
# Why: Require an explicit cleanup policy before discarding isolated changes.
# How: Default to clean-only removal and pass the explicit dirty-discard query flag.
def remove_worktree(
    identifier: str, request: Request, discard_changes: bool = Query(default=False)
) -> WorktreeRecord:
    return request.app.state.worktrees.remove(identifier, discard_changes)


@router.post("/api/v1/voice/transcribe", response_model=VoiceTranscript)
# Why: Demonstrate plain text STT output while reporting unavailable live integration honestly.
# How: Select the transcript stub or explicit Gnani configuration-error adapter.
async def transcribe(body: TranscribeRequest) -> VoiceTranscript:
    provider = TranscriptStub() if body.mode == "transcript" else GnaniSpeechToText()
    return await provider.transcribe(text=body.text, language=body.language)
