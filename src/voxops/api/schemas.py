from typing import Literal

from pydantic import Field

from voxops.domain.models import Model


# Holds the inputs for analyzing a local project.
# Includes its path, model preference and optional development text.
class AnalyzeRequest(Model):
    path: str = Field(min_length=1, max_length=4096)
    use_llm: bool = False
    transcript: str | None = Field(default=None, max_length=2000)


# Holds the source directory for a new working copy.
# Accepts a local path for the worktree manager to check.
class WorktreeRequest(Model):
    path: str = Field(min_length=1, max_length=4096)


# Holds the requested transcription mode and development text.
# Checks the mode without assuming a live audio format.
class TranscribeRequest(Model):
    mode: Literal["transcript", "gnani"] = "transcript"
    text: str | None = Field(default=None, max_length=2000)
    language: str | None = Field(default=None, max_length=35, pattern=r"^[a-zA-Z-]+$")


# Holds the application's basic health response.
# Returns its version and a fixed status.
class HealthResponse(Model):
    status: Literal["ok"] = "ok"
    version: str


# Holds the results of local readiness checks.
# Reports storage, workspace and live speech availability separately.
class ReadyResponse(Model):
    ready: bool
    storage: bool
    workspaces: bool
    gnani_live: bool = False
