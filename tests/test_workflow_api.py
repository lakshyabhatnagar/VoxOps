import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from test_stage4_graph import graph_fixture

from voxops.main import create_app
from voxops.workflows.manager import WorkflowManager


# Wire the actual workflow routes to injected external boundaries and native in-memory checkpoints.
# The graph, existing services, environment form, review checks and local Git publisher remain real.
def api_fixture(tmp_path):
    workflow, previous, remote = graph_fixture(tmp_path)
    workflow.database.workflows.delete_one({"_id": previous.workflow_id})
    manager = WorkflowManager(workflow)
    app = create_app(workflow.service.settings)
    app.state.artifacts, app.state.workflows = workflow.service, manager
    return app, manager, previous, remote


# Wait for the one currently registered background invocation, including its native pause.
# A completed task may already have removed itself from the registry.
async def finish_invocation(manager, workflow_id):
    task = manager.tasks.get(workflow_id)
    if task:
        await asyncio.wait_for(asyncio.shield(task), 20)


# Exercise asynchronous create, polling, secure environment input, exact approval and cleanup.
# No generated branch is sent outside the disposable bare Git remote.
def test_workflow_http_poll_resume_and_publication(tmp_path):
    app, manager, previous, _ = api_fixture(tmp_path)
    routes = app.openapi()["paths"]
    assert "/api/v1/artifact-runs/{run_id}/execute" not in routes
    assert "/api/v1/artifact-runs/{run_id}/publish" not in routes
    assert "/api/v1/workflows/{workflow_id}/retry" not in routes

    # Use the existing prepared source as an eligible artifact retry without re-downloading it.
    # API polling and both resume requests still travel through FastAPI and its private boundary.
    async def scenario():
        await manager.start()
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
        try:
            record = await manager.create(
                previous.request,
                uuid4().hex,
                artifact_run_id=previous.artifact_run_id,
                repository_source_id=previous.repository_source_id,
            )
            await finish_invocation(manager, record.workflow_id)
            assert not manager.slot.locked()
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1"
            ) as client:
                prefix = f"/api/v1/workflows/{record.workflow_id}"
                response = await client.get(prefix)
                assert response.status_code == 200, response.text
                state = response.json()
                assert state["status"] == "waiting_for_env"
                assert state["current_step"] == ["environment"]
                form_url = f"/api/v1/artifact-runs/{previous.artifact_run_id}/verification"
                form = (await client.get(form_url + "/env-form")).json()
                missing = {
                    item["field_id"]: "test-value"
                    for item in form["schema"]["requirements"]
                    if item["resolution"] == "missing"
                }
                denied = await client.post(
                    prefix + "/resume", json={"schema_digest": form["schema_digest"]}
                )
                assert denied.status_code == 409
                supplied = await client.post(
                    form_url + "/env-values",
                    json={
                        "schema_digest": form["schema_digest"],
                        "values": missing,
                    },
                )
                assert supplied.status_code == 200, supplied.text
                resumed = await client.post(
                    prefix + "/resume", json={"schema_digest": form["schema_digest"]}
                )
                assert resumed.status_code == 202, resumed.text
                await finish_invocation(manager, record.workflow_id)
                state = (await client.get(prefix)).json()
                assert state["status"] == "waiting_for_approval", state
                pending = state["pending"][0]
                stale = await client.post(
                    prefix + "/resume",
                    json={
                        "review_id": pending["review_id"],
                        "digest": "0" * 64,
                        "decision": "approve",
                    },
                )
                assert stale.status_code == 409
                approved = await client.post(
                    prefix + "/resume",
                    json={
                        "review_id": pending["review_id"],
                        "digest": pending["digest"],
                        "decision": "approve",
                    },
                )
                assert approved.status_code == 202, approved.text
                await finish_invocation(manager, record.workflow_id)
                final = (await client.get(prefix)).json()
                assert final["status"] == "ready_for_deployment", final
                assert final["cleanup_complete"] and final["cleanup_error"] is None
                assert final["result"]["remote_sha"] == final["result"]["commit_sha"]
                assert (await client.get(f"/api/v1/scans/{final['scan_id']}")).status_code == 200
        finally:
            await manager.close()

    asyncio.run(scenario())


# Hold a managed invocation to prove create returns immediately and another key receives busy.
# The same key returns the first workflow, then cancellation releases the slot.
def test_async_creation_idempotency_busy_and_cancel(tmp_path):
    app, manager, previous, _ = api_fixture(tmp_path)

    # Stop at a controlled awaited boundary without simulating successful graph execution.
    # Cancellation must still clean the manager's metadata and release ownership.
    async def scenario():
        entered = asyncio.Event()
        original = manager.graph.ainvoke

        # Keep this invocation running until cancelled so busy behavior is deterministic.
        # No Docker, Git or model side effects occur while this test gate is held.
        async def hold(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        manager.graph.ainvoke = hold
        await manager.start()
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1"
            ) as client:
                headers = {"Idempotency-Key": uuid4().hex}
                body = previous.request.model_dump(mode="json")
                first = await client.post("/api/v1/workflows", headers=headers, json=body)
                assert first.status_code == 202, first.text
                await entered.wait()
                repeated = await client.post("/api/v1/workflows", headers=headers, json=body)
                assert repeated.json()["workflow_id"] == first.json()["workflow_id"]
                busy = await client.post(
                    "/api/v1/workflows", headers={"Idempotency-Key": uuid4().hex}, json=body
                )
                assert busy.status_code == 409 and busy.json()["error"]["category"] == "server_busy"
                cancelled = await client.post(
                    f"/api/v1/workflows/{first.json()['workflow_id']}/cancel"
                )
                assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
                assert not manager.slot.locked() and not manager.tasks
        finally:
            manager.graph.ainvoke = original
            await manager.close()

    asyncio.run(scenario())


# Expire a saved human-input pause and verify both candidate and source cleanup.
# A restart marks active metadata interrupted without automatically running any checkpoint.
@pytest.mark.parametrize("deadline", ["idle", "workspace"])
def test_idle_expiry_and_startup_interrupted_metadata(tmp_path, deadline):
    _, manager, previous, _ = api_fixture(tmp_path)

    # Use real graph pauses and durable timestamps instead of waiting thirty minutes.
    # The cleanup sweep must enforce the configured elapsed-time rule.
    async def scenario():
        await manager.start()
        try:
            record = await manager.create(
                previous.request,
                uuid4().hex,
                artifact_run_id=previous.artifact_run_id,
                repository_source_id=previous.repository_source_id,
            )
            await finish_invocation(manager, record.workflow_id)
            await manager.update(
                record.workflow_id,
                waiting_since=(datetime.now(UTC) - timedelta(minutes=31)).isoformat(),
            )
            if deadline == "workspace":
                manager.database.sources.update_one(
                    {"_id": previous.repository_source_id},
                    {
                        "$set": {
                            "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
                        }
                    },
                )
            await manager.sweep()
            expired = await manager.workflow.record(record.workflow_id)
            assert expired.status == "expired" and expired.cleanup_complete
            assert expired.error_category == (
                "hitl_idle_timeout" if deadline == "idle" else "workspace_expired"
            )
            assert not manager.slot.locked()
            manager.database.workflows.update_one(
                {"_id": record.workflow_id},
                {
                    "$set": {
                        "status": "running",
                        "created_at": datetime.now(UTC).isoformat(),
                        "waiting_since": None,
                        "repository_source_id": None,
                        "active_seconds": 12,
                        "active_started_at": (
                            datetime.now(UTC) - timedelta(seconds=90)
                        ).isoformat(),
                    }
                },
            )
            await manager.close()
            await manager.start()
            restored = await manager.workflow.record(record.workflow_id)
            assert restored.status == "interrupted"
            assert restored.active_seconds >= 102
            assert restored.active_started_at is None
            assert not manager.tasks
        finally:
            await manager.close()

    asyncio.run(scenario())


# Simulate the persisted operation left by a process that stopped before releasing its claim.
# Cancellation must recover that claim and clean the owned source instead of rejecting the request.
def test_cancel_recovers_dead_process_claim(tmp_path):
    from voxops.domain.artifact_models import OperationClaim

    _, manager, previous, _ = api_fixture(tmp_path)
    run = manager.service.store.load_run(previous.artifact_run_id)
    run.operation = OperationClaim(
        epoch=uuid4().hex, owner_process="99999999:unknown", kind="execute"
    )
    manager.service.store.save_run(run)
    manager.database.workflows.insert_one(
        {"_id": previous.workflow_id, **previous.model_dump(mode="json")}
    )
    result = asyncio.run(manager.cancel(previous.workflow_id))
    assert result.status == "cancelled" and result.cleanup_complete
    assert manager.service.store.load_run(run.run_id).operation is None


# Exhaust the active allowance at an awaited graph boundary without waiting forty-five minutes.
# The manager must stop the invocation, clean its owned workspaces and release the VM slot.
def test_active_budget_timeout_cleans_and_releases(tmp_path):
    _, manager, previous, _ = api_fixture(tmp_path)
    manager.settings = manager.settings.model_copy(
        update={"active_execution_timeout_minutes": 0.001}
    )

    # Hold only the graph invocation, leaving the actual manager and cleanup behavior in place.
    # The active timer must cancel this await rather than leave a live background task.
    async def hold(*args, **kwargs):
        await asyncio.Event().wait()

    manager.graph.ainvoke = hold

    # Use the prepared real source and artifact run so cleanup has actual owned directories.
    # Inspect the persisted terminal result after the timed invocation returns.
    async def scenario():
        record = await manager.create(
            previous.request,
            uuid4().hex,
            artifact_run_id=previous.artifact_run_id,
            repository_source_id=previous.repository_source_id,
        )
        await finish_invocation(manager, record.workflow_id)
        result = await manager.workflow.record(record.workflow_id)
        assert result.status == "expired" and result.cleanup_complete
        assert result.error_category == "active_execution_timeout"
        assert result.active_seconds >= 0.06 and result.active_started_at is None
        assert not manager.tasks and not manager.slot.locked()

    asyncio.run(scenario())
