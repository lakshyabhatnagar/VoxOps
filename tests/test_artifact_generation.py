import asyncio
import json
from collections import Counter
from uuid import uuid4

import pytest
from conftest import settings_for
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from test_artifact_planner import fixture_plan

from voxops.agents.artifact_agent import INSTRUCTIONS
from voxops.artifacts.generator import ArtifactGenerator, ModelBudget
from voxops.domain.artifact_models import ArtifactOutput
from voxops.storage.artifact_store import ArtifactStore
from voxops.storage.mongodb import database_for
from voxops.workflows.stage2_graph import Stage2Graph, merge_results

IGNORES = ".env\n.env.*\n.git/\n*.pem\n*.key\nnode_modules/\n.venv/\n"
DOCKER = 'FROM python:3.12-slim\nWORKDIR /app\nCOPY . .\nUSER 1000\nCMD ["uvicorn", "main:app"]\n'
CI = """name: build
on: [push, pull_request]
permissions:
  contents: read
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683
        with:
          persist-credentials: false
      - run: python -m compileall main.py
"""


# Create a test model that returns sample content for each requested file.
# This lets generation run without a network connection or API key.
def offline_agent(callback=None):
    # Read the file request and pass it to the test response function.
    # Return the response in the format expected by the model.
    async def respond(messages, info):
        prompt = next(part.content for part in messages[-1].parts if hasattr(part, "content"))
        packet = json.loads(prompt)
        task = packet["task"]
        content = {"dockerfile": DOCKER, "dockerignore": IGNORES, "gitignore": IGNORES, "ci": CI}[
            task["artifact_type"]
        ]
        if callback:
            value = callback(packet, content)
            content = await value if hasattr(value, "__await__") else value
        output = {"target_path": task["target_path"], "content": content}
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, output)])

    return Agent(
        FunctionModel(respond),
        output_type=ArtifactOutput,
        retries=0,
        instructions=INSTRUCTIONS,
        instrument=False,
    )


@pytest.mark.parametrize("artifact_type", ["dockerfile", "ci"])
# Inspect existing valid files and request a missing file without a model.
# Check that existing content stays unchanged and the missing file is reported.
def test_existing_preserved_and_unavailable(tmp_path, artifact_type):
    root, analysis, plan = fixture_plan(tmp_path, "fastapi_only", {"fastapi": "container"})
    target, content = (
        ("Dockerfile", DOCKER)
        if artifact_type == "dockerfile"
        else (".github/workflows/ci.yml", CI)
    )
    (root / target).parent.mkdir(parents=True, exist_ok=True)
    (root / target).write_text(content)
    from voxops.artifacts.planner import build_plan

    plan = build_plan(root, analysis, plan.strategy_by_unit)
    settings = settings_for(tmp_path)
    store = ArtifactStore(database_for(settings))
    generator = ArtifactGenerator(settings, store)
    result = asyncio.run(
        generator.generate(
            uuid4().hex,
            root,
            next(task for task in plan.tasks.values() if task.artifact_type == artifact_type),
            plan,
            ModelBudget(settings),
        )
    )
    assert result.status == "validated_existing"
    assert not result.attempts and result.proposal.creator == "existing"
    assert store.read_blob(result.proposal.staged_ref).decode() == content
    assert (root / target).read_text() == content
    result = asyncio.run(
        generator.generate(uuid4().hex, root, plan.tasks["gitignore"], plan, ModelBudget(settings))
    )
    assert result.status == "blocked" and result.error_category == "model_unavailable"
    assert not result.attempts and result.proposal is None


# Resume a run after a model request was interrupted.
# Check that its reserved tokens still count against the spending limit.
def test_unfinished_attempt_retains_token_reservation(tmp_path):
    from datetime import UTC, datetime

    from voxops.domain.artifact_models import ArtifactAttempt, TaskResult
    from voxops.domain.models import AIUsage

    root, _, plan = fixture_plan(tmp_path, "fastapi_only", {"fastapi": "container"})
    settings = settings_for(tmp_path, artifact_max_tokens=2000, artifact_task_tokens=2000)
    generator = ArtifactGenerator(settings, ArtifactStore(database_for(settings)), offline_agent())
    identifier, task = uuid4().hex, plan.tasks["gitignore"]
    prior = ArtifactAttempt(
        run_id=identifier,
        task_id=task.task_id,
        attempt_number=1,
        started_at=datetime.now(UTC),
        model="offline",
        context_hash="digest",
        reserved_tokens=2000,
        usage=AIUsage(status="used", requests=1),
    )
    generator.persist(
        identifier, TaskResult(task_id=task.task_id, status="pending", attempts=[prior])
    )
    budget = ModelBudget(settings, [prior])
    result = asyncio.run(generator.generate(identifier, root, task, plan, budget))
    assert result.status == "blocked" and result.error_category == "model_budget_exhausted"
    assert len(result.attempts) == 1 and budget.requests == 1 and budget.tokens == 2000


# Generate a correction for an existing invalid file.
# Check that the suggested change is small and the original stays untouched.
def test_existing_invalid_artifact_gets_minimal_proposal(tmp_path):
    from voxops.artifacts.planner import build_plan
    from voxops.storage.artifact_store import digest

    root, analysis, plan = fixture_plan(tmp_path, "fastapi_only", {"fastapi": "container"})
    original = DOCKER.replace("USER 1000", "USER root")
    (root / "Dockerfile").write_text(original)
    plan = build_plan(root, analysis, plan.strategy_by_unit)
    settings = settings_for(tmp_path)
    store = ArtifactStore(database_for(settings))
    generator = ArtifactGenerator(settings, store, offline_agent())
    task = next(task for task in plan.tasks.values() if task.artifact_type == "dockerfile")
    result = asyncio.run(generator.generate(uuid4().hex, root, task, plan, ModelBudget(settings)))
    assert result.status == "passed" and len(result.attempts) == 1
    assert result.proposal.original_hash == digest(original.encode())
    assert store.read_blob(result.proposal.staged_ref).decode() == DOCKER
    assert (root / "Dockerfile").read_text() == original


@pytest.mark.parametrize("artifact_type", ["dockerfile", "ci"])
@pytest.mark.parametrize("original", ["", "This file is broken.\n"])
# Start with an empty or broken Dockerfile or CI file and ask the AI to replace it.
# Return a complete valid file that is larger than the old six-line repair allowance.
# Check that the replacement is accepted and saved for review after one request.
# Confirm that the original project file is still untouched.
def test_existing_broken_artifact_can_be_fully_replaced(tmp_path, artifact_type, original):
    from voxops.artifacts.planner import build_plan
    from voxops.storage.artifact_store import digest

    root, analysis, plan = fixture_plan(tmp_path, "fastapi_only", {"fastapi": "container"})
    target = "Dockerfile" if artifact_type == "dockerfile" else ".github/workflows/ci.yml"
    replacement = (
        DOCKER.replace(
            "USER 1000\n",
            "ENV PYTHONUNBUFFERED=1\nENV PYTHONDONTWRITEBYTECODE=1\n"
            'LABEL description="Example app"\nEXPOSE 8000\nUSER 1000\n',
        )
        if artifact_type == "dockerfile"
        else CI
    )
    (root / target).parent.mkdir(parents=True, exist_ok=True)
    (root / target).write_text(original)
    plan = build_plan(root, analysis, plan.strategy_by_unit)
    settings = settings_for(tmp_path)
    store = ArtifactStore(database_for(settings))
    agent = Agent(
        TestModel(custom_output_args={"target_path": target, "content": replacement}),
        output_type=ArtifactOutput,
        retries=0,
    )
    generator = ArtifactGenerator(settings, store, agent)
    task = next(task for task in plan.tasks.values() if task.artifact_type == artifact_type)
    result = asyncio.run(generator.generate(uuid4().hex, root, task, plan, ModelBudget(settings)))

    assert result.status == "passed", result.model_dump(mode="json")
    assert len(result.attempts) == 1
    assert result.proposal.original_hash == digest(original.encode())
    assert store.read_blob(result.proposal.staged_ref).decode() == replacement
    assert (root / target).read_text() == original


@pytest.mark.parametrize("port_count", [0, 1, 12, 60])
# Ask the AI to correct every service port, or populate an empty settings file when requested.
# Some cases need more changed lines than the old relative and forty-line allowances.
# Check that the complete requested repair is saved for review after one attempt.
# Confirm the original file stays unchanged and the repair still needs source-change approval.
def test_requested_source_repair_can_change_every_required_line(tmp_path, port_count):
    from voxops.artifacts.planner import build_plan
    from voxops.domain.artifact_models import SourcePatchSpec
    from voxops.storage.artifact_store import digest

    root, analysis, plan = fixture_plan(tmp_path, "fastapi_only", {"fastapi": "container"})
    original = "".join(f"SERVICE_{index}_PORT = 8000\n" for index in range(port_count))
    replacement = "".join(f"SERVICE_{index}_PORT = 8080\n" for index in range(port_count or 12))
    (root / "settings.py").write_text(original)
    patch = SourcePatchSpec(
        target_path="settings.py",
        expected_hash=digest(original.encode()),
        reason="Correct the documented service ports",
        correction=(
            "Change every service port from 8000 to 8080."
            if port_count
            else (
                "Create twelve service port settings named SERVICE_0_PORT through SERVICE_11_PORT, "
                "each set to 8080."
            )
        ),
    )
    plan = build_plan(root, analysis, plan.strategy_by_unit, patches=[patch])
    settings = settings_for(tmp_path)
    store = ArtifactStore(database_for(settings))
    agent = Agent(
        TestModel(custom_output_args={"target_path": "settings.py", "content": replacement}),
        output_type=ArtifactOutput,
        retries=0,
    )
    generator = ArtifactGenerator(settings, store, agent)
    task = plan.tasks["patch-0"]
    result = asyncio.run(generator.generate(uuid4().hex, root, task, plan, ModelBudget(settings)))
    assert (root / "settings.py").read_text() == original
    assert result.status == "passed" and result.proposal.artifact_type == "source_patch"
    assert len(result.attempts) == 1
    assert task.approval_class == "source_patch"
    assert result.proposal.original_hash == digest(original.encode())
    assert store.read_blob(result.proposal.staged_ref).decode() == replacement


# Generate several files with a limit of one model request for the run.
# Check that the other requests report the exhausted limit.
def test_run_wide_request_budget(tmp_path):
    root, _, plan = fixture_plan(tmp_path, "fastapi_only", {"fastapi": "container"})
    settings = settings_for(tmp_path, artifact_max_requests=1)
    generator = ArtifactGenerator(settings, ArtifactStore(database_for(settings)), offline_agent())
    budget = ModelBudget(settings)
    results = asyncio.run(Stage2Graph(settings, generator, budget).execute(uuid4().hex, root, plan))
    assert budget.requests == 1
    assert sum(len(result.attempts) for result in results.values()) == 1
    assert any(result.error_category == "model_budget_exhausted" for result in results.values())


# Generate files while one Dockerfile repeatedly fails validation.
# Check that it stops after three attempts and other files keep their results.
def test_three_attempts_preserve_siblings_and_barrier(tmp_path):
    root, _, plan = fixture_plan(tmp_path, "fastapi_only", {"fastapi": "container"})
    counts = Counter()

    # Count each file request and remove the Docker instruction for a non-root user.
    # Return valid content for other files so their results can still succeed.
    def broken(packet, content):
        counts[packet["task"]["task_id"]] += 1
        return (
            content.replace("USER 1000\n", "")
            if packet["task"]["artifact_type"] == "dockerfile"
            else content
        )

    settings = settings_for(tmp_path)
    generator = ArtifactGenerator(
        settings, ArtifactStore(database_for(settings)), offline_agent(broken)
    )
    identifier = uuid4().hex
    results = asyncio.run(
        Stage2Graph(settings, generator, ModelBudget(settings)).execute(identifier, root, plan)
    )
    docker = next(key for key in plan.tasks if key.startswith("docker-"))
    assert counts[docker] == 3 and len(results[docker].attempts) == 3
    assert (
        results[docker].status == "failed"
        and results[docker].error_category == "validation_exhausted"
    )
    assert counts["gitignore"] == counts["dockerignore"] == 1
    assert results["gitignore"].status == results["dockerignore"].status == "passed"
    assert results["ci"].status == "blocked" and counts["ci"] == 0
    assert len(generator.previous(identifier, plan.tasks[docker]).attempts) == 3
    assert not (root / ".gitignore").exists()
    with pytest.raises(Exception, match="conflicting proposals"):
        merge_results({"x": results[docker]}, {"x": results["gitignore"]})


# Hold several model requests open and make one file need a correction.
# Check that requests overlap within the limit and the correction uses the feedback.
def test_actual_parallel_generation_and_repair_feedback(tmp_path):
    root, _, plan = fixture_plan(tmp_path, "fastapi_only", {"fastapi": "container"})
    active, maximum, calls = 0, 0, Counter()

    # Create a test environment with an event that controls model responses.
    # Run the generation checks while recording overlapping requests.
    async def scenario():
        event = asyncio.Event()

        # Hold the first model requests until three have started.
        # Then return corrected content when the failed file is requested again.
        async def controlled(packet, content):
            nonlocal active, maximum
            task = packet["task"]
            calls[task["task_id"]] += 1
            active += 1
            maximum = max(maximum, active)
            if active == 3:
                event.set()
            await asyncio.wait_for(event.wait(), 5)
            active -= 1
            if task["artifact_type"] == "dockerfile":
                if calls[task["task_id"]] == 1:
                    return content.replace("USER 1000\n", "")
                assert "non-root USER" in json.dumps(packet["repair_feedback"])
            if task["artifact_type"] == "ci":
                assert calls[next(key for key in plan.tasks if key.startswith("docker-"))] == 2
            return content

        settings = settings_for(tmp_path)
        generator = ArtifactGenerator(
            settings, ArtifactStore(database_for(settings)), offline_agent(controlled)
        )
        return await Stage2Graph(settings, generator, ModelBudget(settings)).execute(
            uuid4().hex, root, plan
        )

    results = asyncio.run(scenario())
    assert maximum == 3
    assert all(result.status in {"passed", "skipped"} for result in results.values())
    assert sum(len(result.attempts) for result in results.values()) == 5


@pytest.mark.parametrize(
    ("path", "content", "error"),
    [
        ("other.txt", IGNORES, "validation_exhausted"),
        (".gitignore", "password = abcdefghijklmno\n", "secret_detected"),
    ],
)
# Return unsafe content from the test model.
# Check that validation reports the problem and does not save the content.
def test_unsafe_model_output_is_not_staged(tmp_path, path, content, error):
    root, _, plan = fixture_plan(tmp_path, "fastapi_only", {"fastapi": "container"})
    settings = settings_for(tmp_path)
    model = TestModel(custom_output_args={"target_path": path, "content": content})
    generator = ArtifactGenerator(
        settings,
        ArtifactStore(database_for(settings)),
        Agent(model, output_type=ArtifactOutput, retries=0),
    )
    result = asyncio.run(
        generator.generate(uuid4().hex, root, plan.tasks["gitignore"], plan, ModelBudget(settings))
    )
    assert result.error_category == error
    assert result.proposal is None
    assert "abcdefghijklmno" not in result.model_dump_json()
    assert generator.store.database.artifact_blobs.count_documents({}) == 0
