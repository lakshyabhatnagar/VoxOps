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
# Serves the local page for trying the API.
# Returns the packaged page through the local service.
def api_tester() -> FileResponse:
    return FileResponse(Path(__file__).resolve().parents[1] / "static" / "index.html")


@router.get("/health", response_model=HealthResponse)
# Reports whether the application is running.
# Returns a fixed status and the installed version.
def health() -> HealthResponse:
    return HealthResponse(version=__version__)


@router.get("/ready", response_model=ReadyResponse)
# Checks whether the application can perform local analysis.
# Tests storage and workspace access without requiring optional service credentials.
def ready(request: Request, response: Response) -> ReadyResponse:
    storage = request.app.state.store.ready()
    workspaces = all(root.is_dir() for root in request.app.state.settings.allowed_roots)
    available = storage and workspaces
    response.status_code = 200 if available else 503
    return ReadyResponse(ready=available, storage=storage, workspaces=workspaces)


@router.post("/api/v1/repositories/analyze", response_model=AnalysisResult)
# Analyzes a local project directory.
# Checks the request and passes the directory and model choice to the analysis service.
async def analyze(body: AnalyzeRequest, request: Request) -> AnalysisResult:
    if body.transcript is not None:
        validate_transcript(body.transcript)
    return await request.app.state.workflow.analyze(body.path, body.use_llm)


@router.get("/api/v1/scans/{scan_id}", response_model=AnalysisResult)
# Read a saved analysis so workflow polling need not return the entire scan every time.
# Source paths are represented by existing safe relative evidence and repository fingerprints.
def inspect_scan(scan_id: str, request: Request) -> AnalysisResult:
    return request.app.state.store.load(scan_id)


@router.post("/api/v1/worktrees", response_model=WorktreeRecord, status_code=201)
# Creates a separate working copy of committed project files.
# Checks the source and assigns a branch through the worktree manager.
def create_worktree(body: WorktreeRequest, request: Request) -> WorktreeRecord:
    return request.app.state.worktrees.create(body.path)


@router.get("/api/v1/worktrees", response_model=list[WorktreeRecord])
# Lists the working copies managed by this application.
# Returns their recorded details and current Git status.
def list_worktrees(request: Request) -> list[WorktreeRecord]:
    return request.app.state.worktrees.list()


@router.get("/api/v1/worktrees/{identifier}", response_model=WorktreeRecord)
# Returns the details of one managed working copy.
# Checks its ownership record before reading its Git status.
def inspect_worktree(identifier: str, request: Request) -> WorktreeRecord:
    return request.app.state.worktrees.inspect(identifier)


@router.delete("/api/v1/worktrees/{identifier}", response_model=WorktreeRecord)
# Removes a working copy managed by this application.
# Requires an explicit choice before discarding uncommitted changes.
def remove_worktree(
    identifier: str, request: Request, discard_changes: bool = Query(default=False)
) -> WorktreeRecord:
    return request.app.state.worktrees.remove(identifier, discard_changes)


@router.post("/api/v1/voice/transcribe", response_model=VoiceTranscript)
# Returns text using the requested speech provider.
# Uses the supplied transcript in development mode or reports missing Gnani setup.
async def transcribe(body: TranscribeRequest) -> VoiceTranscript:
    provider = TranscriptStub() if body.mode == "transcript" else GnaniSpeechToText()
    return await provider.transcribe(text=body.text, language=body.language)
