import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from conftest import settings_for
from test_artifact_generation import DOCKER, offline_agent
from test_artifact_planner import fixture_plan
from test_execution_adapters import AdapterDocker, container_files

from voxops.artifacts.generator import ArtifactGenerator, ModelBudget
from voxops.artifacts.planner import build_plan
from voxops.artifacts.service import apply_proposals
from voxops.core.errors import AppError
from voxops.domain.artifact_models import ArtifactRun, ArtifactTask, RepairIntent, RepositoryTarget
from voxops.domain.execution_models import (
    CheckResult,
    EnvConsumer,
    EnvFormSchema,
    EnvRequirement,
    ExecutionAttempt,
    ExecutionDiagnostic,
    OperatorExternalBlocker,
    UnitVerification,
)
from voxops.execution.contexts import remove_context
from voxops.execution.diagnostics import (
    external_blocker,
    normalize_operator_blocker,
    operator_external_blocker,
    repair_target,
)
from voxops.execution.environment import SecretSession
from voxops.execution.planner import build_execution_plan, freeze_plan_contexts
from voxops.execution.readiness import HTTPObservation
from voxops.execution.repair import propose_repair
from voxops.execution.runner import ExecutionRunner
from voxops.storage.artifact_store import ArtifactStore, digest


def authorized_run(run_id, plan, task, result, root, store):
    run = ArtifactRun(
        run_id=run_id,
        repo_fingerprint=plan.repo_fingerprint,
        scan_id=plan.scan_id,
        source_input_fingerprint=plan.source_input_fingerprint,
        source_fingerprint="source",
        source_head_sha=None,
        plan=plan,
        target=RepositoryTarget(owner="example", name="test"),
        branch="test",
        tasks={task.task_id: result},
    )
    apply_proposals(root, run, store)
    store.save_run(run)
    return run


def test_existing_file_repair_uses_same_ledger_and_parent(tmp_path):
    root, analysis, plan = fixture_plan(tmp_path, "fastapi_only", {"fastapi": "container"})
    (root / "Dockerfile").write_text(DOCKER)
    plan = build_plan(root, analysis, plan.strategy_by_unit)
    task = next(task for task in plan.tasks.values() if task.artifact_type == "dockerfile")
    settings = settings_for(tmp_path)
    store = ArtifactStore(settings.data_dir)
    generator = ArtifactGenerator(
        settings, store, offline_agent(lambda packet, content: content + "# fix\n")
    )
    run_id = uuid4().hex
    run = ArtifactRun(
        run_id=run_id,
        repo_fingerprint=plan.repo_fingerprint,
        scan_id=plan.scan_id,
        source_input_fingerprint=plan.source_input_fingerprint,
        source_fingerprint="source",
        source_head_sha=None,
        plan=plan,
        target=RepositoryTarget(owner="example", name="test"),
        branch="test",
    )
    budget = ModelBudget(settings)
    initial = asyncio.run(generator.generate(run_id, root, task, plan, budget))
    assert initial.status == "validated_existing"
    run.tasks[task.task_id] = initial
    apply_proposals(root, run, store)
    store.save_run(run)
    assert run.applied_hashes[task.task_id] == digest(DOCKER.encode())
    intent = RepairIntent(expected_applied_hash=digest(DOCKER.encode()), feedback="COPY failed")
    repaired = asyncio.run(generator.generate(run_id, root, task, plan, budget, repair=intent))
    assert repaired.proposal.original_hash == task.existing_hash
    assert repaired.proposal.parent_hash == intent.expected_applied_hash
    assert len(repaired.attempts) == 1
    assert (
        asyncio.run(generator.generate(run_id, root, task, plan, budget, repair=intent)) == repaired
    )
    run.tasks[task.task_id] = repaired
    (root / "Dockerfile").write_text(DOCKER + "# fix\n")
    apply_proposals(root, run, store)
    apply_proposals(root, run, store)
    assert (root / "Dockerfile").read_text() == DOCKER + "# fix\n"
    stale = asyncio.run(generator.generate(run_id, root, task, plan, budget, repair=intent))
    assert stale.status == "blocked" and stale.error_category == "source_changed"
    original_parent = repaired.proposal.parent_hash
    repaired.proposal.parent_hash = "a" * 64
    with pytest.raises(AppError):
        apply_proposals(root, run, store)
    repaired.proposal.parent_hash = original_parent
    repaired.proposal.target_path = "other/Dockerfile"
    with pytest.raises(AppError):
        apply_proposals(root, run, store)
    repaired.proposal.target_path = task.target_path
    (root / "Dockerfile").write_text("tampered")
    with pytest.raises(AppError):
        apply_proposals(root, run, store)
    (root / "Dockerfile").write_text(DOCKER + "# fix\n")
    (store.directory / "artifact-blobs" / repaired.proposal.staged_ref).write_text("corrupt")
    with pytest.raises(AppError):
        apply_proposals(root, run, store)


def test_existing_proposal_refuses_changed_baseline(tmp_path):
    root, analysis, plan = fixture_plan(tmp_path, "fastapi_only", {"fastapi": "container"})
    (root / "Dockerfile").write_text(DOCKER)
    plan = build_plan(root, analysis, plan.strategy_by_unit)
    task = next(task for task in plan.tasks.values() if task.artifact_type == "dockerfile")
    settings = settings_for(tmp_path)
    store = ArtifactStore(settings.data_dir)
    generator = ArtifactGenerator(settings, store, offline_agent())
    run_id = uuid4().hex
    result = asyncio.run(generator.generate(run_id, root, task, plan, ModelBudget(settings)))
    run = ArtifactRun(
        run_id=run_id,
        repo_fingerprint=plan.repo_fingerprint,
        scan_id=plan.scan_id,
        source_input_fingerprint=plan.source_input_fingerprint,
        source_fingerprint="source",
        source_head_sha=None,
        plan=plan,
        target=RepositoryTarget(owner="example", name="test"),
        branch="test",
        tasks={task.task_id: result},
    )
    (root / "Dockerfile").write_text("tampered")
    with pytest.raises(AppError):
        apply_proposals(root, run, store)


def test_repair_may_rewrite_existing_artifact(tmp_path):
    root, analysis, plan = fixture_plan(tmp_path, "fastapi_only", {"fastapi": "container"})
    (root / "Dockerfile").write_text(DOCKER)
    plan = build_plan(root, analysis, plan.strategy_by_unit)
    task = next(task for task in plan.tasks.values() if task.artifact_type == "dockerfile")
    rewritten = (
        'FROM python:3.11-alpine\nWORKDIR /srv\nCOPY . .\nUSER 1001\nCMD ["uvicorn", "main:app"]\n'
    )
    settings = settings_for(tmp_path)
    generator = ArtifactGenerator(
        settings, ArtifactStore(settings.data_dir), offline_agent(lambda packet, content: rewritten)
    )
    run_id = uuid4().hex
    budget = ModelBudget(settings)
    initial = asyncio.run(generator.generate(run_id, root, task, plan, budget))
    authorized_run(run_id, plan, task, initial, root, generator.store)
    result = asyncio.run(
        generator.generate(
            run_id,
            root,
            task,
            plan,
            budget,
            repair=RepairIntent(
                expected_applied_hash=digest(DOCKER.encode()), feedback="COPY failed"
            ),
        )
    )
    assert result.status == "passed"
    assert generator.store.read_blob(result.proposal.staged_ref).decode() == rewritten


def test_repair_keeps_persisted_attempt_limit_across_generators(tmp_path):
    root, analysis, plan = fixture_plan(tmp_path, "fastapi_only", {"fastapi": "container"})
    (root / "Dockerfile").write_text(DOCKER)
    plan = build_plan(root, analysis, plan.strategy_by_unit)
    task = next(task for task in plan.tasks.values() if task.artifact_type == "dockerfile")
    settings = settings_for(tmp_path)
    store = ArtifactStore(settings.data_dir)
    calls = []
    agent = offline_agent(lambda packet, content: calls.append(packet) or content)
    run_id = uuid4().hex
    generator = ArtifactGenerator(settings, store, agent)
    initial = asyncio.run(generator.generate(run_id, root, task, plan, ModelBudget(settings)))
    authorized_run(run_id, plan, task, initial, root, store)
    intent = RepairIntent(expected_applied_hash=digest(DOCKER.encode()), feedback="COPY failed")
    for count in range(1, 4):
        generator = ArtifactGenerator(settings, store, agent)
        previous = generator.previous(run_id, task)
        result = asyncio.run(
            generator.generate(
                run_id, root, task, plan, ModelBudget(settings, previous.attempts), repair=intent
            )
        )
        assert result.status == "passed"
        assert len(result.attempts) == count
    previous = generator.previous(run_id, task)
    result = asyncio.run(
        generator.generate(
            run_id, root, task, plan, ModelBudget(settings, previous.attempts), repair=intent
        )
    )
    assert result.error_category == "validation_exhausted"
    assert len(calls) == 3


def test_external_failure_requires_linked_configured_runtime_dependency():
    schema = EnvFormSchema(
        source_digest="0" * 64,
        requirements=[
            EnvRequirement(
                field_id="db",
                name="DATABASE_URL",
                necessity="required",
                resolution="supplied",
                consumers=[EnvConsumer(unit_id="api", phase="runtime")],
            )
        ],
    )
    failure = ExecutionDiagnostic(
        diagnostic_id="d",
        unit_id="api",
        task_id="api:startup",
        stage="startup",
        category="UNKNOWN_RUNTIME_FAILURE",
        message="Process exited",
    )
    diagnostic = external_blocker(failure, schema, ["DATABASE_URL: connection refused"])
    assert diagnostic.category == "EXTERNAL_SERVICE_UNAVAILABLE"
    assert diagnostic.dependency_name == "DATABASE_URL"
    assert external_blocker(failure, schema, ["HTTP 500"]) is None
    assert external_blocker(failure, schema, ["OTHER_URL: connection refused"]) is None
    assert external_blocker(failure, schema, ["DATABASE_URL: request timeout"]) is None
    assert (
        external_blocker(
            failure.model_copy(update={"category": "TIMEOUT"}),
            schema,
            ["DATABASE_URL: connection refused"],
        )
        is None
    )


@pytest.mark.parametrize(("category", "allowed"), [("DOCKER_UNAVAILABLE", 2), ("BUILD_FAILED", 1)])
def test_transient_retry_survives_new_runner_instance(tmp_path, category, allowed):
    root, _, artifact = fixture_plan(tmp_path, "vite_only", {"react_vite": "static"})
    settings = settings_for(tmp_path)
    plan = build_execution_plan(root, artifact, settings)
    contexts = freeze_plan_contexts(root, plan, settings)
    calls = []

    class TransientDocker(AdapterDocker):
        async def workspace_builder(self, *args, **kwargs):
            calls.append(1)
            raise AppError(category, "Fixture execution failed.", 409)

    async def run():
        session = SecretSession(EnvFormSchema(source_digest="0" * 64))
        first = await ExecutionRunner(settings, lambda _: TransientDocker()).run(
            plan, contexts, session
        )
        second = await ExecutionRunner(settings, lambda _: TransientDocker()).run(
            plan, contexts, session, previous=first
        )
        third = await ExecutionRunner(settings, lambda _: TransientDocker()).run(
            plan, contexts, session, previous=second
        )
        assert len(calls) == allowed
        assert len(first.attempts) == 1
        assert len(second.attempts) == len(third.attempts) == allowed

    try:
        asyncio.run(run())
    finally:
        for context in contexts.values():
            remove_context(context)


def test_operator_blocker_is_scoped_to_optional_default_and_missing_checks():
    schema = EnvFormSchema(
        source_digest="0" * 64,
        requirements=[
            EnvRequirement(
                field_id="db",
                name="DATABASE_URL",
                necessity="optional",
                default_kind="literal",
                resolution="default",
                consumers=[EnvConsumer(unit_id="api", phase="runtime")],
            )
        ],
    )
    unit = UnitVerification(
        unit_id="api",
        input_digest="0" * 64,
        required_level="http_responded",
        status="blocked",
        checks={
            "build": CheckResult(
                check_id="api:build",
                kind="build",
                status="passed",
                attempt_id="build",
                evidence=["build completed"],
            )
        },
    )
    blocker = OperatorExternalBlocker(
        blocker_id="a" * 32,
        unit_id="api",
        dependency_name="DATABASE_URL",
        explanation="The configured database is unavailable.",
        checks=["startup", "http"],
    )
    result = operator_external_blocker(blocker, unit, schema)
    assert result is not None and result.external_signal == "operator"
    assert (
        operator_external_blocker(blocker.model_copy(update={"unit_id": "other"}), unit, schema)
        is None
    )
    unit.diagnostics = [
        ExecutionDiagnostic(
            diagnostic_id="timeout",
            unit_id="api",
            category="TIMEOUT",
            stage="startup",
            message="Startup timed out",
        )
    ]
    assert operator_external_blocker(blocker, unit, schema) is None


def test_operator_normalization_preserves_real_timeout_and_redacts_explanation():
    schema = EnvFormSchema(source_digest="0" * 64, requirements=[EnvRequirement(
        field_id="db", name="DATABASE_URL", necessity="optional", default_kind="literal",
        resolution="default", visibility="secret",
        consumers=[EnvConsumer(unit_id="api", phase="runtime")],
    )])
    original = ExecutionDiagnostic(
        diagnostic_id="timeout", task_id="api:startup", unit_id="api", category="TIMEOUT",
        stage="startup", message="Startup exceeded deadline.",
    )
    attempt = ExecutionAttempt(
        attempt_id="attempt", task_id="api:startup", unit_id="api", input_digest="0" * 64,
        attempt_number=1, status="failed", ended_at=datetime.now(UTC), diagnostics=[original],
    )
    checks = {"build": CheckResult(check_id="api:build", kind="build", status="passed",
                                   attempt_id="build", evidence=["built"])}
    for kind in ("startup", "http"):
        checks[kind] = CheckResult(check_id=f"api:{kind}", kind=kind, status="failed",
                                   attempt_id="attempt", diagnostic_ids=["timeout"])
    unit = UnitVerification(unit_id="api", input_digest="0" * 64, required_level="http_responded",
                            status="failed", checks=checks, diagnostics=[original])
    declaration = OperatorExternalBlocker(
        blocker_id="a" * 32, unit_id="api", dependency_name="DATABASE_URL",
        explanation="Database synthetic-private-value is unavailable.", checks=["startup", "http"],
    )
    result = normalize_operator_blocker(
        declaration, unit, schema, attempt, SecretSession(schema, {"db": "synthetic-private-value"})
    )
    assert result.unit.status == "verification_incomplete"
    assert original in result.attempt.diagnostics and result.attempt.status == "failed"
    assert "synthetic-private-value" not in result.declaration.explanation
    invalid = attempt.model_copy(update={"oom_killed": True})
    assert (
        normalize_operator_blocker(declaration, unit, schema, invalid, SecretSession(schema))
        is None
    )


def test_copy_error_targets_owned_ignore_only_with_existing_excluded_input(tmp_path):
    root, _, artifact = fixture_plan(tmp_path, "fastapi_only", {"fastapi": "container"})
    container_files(root, artifact)
    (root / ".dockerignore").write_text("main.py\n")
    plan = build_execution_plan(root, artifact, settings_for(tmp_path))
    unit = artifact.units[0].id
    docker = next(task for task in artifact.tasks.values() if task.artifact_type == "dockerfile")
    assert "main.py" in docker.required_paths
    diagnostic = ExecutionDiagnostic(
        diagnostic_id="copy",
        unit_id=unit,
        task_id=f"{unit}:build",
        stage="build",
        category="BUILD_FAILED",
        message="COPY main.py failed: not found",
    )
    target = repair_target(diagnostic, plan, artifact, root)
    assert target.task_id == "dockerignore"
    (root / ".dockerignore").write_text(".env\n")
    assert repair_target(diagnostic, plan, artifact, root).task_id == docker.task_id
    assert (
        repair_target(
            diagnostic.model_copy(update={"category": "RESOURCE_LIMIT"}), plan, artifact, root
        )
        is None
    )


def test_source_repair_requires_approved_exact_path_and_deployment_failure(tmp_path):
    root, _, artifact = fixture_plan(tmp_path, "fastapi_only", {"fastapi": "container"})
    container_files(root, artifact)
    plan = build_execution_plan(root, artifact, settings_for(tmp_path))
    unit = artifact.units[0].id
    artifact.tasks["patch"] = ArtifactTask(
        task_id="patch",
        artifact_type="source_patch",
        target_path="main.py",
        existing_hash=digest((root / "main.py").read_bytes()),
        approval_class="source_patch",
        reason="Approved deployment correction",
    )
    diagnostic = ExecutionDiagnostic(
        diagnostic_id="bind",
        unit_id=unit,
        task_id=f"{unit}:startup",
        stage="startup",
        category="UNKNOWN_RUNTIME_FAILURE",
        message="EADDRNOTAVAIL",
        paths=["main.py"],
    )
    assert repair_target(diagnostic, plan, artifact, root).task_id == "patch"
    assert (
        repair_target(diagnostic.model_copy(update={"paths": ["other.py"]}), plan, artifact, root)
        is None
    )
    assert (
        repair_target(diagnostic.model_copy(update={"message": "HTTP 500"}), plan, artifact, root)
        is None
    )


def test_simulated_execution_drives_two_repairs_on_original_ledger(tmp_path, monkeypatch):
    root, _, plan = fixture_plan(tmp_path, "fastapi_only", {"fastapi": "container"})
    task = next(task for task in plan.tasks.values() if task.artifact_type == "dockerfile")
    settings = settings_for(tmp_path)
    store = ArtifactStore(settings.data_dir)
    calls = []
    definition = (
        "FROM python:3.12-slim\nWORKDIR /app\nCOPY . .\nUSER 1000\n"
        'CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]\n'
    )

    def respond(packet, content):
        calls.append(packet)
        return definition + f"# revision {len(calls)}\n"

    generator = ArtifactGenerator(settings, store, offline_agent(respond))
    run_id = uuid4().hex
    run = ArtifactRun(
        run_id=run_id,
        repo_fingerprint=plan.repo_fingerprint,
        scan_id=plan.scan_id,
        source_input_fingerprint=plan.source_input_fingerprint,
        source_fingerprint="source",
        source_head_sha=None,
        plan=plan,
        target=RepositoryTarget(owner="example", name="test"),
        branch="test",
    )

    class BuildDocker(AdapterDocker):
        async def build(self, builder, context, dockerfile, session, unit):
            if "# revision 3" not in (context.path / dockerfile).read_text():
                raise AppError("BUILD_FAILED", "COPY main.py: checksum not found", 409)
            return await super().build(builder, context, dockerfile, session, unit)

    async def observe(*args, **kwargs):
        return {"Running": True, "OOMKilled": False}

    async def probe(*args, **kwargs):
        return HTTPObservation(200, 0.0, ("Simulated HTTP response.",))

    monkeypatch.setattr("voxops.execution.runner.observe_process", observe)
    monkeypatch.setattr("voxops.execution.runner.probe_http", probe)

    async def exercise():
        budget = ModelBudget(settings)
        run.tasks[task.task_id] = await generator.generate(run_id, root, task, plan, budget)
        apply_proposals(root, run, store)
        store.save_run(run)
        previous = None
        session = SecretSession(EnvFormSchema(source_digest="0" * 64))
        for revision in range(1, 4):
            execution_plan = build_execution_plan(root, plan, settings)
            contexts = freeze_plan_contexts(root, execution_plan, settings)
            try:
                outcome = await ExecutionRunner(settings, lambda _: BuildDocker()).run(
                    execution_plan, contexts, session, previous=previous
                )
            finally:
                for context in contexts.values():
                    remove_context(context)
            unit = outcome.units[task.unit_id]
            if revision == 3:
                assert unit.checks["build"].status == "passed"
                assert len(run.tasks[task.task_id].attempts) == 3
                break
            assert unit.checks["build"].status == "failed"
            repaired = await propose_repair(
                run, root, execution_plan, unit.diagnostics[0], generator, budget
            )
            assert repaired.status == "passed"
            assert len(repaired.attempts) == revision + 1
            run.tasks[task.task_id] = repaired
            apply_proposals(root, run, store)
            store.save_run(run)
            previous = outcome
        assert len(calls) == 3
        assert calls[0]["execution_feedback"] is None
        assert all(
            "COPY main.py: checksum not found" in packet["execution_feedback"]
            for packet in calls[1:]
        )
        exhausted = await generator.generate(
            run_id,
            root,
            task,
            plan,
            budget,
            repair=RepairIntent(
                expected_applied_hash=run.applied_hashes[task.task_id], feedback="COPY failed"
            ),
        )
        assert exhausted.error_category == "validation_exhausted"
        assert len(calls) == 3

    asyncio.run(exercise())
