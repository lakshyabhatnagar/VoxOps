import asyncio
from uuid import uuid4

import pytest
from conftest import settings_for
from httpx import Request
from openai import APIConnectionError
from pydantic_ai.exceptions import ModelAPIError
from test_artifact_generation import CI, offline_agent
from test_artifact_planner import fixture_plan

from voxops.artifacts.generator import ArtifactGenerator, ModelBudget
from voxops.domain.artifact_models import ArtifactRun, RepositoryTarget
from voxops.storage.artifact_store import ArtifactStore
from voxops.storage.mongodb import database_for


# Exhaust one workflow's file allowance, then start a distinct user attempt against the same run.
# The new attempt gets three bounded tries while earlier usage and globally unique IDs remain.
def test_new_workflow_gets_fresh_file_allowance_without_erasing_history(tmp_path):
    root, _, plan = fixture_plan(tmp_path, "fastapi_only", {"fastapi": "container"})
    settings = settings_for(tmp_path)
    store = ArtifactStore(database_for(settings))
    first_workflow, second_workflow = uuid4().hex, uuid4().hex
    run = ArtifactRun(
        run_id=uuid4().hex,
        workflow_attempt_id=first_workflow,
        repo_fingerprint=plan.repo_fingerprint,
        scan_id=plan.scan_id,
        source_input_fingerprint=plan.source_input_fingerprint,
        source_fingerprint="source",
        source_head_sha=None,
        plan=plan,
        target=RepositoryTarget(owner="example", name="test"),
        branch="voxops/test",
    )
    store.save_run(run)
    task = plan.tasks["ci"]
    bad = ArtifactGenerator(settings, store, offline_agent(lambda packet, content: "invalid yaml"))
    failed = asyncio.run(bad.generate(run.run_id, root, task, plan, ModelBudget(settings)))
    assert failed.status == "failed" and len(failed.attempts) == 3
    same = asyncio.run(bad.generate(run.run_id, root, task, plan, ModelBudget(settings)))
    assert len(same.attempts) == 3
    run.workflow_attempt_id = second_workflow
    store.save_run(run)
    good = ArtifactGenerator(settings, store, offline_agent(lambda packet, content: CI))
    passed = asyncio.run(
        good.generate(run.run_id, root, task, plan, ModelBudget(settings, failed.attempts))
    )
    assert passed.status == "passed" and len(passed.attempts) == 4
    assert [a.attempt_number for a in passed.attempts] == [1, 2, 3, 4]
    assert len({a.attempt_id for a in passed.attempts}) == 4
    assert [a.workflow_id for a in passed.attempts] == [first_workflow] * 3 + [second_workflow]


@pytest.mark.parametrize("recovers", [True, False])
@pytest.mark.parametrize("failure", ["timeout", "connection"])
# Retry a transient model timeout once, preserving both request records and reserved usage.
# Successful validation completes the task; another timeout stops before a third automatic call.
def test_transient_model_call_has_one_additional_retry(tmp_path, recovers, failure):
    root, _, plan = fixture_plan(tmp_path, "fastapi_only", {"fastapi": "container"})
    settings = settings_for(tmp_path)
    store = ArtifactStore(database_for(settings))
    agent = offline_agent(lambda packet, content: CI)
    original = agent.run
    calls = []

    # Fail at the provider boundary without sleeping or contacting a real model.
    # Delegate the second request to the typed offline agent when recovery is requested.
    async def respond(*args, **kwargs):
        calls.append(True)
        if not recovers or len(calls) == 1:
            if failure == "connection":
                raise ModelAPIError(
                    model_name="offline", message="Connection error"
                ) from APIConnectionError(request=Request("POST", "https://example.invalid"))
            raise TimeoutError
        return await original(*args, **kwargs)

    agent.run = respond
    result = asyncio.run(
        ArtifactGenerator(settings, store, agent).generate(
            uuid4().hex, root, plan.tasks["ci"], plan, ModelBudget(settings)
        )
    )
    assert len(calls) == len(result.attempts) == 2
    assert result.status == ("passed" if recovers else "blocked")
    assert result.attempts[0].error_category == (
        "model_timeout" if failure == "timeout" else "model_transport"
    )
    assert sum(a.usage.requests for a in result.attempts) == 2
