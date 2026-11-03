from typing import Literal

from pydantic import Field

from voxops.domain.models import Model


# Why: Keep analysis inputs narrow and avoid remote repository or execution options.
# How: Accept a bounded local path, explicit model preference and optional development text.
class AnalyzeRequest(Model):
    path: str = Field(min_length=1, max_length=4096)
    use_llm: bool = False
    transcript: str | None = Field(default=None, max_length=2000)


# Why: Make worktree creation independent from analysis and deployment.
# How: Accept only a verified local source directory path.
class WorktreeRequest(Model):
    path: str = Field(min_length=1, max_length=4096)


# Why: Expose honest development transcript mode while live transport remains unconfigured.
# How: Validate the mode and accept no speculative audio encoding or response schema.
class TranscribeRequest(Model):
    mode: Literal["transcript", "gnani"] = "transcript"
    text: str | None = Field(default=None, max_length=2000)
    language: str | None = Field(default=None, max_length=35, pattern=r"^[a-zA-Z-]+$")


# Why: Keep health responses lightweight and stable.
# How: Return a version and fixed status without checking optional external services.
class HealthResponse(Model):
    status: Literal["ok"] = "ok"
    version: str


# Why: Report local prerequisites independently of optional Anthropic and Gnani access.
# How: Expose only storage/workspace readiness and explicit live STT availability.
class ReadyResponse(Model):
    ready: bool
    storage: bool
    workspaces: bool
    gnani_live: bool = False
