import asyncio

from conftest import settings_for
from test_artifact_planner import fixture_plan
from test_execution_adapters import AdapterDocker, container_files

from voxops.core.errors import AppError
from voxops.domain.artifact_models import ArtifactTask
from voxops.domain.execution_models import EnvConsumer, EnvFormSchema, EnvRequirement
from voxops.execution.contexts import remove_context
from voxops.execution.diagnostics import repair_target
from voxops.execution.docker_driver import CommandOutcome
from voxops.execution.environment import SecretSession
from voxops.execution.planner import build_execution_plan, freeze_plan_contexts
from voxops.execution.runner import ExecutionRunner
from voxops.storage.artifact_store import digest


def test_transient_limit_when_recorded_toolchain_cannot_be_rechecked(tmp_path):
    root, _, artifact = fixture_plan(tmp_path, "vite_only", {"react_vite": "static"})
    settings = settings_for(tmp_path)
    plan = build_execution_plan(root, artifact, settings)
    contexts = freeze_plan_contexts(root, plan, settings)
    state = {"offline": False}

    class OutageDocker(AdapterDocker):
        async def preflight(self, *args, **kwargs):
            if state["offline"]:
                raise AppError("DOCKER_UNAVAILABLE", "Docker daemon is unavailable.", 409)
            return await super().preflight(*args, **kwargs)

        async def exec(self, runtime, argv, timeout, **kwargs):
            if argv[-1] != "--version":
                raise AppError("REGISTRY_UNAVAILABLE", "Registry connection refused.", 409)
            return await super().exec(runtime, argv, timeout, **kwargs)

    async def exercise():
        session = SecretSession(EnvFormSchema(source_digest="0" * 64))
        outcome = await ExecutionRunner(settings, lambda _: OutageDocker()).run(
            plan, contexts, session
        )
        assert next(iter(outcome.plan.units.values())).toolchain
        state["offline"] = True
        for _ in range(10):
            outcome = await ExecutionRunner(settings, lambda _: OutageDocker()).run(
                outcome.plan, contexts, session, previous=outcome, retry_failed=False
            )
        assert len(outcome.attempts) <= 2

    try:
        asyncio.run(exercise())
    finally:
        for context in contexts.values():
            remove_context(context)


def failed_container(tmp_path, monkeypatch, log, schema_factory=None):
    root, _, artifact = fixture_plan(tmp_path, "fastapi_only", {"fastapi": "container"})
    container_files(root, artifact)
    settings = settings_for(tmp_path)
    plan = build_execution_plan(root, artifact, settings)
    unit = artifact.units[0].id
    schema = schema_factory(unit) if schema_factory else EnvFormSchema(source_digest="0" * 64)
    contexts = freeze_plan_contexts(root, plan, settings)

    class FailedDocker(AdapterDocker):
        async def logs(self, runtime):
            return CommandOutcome(0, log, 0)

    async def exited(*args, **kwargs):
        raise AppError(
            "UNKNOWN_RUNTIME_FAILURE",
            "Owned process exited during observation (exit=1, OOM=False).",
            409,
        )

    monkeypatch.setattr("voxops.execution.runner.observe_process", exited)
    try:
        outcome = asyncio.run(
            ExecutionRunner(settings, lambda _: FailedDocker()).run(
                plan, contexts, SecretSession(schema)
            )
        )
    finally:
        for context in contexts.values():
            remove_context(context)
    return (root, artifact, outcome, unit)


def test_unrelated_log_lines_do_not_make_runtime_failure_waivable(tmp_path, monkeypatch):

    def schema(unit):
        return EnvFormSchema(
            source_digest="0" * 64,
            requirements=[
                EnvRequirement(
                    field_id="db",
                    name="DATABASE_URL",
                    necessity="required",
                    resolution="default",
                    default_kind="literal",
                    consumers=[EnvConsumer(unit_id=unit, phase="runtime")],
                )
            ],
        )

    _, _, outcome, unit = failed_container(
        tmp_path,
        monkeypatch,
        "DATABASE_URL connected successfully\n"
        "Local healthcheck failed: connection refused on localhost:8000\n",
        schema,
    )
    result = outcome.units[unit]
    assert result.status == "failed"


def test_owned_bind_failure_in_runtime_logs_reaches_repair_selector(tmp_path, monkeypatch):
    root, artifact, outcome, unit = failed_container(
        tmp_path, monkeypatch, "main.py: EADDRNOTAVAIL while binding application listener\n"
    )
    artifact.tasks["patch"] = ArtifactTask(
        task_id="patch",
        artifact_type="source_patch",
        target_path="main.py",
        existing_hash=digest((root / "main.py").read_bytes()),
        approval_class="source_patch",
        reason="Approved deployment correction",
    )
    diagnostic = outcome.units[unit].diagnostics[0]
    target = repair_target(diagnostic, outcome.plan, artifact, root)
    assert target is not None and target.task_id == "patch"
