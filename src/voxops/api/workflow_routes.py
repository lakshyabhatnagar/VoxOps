import asyncio

from fastapi import APIRouter, Header, Request
from pydantic import Field

from voxops.domain.models import Model
from voxops.domain.workflow_models import WorkflowRequest

router = APIRouter(prefix="/api/v1/workflows", tags=["Workflows"])


# Accept only safe form acknowledgement or an exact review decision for a paused workflow.
# Environment values use the existing dedicated submission endpoint instead of this request.
class ResumeRequest(Model):
    schema_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    review_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    decision: str | None = Field(default=None, pattern=r"^(approve|reject)$")


# Create a background workflow and return its identity without waiting for a model or Docker.
# The client key prevents an accidentally repeated submission from starting another run.
@router.post("", status_code=202)
async def create_workflow(
    body: WorkflowRequest,
    request: Request,
    idempotency_key: str = Header(min_length=8, max_length=128),
):
    record = await request.app.state.workflows.create(body, idempotency_key)
    return {"workflow_id": record.workflow_id, "status": record.status}


# Read a concise projection and derive the current graph step from the actual checkpoint.
# Full domain evidence remains available through the existing scan/run/review read endpoints.
@router.get("/{workflow_id}")
async def workflow_status(workflow_id: str, request: Request):
    manager = request.app.state.workflows
    record = await manager.workflow.record(workflow_id)
    state = await manager.graph.aget_state({"configurable": {"thread_id": workflow_id}})
    run = (
        await asyncio.to_thread(manager.service.store.load_run, record.artifact_run_id)
        if record.artifact_run_id
        else None
    )
    return {
        "workflow_id": workflow_id,
        "status": record.status,
        "current_step": list(state.next),
        "scan_id": record.scan_id,
        "artifact_run_id": record.artifact_run_id,
        "pending": [item.value for item in state.interrupts],
        "units": {
            key: {
                "status": unit.status,
                "checks": {name: check.status for name, check in unit.checks.items()},
                "diagnostics": unit.diagnostics,
            }
            for key, unit in run.execution.units.items()
        }
        if run and run.execution
        else {},
        "result": run.result if run else None,
        "error_category": record.error_category,
        "cleanup_complete": record.cleanup_complete,
        "cleanup_error": record.cleanup_error,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


# Resume one native interruption after checking its expected form or review identifiers.
# A busy VM returns immediately so no hidden queue builds up behind another workflow.
@router.post("/{workflow_id}/resume", status_code=202)
async def resume_workflow(workflow_id: str, body: ResumeRequest, request: Request):
    record = await request.app.state.workflows.resume(
        workflow_id, body.model_dump(exclude_none=True)
    )
    return {"workflow_id": record.workflow_id, "status": "running"}


# Signal cancellation through the existing runner and wait for owned-resource cleanup.
# Durable evidence is retained even after its temporary source and candidate are removed.
@router.post("/{workflow_id}/cancel")
async def cancel_workflow(workflow_id: str, request: Request):
    record = await request.app.state.workflows.cancel(workflow_id)
    return {
        "workflow_id": record.workflow_id,
        "status": record.status,
        "cleanup_complete": record.cleanup_complete,
    }
