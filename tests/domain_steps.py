import asyncio

import httpx

from voxops.core.errors import AppError


# Exercise the extracted Stage 3 domain steps without keeping its superseded public executor.
# Stage 4 HTTP orchestration is tested separately through native checkpoints and workflow routes.
async def advance_candidate(service, run_id):
    run = await service.generate_candidate(run_id)
    if run.execution is None or run.status in {
        "awaiting_review",
        "approved",
        "ready_for_deployment",
        "rejected",
    }:
        return run
    while True:
        run = await service.verify_candidate(run_id)
        if run.execution.status in {"passed", "verification_incomplete", "waiting_for_env"}:
            return run
        failed = sorted(key for key, unit in run.execution.units.items() if unit.status == "failed")
        if not failed:
            return run
        before = dict(run.applied_hashes)
        run = await service.repair_candidate(run_id, failed[0])
        if run.applied_hashes == before:
            return run


# Return response-shaped domain results so existing stage assertions keep their readable checks.
# This helper calls services directly and never installs or simulates an obsolete HTTP route.
def artifact_step(client, operation, run_id):
    service = client.app.state.artifacts
    try:
        result = asyncio.run(
            advance_candidate(service, run_id)
            if operation == "advance"
            else service.publish(run_id)
        )
        return httpx.Response(200, json=result.model_dump(mode="json"))
    except AppError as error:
        return httpx.Response(
            error.status, json={"error": {"category": error.category, "message": error.message}}
        )
