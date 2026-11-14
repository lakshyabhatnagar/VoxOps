from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

from voxops.domain.artifact_models import SourcePatchSpec, Strategy
from voxops.domain.models import Model


# Accept GitHub source preferences without exposing a VM filesystem path.
# Values submitted for application configuration belong to the separate volatile form endpoint.
class WorkflowRequest(Model):
    repository_url: str = Field(max_length=250)
    ref: str | None = Field(default=None, max_length=200)
    strategies: dict[str, Strategy] = Field(default_factory=dict, max_length=32)
    owner: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
    name: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
    selected_paths: list[str] | None = Field(default=None, max_length=3000)
    provider: str | None = Field(default=None, max_length=50)
    patches: list[SourcePatchSpec] = Field(default_factory=list, max_length=8)
    use_llm: bool = False

    # Reject unsupported sources before reserving the VM's execution slot.
    # Import locally to keep repository acquisition separate from domain initialization.
    @field_validator("repository_url")
    @classmethod
    def github_url(cls, value):
        from voxops.repository.acquisition import github_source

        github_source(value)
        return value

    # Require a complete publication destination when the caller overrides it.
    # Otherwise both fields come from the supplied GitHub source address.
    @model_validator(mode="after")
    def complete_target(self):
        if bool(self.owner) != bool(self.name):
            raise ValueError("Supply both destination owner and repository name")
        return self


# Keep lookup, timeout and ownership metadata outside LangGraph's checkpoint payloads.
# Detailed scans, tasks, reviews and Docker evidence remain in their existing domain records.
class WorkflowRecord(Model):
    workflow_id: str = Field(default_factory=lambda: uuid4().hex)
    idempotency_key: str = Field(min_length=8, max_length=128)
    request: WorkflowRequest
    artifact_run_id: str | None = None
    repository_source_id: str | None = None
    scan_id: str | None = None
    status: Literal[
        "created",
        "running",
        "waiting_for_env",
        "waiting_for_approval",
        "interrupted",
        "failed",
        "cancelled",
        "expired",
        "ready_for_deployment",
        "rejected",
    ] = "created"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    waiting_since: datetime | None = None
    active_seconds: float = 0
    active_started_at: datetime | None = None
    error_category: str | None = None
    cleanup_complete: bool = False
    cleanup_error: str | None = None
