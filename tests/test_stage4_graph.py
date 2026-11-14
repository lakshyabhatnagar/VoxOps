import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from test_stage3_service_integration import integration_fixture

from voxops.artifacts.service import ArtifactService
from voxops.core.errors import AppError
from voxops.domain.workflow_models import WorkflowRecord, WorkflowRequest
from voxops.repository.acquisition import RepositoryAcquirer, RepositorySource
from voxops.storage.scan_store import ScanStore
from voxops.workflows.stage4_graph import Stage4Workflow, merge_unit_outcomes


# Attach the real service fixture to a graph without re-downloading source from GitHub.
# Git, domain records, native graph transitions and publication remain real local operations.
def graph_fixture(tmp_path):
    service, run, remote = integration_fixture(tmp_path)
    source_path = service.store.source(run.run_id)
    acquirer = RepositoryAcquirer(
        service.settings, service.store.database, service.publisher.executor
    )
    acquirer.root = source_path.parent
    source = RepositorySource(
        id=source_path.name,
        repository="example/project",
        url="https://github.com/example/project.git",
        ref="main",
        commit_sha=run.source_head_sha,
        path=str(source_path),
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=75),
    )
    database = service.store.database
    database.sources.insert_one({"_id": source.id, **source.model_dump(mode="json")})
    record = WorkflowRecord(
        idempotency_key=uuid4().hex,
        request=WorkflowRequest(repository_url=source.url),
        artifact_run_id=run.run_id,
        repository_source_id=source.id,
    )
    database.workflows.insert_one({"_id": record.workflow_id, **record.model_dump(mode="json")})
    workflow = Stage4Workflow(service, acquirer, InMemorySaver())
    return workflow, record, remote


@pytest.mark.parametrize("decision", ["approve", "reject"])
# Drive native environment and review interrupts through the actual generation/verification service.
# Only Docker commands, model responses and GitHub metadata are simulated by the existing fixture.
def test_graph_environment_review_and_local_publication(tmp_path, decision):
    workflow, record, remote = graph_fixture(tmp_path)
    service = workflow.service
    config = {"configurable": {"thread_id": record.workflow_id}}

    # Submit values only to the volatile environment manager, then acknowledge the saved form.
    # Final review resumes the same thread and requires its exact ID and digest.
    async def scenario():
        first = await workflow.graph.ainvoke({"workflow_id": record.workflow_id}, config)
        pending = first["__interrupt__"][0].value
        assert pending["kind"] == "environment"
        form = await asyncio.to_thread(service.environment_form, record.artifact_run_id)
        fields = form["schema"].requirements
        values = {
            field.field_id: "private-test-secret"
            for field in fields
            if field.resolution == "missing"
        }
        await asyncio.to_thread(
            service.submit_environment, record.artifact_run_id, form["schema_digest"], values
        )
        second = await workflow.graph.ainvoke(
            Command(
                resume={
                    "kind": "environment",
                    "schema_digest": form["schema_digest"],
                }
            ),
            config,
        )
        review = second["__interrupt__"][0].value
        assert review["kind"] == "final_review"
        saved = service.store.load_run(record.artifact_run_id)
        assert saved.execution.status == "passed"
        assert second["unit_outcomes"]
        resume = Command(
            resume={
                "review_id": review["review_id"],
                "digest": review["digest"],
                "decision": decision,
            }
        )
        if decision == "reject":
            with pytest.raises(AppError, match="rejected"):
                await workflow.graph.ainvoke(resume, config)
            assert (
                service.publisher.executor.remote_sha(
                    service.store.source(saved.run_id), saved.target.clone_url, saved.branch
                )
                is None
            )
        else:
            await workflow.graph.ainvoke(resume, config)
            published = service.store.load_run(saved.run_id)
            assert published.status == "ready_for_deployment"
            assert published.result.remote_sha == published.result.commit_sha
            assert all(item.state == "removed" for item in published.execution.resources)
        state = await workflow.graph.aget_state(config)
        assert "private-test-secret" not in str(state)
        for collection in service.store.database.list_collection_names():
            assert "private-test-secret" not in str(list(service.store.database[collection].find()))

    try:
        asyncio.run(scenario())
    finally:
        run = service.store.load_run(record.artifact_run_id)
        if run.worktree_id:
            service.worktrees.remove(run.worktree_id, discard_changes=True)
    assert Path(remote).is_dir()


# Merge receipts from independent units and reject two different answers for one unit.
# No worker may silently replace another worker's verification result.
def test_unit_reducer_merges_distinct_keys_and_rejects_conflicts():
    first = {"api": {"status": "passed", "input_digest": "a"}}
    second = {"web": {"status": "passed", "input_digest": "b"}}
    assert merge_unit_outcomes(first, second) == {**first, **second}
    assert merge_unit_outcomes(first, first) == first
    with pytest.raises(AppError, match="conflicting"):
        merge_unit_outcomes(first, {"api": {"status": "failed", "input_digest": "a"}})


# Cancel while source acquisition is finishing on a worker thread.
# The node must join the worker and delete its clone before reporting cancellation complete.
def test_cancelled_acquisition_joins_and_removes_source(tmp_path, monkeypatch):
    workflow, record, _ = graph_fixture(tmp_path)
    workflow.database.workflows.update_one(
        {"_id": record.workflow_id}, {"$set": {"repository_source_id": None}}
    )
    entered = Event()
    created = []
    original = workflow.acquirer.acquire

    # Perform a real local Git acquisition, then hold its result until cancellation arrives.
    # This reproduces a clone completed by a thread whose awaiting graph task was cancelled.
    def finishing_acquire(url, ref, *, cancel):
        source = original(url, "main", cancel=cancel)
        created.append(source)
        entered.set()
        assert cancel.wait(5), "Cancellation did not reach the acquisition worker"
        return source

    monkeypatch.setattr(workflow.acquirer, "acquire", finishing_acquire)

    # Cancel the actual node task after its worker has created the owned checkout.
    # Awaiting that cancellation must include removal rather than leave background writes running.
    async def scenario():
        task = asyncio.create_task(workflow.acquire({"workflow_id": record.workflow_id}))
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert len(created) == 1 and not Path(created[0].path).exists()
    assert workflow.database.sources.find_one({"_id": created[0].id})["status"] == "removed"


# Pause a real baseline publication review before any generated branch or baseline can be pushed.
# The same native thread resumes only with its exact review and persists the ordinary approval.
def test_native_source_review_interrupt(tmp_path):
    from test_stage2_git import scenario

    case = scenario(tmp_path, remote=False)
    settings = case.publisher.settings
    service = ArtifactService(
        settings, ScanStore(case.store.database), case.manager, publisher=case.publisher
    )
    workflow = Stage4Workflow(
        service, RepositoryAcquirer(settings, case.store.database), InMemorySaver()
    )
    builder = StateGraph(dict)
    builder.add_node("source_review", workflow.source_review)
    builder.add_edge(START, "source_review")
    builder.add_edge("source_review", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": uuid4().hex}}

    # Build the review with the real publisher and resume its actual graph node.
    # Approval itself must not create the fake destination or send any Git commits.
    async def check():
        await case.publisher.preflight(case.run)
        result = await graph.ainvoke({"artifact_run_id": case.run.run_id}, config)
        pending = result["__interrupt__"][0].value
        assert pending["kind"] == "source_review"
        await graph.ainvoke(
            Command(
                resume={
                    "review_id": pending["review_id"],
                    "digest": pending["digest"],
                    "decision": "approve",
                }
            ),
            config,
        )
        saved = case.store.load_run(case.run.run_id)
        assert saved.approvals and not saved.progress.baseline_pushed
        assert case.connection.creations == 0
        assert not case.executor.remote_sha(
            case.source, case.connection.target.clone_url, saved.branch
        )

    asyncio.run(check())
