from typing import Literal

from fastapi import APIRouter, Request
from pydantic import Field

from voxops.core.errors import AppError
from voxops.domain.artifact_models import ArtifactPlan, ArtifactRun, SourcePatchSpec, Strategy
from voxops.domain.execution_models import IncompleteAcknowledgement
from voxops.domain.models import Model

router = APIRouter(prefix="/api/v1/artifact-runs", tags=["Stage 2"])


# Describe a request to plan deployment-related files for a repository.
# Include the path, per-app strategy choices, and explicitly requested source patches.
# The service validates these choices before any generation run starts.
class PlanRequest(Model):
    path: str = Field(min_length=1, max_length=4096)
    strategies: dict[str, Strategy] = Field(max_length=32)
    provider: str | None = Field(default=None, max_length=50)
    patches: list[SourcePatchSpec] = Field(default_factory=list, max_length=8)


# Describe a request to start a previously saved plan.
# Include the target GitHub owner and repository plus selected source paths.
# The saved plan ID keeps this request tied to the choices already made during planning.
class RunRequest(Model):
    plan_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    owner: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
    selected_paths: list[str] | None = Field(default=None, max_length=3000)


# Hold the exact paths selected for the run's source review.
# An empty list means the user chose to omit all such paths.
# The service checks when this selection may still be changed.
class SourceSelection(Model):
    paths: list[str] = Field(max_length=3000)


# Carry the user's decision for one exact review.
# Include the review ID and comparison code so an old screen cannot approve newer contents.
# For incomplete verification, also carry the acknowledgement naming the unverified checks.
class ApprovalRequest(Model):
    review_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision: Literal["approve", "reject"]
    incomplete_acknowledgement: IncompleteAcknowledgement | None = None


# Carry values submitted from the current environment form.
# Include the form's comparison code so the service can reject outdated submissions.
# The service keeps actual values in memory and saves only their field states.
class EnvValuesRequest(Model):
    schema_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    values: dict[str, str] = Field(max_length=3000)


# Return the saved execution record for this run.
# Include current checks, attempts, diagnostics, and cleanup state.
# Actual secret values are held in the environment session rather than this response record.
@router.get("/{run_id}/verification")
def verification(run_id: str, request: Request):
    return request.app.state.artifacts.store.load_run(run_id).execution


# Ask the service to stop this run's execution.
# The service handles cancellation signalling and any necessary recovery.
# Return the resulting run state so the testing interface can show progress or cleanup problems.
@router.post("/{run_id}/verification/cancel")
async def cancel_verification(run_id: str, request: Request):
    return await request.app.state.artifacts.cancel_execution(run_id)


# Return the current environment form for this candidate.
# The service refreshes local-file and session state before returning it.
# The response describes names and resolution states rather than exposing stored secret values.
@router.get("/{run_id}/verification/env-form")
def environment_form(run_id: str, request: Request):
    return request.app.state.artifacts.environment_form(run_id)


# Pass the submitted values and form comparison code to the service.
# Let the environment manager validate the complete submission before accepting it.
# Return refreshed form state so the interface can continue verification with valid configuration.
@router.post("/{run_id}/verification/env-values")
def submit_environment(run_id: str, body: EnvValuesRequest, request: Request):
    return request.app.state.artifacts.submit_environment(run_id, body.schema_digest, body.values)


# Ask the service to analyze the repository and plan the requested files.
# Forward the app strategies, provider choice, and explicitly approved patch specifications.
# Return the plan for inspection before starting a generation run.
@router.post("/plan", response_model=ArtifactPlan)
async def plan_artifacts(body: PlanRequest, request: Request):
    return await request.app.state.artifacts.plan(
        body.path, body.strategies, body.provider, body.patches
    )


# Start a saved plan for the selected GitHub repository.
# Forward the source-path selection to the service's baseline review process.
# Return the run state so the client can see which review or execution action is next.
@router.post("", response_model=ArtifactRun, status_code=201)
async def start_run(body: RunRequest, request: Request):
    return await request.app.state.artifacts.start(
        body.plan_id, body.owner, body.name, body.selected_paths
    )


# Return the run's current saved progress and results.
# Remove review diff text from this general status response.
# The dedicated review endpoint supplies the full diff when the user wants to inspect changes.
@router.get("/{run_id}", response_model=ArtifactRun)
def inspect_run(run_id: str, request: Request):
    run = request.app.state.artifacts.store.load_run(run_id)
    for review in run.reviews.values():
        review.diff = ""
    return run


# Pass the chosen source paths to the service.
# The service checks that selection is still open and prepares the appropriate review.
# Return the updated run rather than writing source files directly in this route.
@router.post("/{run_id}/source-selection", response_model=ArtifactRun)
async def select_source(run_id: str, body: SourceSelection, request: Request):
    return await request.app.state.artifacts.select_source(run_id, body.paths)


# Return the most recently created review and the run's saved decisions.
# Reject the request when no review has been prepared yet.
# The returned diff and comparison code are the exact contents the user is being asked to approve.
@router.get("/{run_id}/review")
def inspect_review(run_id: str, request: Request):
    run = request.app.state.artifacts.store.load_run(run_id)
    if not run.reviews:
        raise AppError("review_unavailable", "This run has no review yet.", 409)
    review = max(run.reviews.values(), key=lambda item: item.created_at)
    return {"review": review, "approvals": run.approvals}


# Send the decision, review identity, and optional incomplete acknowledgement to the service.
# The existing approval code verifies that they match the current review.
# Return the updated run after the decision is recorded.
@router.post("/{run_id}/approvals", response_model=ArtifactRun)
def approve(run_id: str, body: ApprovalRequest, request: Request):
    return request.app.state.artifacts.approve(
        run_id, body.review_id, body.digest, body.decision, body.incomplete_acknowledgement
    )


# Return the run's current status, errors, and saved publication result.
# The result can still be absent while generation, verification, or approval is pending.
# Also state explicitly that this workflow has not executed a deployment.
@router.get("/{run_id}/result")
def result(run_id: str, request: Request):
    run = request.app.state.artifacts.store.load_run(run_id)
    return {
        "run_id": run.run_id,
        "status": run.status,
        "result": run.result,
        "deployment_executed": False,
        "errors": run.errors,
    }
