from typing import Literal

from fastapi import APIRouter, Request
from pydantic import Field

from voxops.core.errors import AppError
from voxops.domain.artifact_models import ArtifactPlan, ArtifactRun, SourcePatchSpec, Strategy
from voxops.domain.models import Model

router = APIRouter(prefix="/api/v1/artifact-runs", tags=["Stage 2"])


# Holds the choices needed to plan generated files.
# Includes the source directory, per-unit strategies and an optional provider.
class PlanRequest(Model):
    path: str = Field(min_length=1, max_length=4096)
    strategies: dict[str, Strategy] = Field(max_length=32)
    provider: str | None = Field(default=None, max_length=50)
    patches: list[SourcePatchSpec] = Field(default_factory=list, max_length=8)


# Holds the information needed to start a saved plan.
# Includes the selected source and repository details for the run.
class RunRequest(Model):
    plan_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    owner: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
    selected_paths: list[str] | None = Field(default=None, max_length=3000)


# Holds the exact source files selected for review.
# An empty list records the choice to omit all files.
class SourceSelection(Model):
    paths: list[str] = Field(max_length=3000)


# Holds a decision about a particular review.
# Includes the review ID and displayed checksum so the decision refers to those contents.
class ApprovalRequest(Model):
    review_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision: Literal["approve", "reject"]


@router.post("/plan", response_model=ArtifactPlan)
# Creates a plan for files that may be generated.
# Scans the source and returns ownership and unresolved questions for inspection.
async def plan_artifacts(body: PlanRequest, request: Request):
    return await request.app.state.artifacts.plan(
        body.path, body.strategies, body.provider, body.patches
    )


@router.post("", response_model=ArtifactRun, status_code=201)
# Starts a saved generation plan.
# Creates a run and returns its current review status.
async def start_run(body: RunRequest, request: Request):
    return await request.app.state.artifacts.start(
        body.plan_id, body.owner, body.name, body.selected_paths
    )


@router.get("/{run_id}", response_model=ArtifactRun)
# Returns the current state of a generation run.
# Includes progress and validation results while the review endpoint supplies the diff.
def inspect_run(run_id: str, request: Request):
    run = request.app.state.artifacts.store.load_run(run_id)
    for review in run.reviews.values():
        review.diff = ""
    return run


@router.post("/{run_id}/source-selection", response_model=ArtifactRun)
# Records which changed source files should be reviewed.
# Creates a review of the selected files without committing them.
async def select_source(run_id: str, body: SourceSelection, request: Request):
    return await request.app.state.artifacts.select_source(run_id, body.paths)


@router.post("/{run_id}/execute", response_model=ArtifactRun)
# Continues a run after the required source review.
# Runs the generation tasks and saves their checked results.
async def execute_run(run_id: str, request: Request):
    return await request.app.state.artifacts.execute(run_id)


@router.get("/{run_id}/review")
# Returns the files and changes awaiting a decision.
# Includes the target, checksums and previous decisions for the run.
def inspect_review(run_id: str, request: Request):
    run = request.app.state.artifacts.store.load_run(run_id)
    if not run.reviews:
        raise AppError("review_unavailable", "This run has no review yet.", 409)
    review = max(run.reviews.values(), key=lambda item: item.created_at)
    return {"review": review, "approvals": run.approvals}


@router.post("/{run_id}/approvals", response_model=ArtifactRun)
# Records a decision on the displayed review.
# Checks the source and review checksum before saving approval or rejection.
def approve(run_id: str, body: ApprovalRequest, request: Request):
    return request.app.state.artifacts.approve(run_id, body.review_id, body.digest, body.decision)


@router.post("/{run_id}/publish", response_model=ArtifactRun)
# Publishes an approved set of changes to GitHub.
# Checks the approved branch and verifies the resulting remote commit.
async def publish(run_id: str, request: Request):
    return await request.app.state.artifacts.publish(run_id)


@router.get("/{run_id}/result")
# Returns the repository revision produced by a completed run.
# Waits until the remote commit has been verified before providing the handoff.
def result(run_id: str, request: Request):
    run = request.app.state.artifacts.store.load_run(run_id)
    return {
        "run_id": run.run_id,
        "status": run.status,
        "result": run.result,
        "deployment_executed": False,
        "errors": run.errors,
    }
