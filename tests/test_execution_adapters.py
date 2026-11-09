import asyncio
import json
import shutil
import subprocess
import time
from dataclasses import replace

import httpx
import pytest
from conftest import settings_for
from test_artifact_planner import fixture_plan
from test_execution_docker import SimulatedDocker

from voxops.core.errors import AppError
from voxops.core.settings import Settings
from voxops.domain.execution_models import EnvConsumer, EnvFormSchema, EnvRequirement, ProbeSpec
from voxops.execution.candidate import unit_input_digest
from voxops.execution.contexts import freeze_context, remove_context
from voxops.execution.docker_driver import CommandOutcome, RuntimeHandle, WorkspaceHandle
from voxops.execution.environment import SecretSession
from voxops.execution.planner import build_execution_plan, freeze_plan_contexts
from voxops.execution.readiness import probe_http
from voxops.execution.runner import ExecutionRunner


@pytest.mark.parametrize("suffix", ["", "?v=123", "#version", "?v=123#version"])
def test_static_output_checks_asset_urls_with_query_and_fragment(tmp_path, suffix):
    from voxops.execution.runner import _STATIC_CHECK

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to execute the container static-output checker locally")
    output = tmp_path / "dist"
    output.mkdir()
    (output / "index.html").write_text(f'<html><script src="app.js{suffix}"></script></html>')
    shim = r"""
const fsShim = require('node:fs');
for (const method of ['lstatSync', 'readdirSync', 'readFileSync']) {
  const original = fsShim[method];
  fsShim[method] = function (p, ...args) {
    return original.call(this, p.replace(/^\/workspace(?=\/|$)/, process.argv[2]), ...args);
  };
}
"""
    command = [node, "-e", shim + _STATIC_CHECK, "/workspace/dist", str(tmp_path)]
    absent = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert absent.returncode != 0, "Missing referenced JavaScript must fail verification"
    (output / "app.js").write_text("console.log('built')")
    present = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert present.returncode == 0, present.stderr


def container_files(root, artifact):
    for task in artifact.tasks.values():
        if task.artifact_type != "dockerfile":
            continue
        unit = next(u for u in artifact.units if u.id == task.unit_id)
        command = (
            '["uvicorn","main:app","--host","0.0.0.0","--port","8000"]'
            if unit.framework == "fastapi"
            else '["npm","run","start"]'
        )
        port = 8000 if unit.framework == "fastapi" else 3000
        (root / task.target_path).write_text(
            f"FROM example\nEXPOSE {port}\nUSER 1000\nCMD {command}\n"
        )


@pytest.mark.parametrize(
    ("name", "strategies"),
    [
        ("fastapi_only", {"fastapi": "container"}),
        ("vite_only", {"react_vite": "static"}),
        ("vite_only", {"react_vite": "container"}),
        ("nextjs_fullstack", {"nextjs": "native"}),
        ("nextjs_fullstack", {"nextjs": "container"}),
    ],
)
def test_plans_actual_framework_operations(tmp_path, name, strategies):
    root, _, artifact = fixture_plan(tmp_path, name, strategies)
    container_files(root, artifact)
    plan = build_execution_plan(root, artifact, settings_for(tmp_path))
    spec = next(iter(plan.units.values()))
    assert spec.strategy in strategies.values()
    assert spec.source_inventory_digest
    assert all(t.input_digest for t in plan.tasks.values())
    if spec.strategy == "container":
        assert spec.dockerfile_path == "Dockerfile"
        assert spec.start_argv == []
    else:
        manager = artifact.units[0].runtime.package_manager
        assert spec.install_argv[:2] == (
            ["npm", "install"] if manager == "npm" else ["corepack", manager]
        )
        assert spec.build_argv[-3:] == [manager, "run", "build"]
        assert not spec.dockerfile_path
    contexts = freeze_plan_contexts(root, plan, settings_for(tmp_path))
    for context in contexts.values():
        remove_context(context)


def test_monorepo_context_and_identity_follow_real_inputs(tmp_path):
    root, _, artifact = fixture_plan(
        tmp_path, "monorepo_fastapi_vite", {"fastapi": "container", "react_vite": "static"}
    )
    package = root / "package.json"
    data = json.loads(package.read_text())
    data["packageManager"] = "pnpm@9.15.9"
    package.write_text(json.dumps(data))
    container_files(root, artifact)
    settings = settings_for(tmp_path)
    plan = build_execution_plan(root, artifact, settings)
    web = next(spec for spec in plan.units.values() if spec.strategy == "static")
    api = next(spec for spec in plan.units.values() if spec.strategy == "container")
    assert web.install_argv == ["corepack", "pnpm", "install", "--frozen-lockfile"]
    assert web.command_workdir == "/workspace/apps/web"
    assert web.context_path == "."
    assert "pnpm-lock.yaml" in web.selected_paths
    original = web.source_inventory_digest
    (root / "apps/api/Dockerfile").write_text((root / "apps/api/Dockerfile").read_text() + "\n")
    revised = build_execution_plan(root, artifact, settings)
    assert revised.units[web.unit_id].source_inventory_digest == original
    assert revised.units[api.unit_id].source_inventory_digest != api.source_inventory_digest
    (root / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\nsettings: {}\n")
    changed = build_execution_plan(root, artifact, settings)
    assert changed.units[web.unit_id].source_inventory_digest != original
    contexts = freeze_plan_contexts(root, changed, settings)
    try:
        assert "apps/api/Dockerfile" not in contexts[web.unit_id].files
        assert "apps/api/Dockerfile" in contexts[api.unit_id].files
    finally:
        for context in contexts.values():
            remove_context(context)


@pytest.mark.parametrize("bind_flag", ["-b", "--bind"])
def test_fastapi_container_preserves_supported_gunicorn_bind_flags(tmp_path, bind_flag):
    root, _, artifact = fixture_plan(tmp_path, "fastapi_only", {"fastapi": "container"})
    argv = ["gunicorn", "main:app", bind_flag, "0.0.0.0:9000"]
    artifact.units[0].runtime.start_command = " ".join(argv)
    (root / "Dockerfile").write_text(
        "FROM python:3.12-slim\nCOPY . /app\nWORKDIR /app\nUSER 1000\nEXPOSE 9000\n"
        + "CMD "
        + json.dumps(argv)
        + "\n"
    )
    plan = build_execution_plan(root, artifact, settings_for(tmp_path))
    spec = plan.units[artifact.units[0].id]
    assert spec.probe.port == 9000
    assert spec.start_argv == []


@pytest.mark.parametrize(
    ("manager", "lock", "install"),
    [
        ("npm", "package-lock.json", ["npm", "ci", "--no-audit", "--no-fund"]),
        ("pnpm", "pnpm-lock.yaml", ["corepack", "pnpm", "install", "--frozen-lockfile"]),
        ("yarn", "yarn.lock", ["corepack", "yarn", "install", "--immutable"]),
    ],
)
def test_installer_uses_actual_lock_and_version(tmp_path, manager, lock, install):
    root, _, artifact = fixture_plan(tmp_path, "vite_only", {"react_vite": "static"})
    package = json.loads((root / "package.json").read_text())
    package["packageManager"] = manager + "@4.6.0"
    (root / "package.json").write_text(json.dumps(package))
    (root / lock).write_text("{}")
    unit = artifact.units[0]
    unit.runtime.package_manager = manager
    unit.runtime.build_command = manager + " run build"
    spec = build_execution_plan(root, artifact, settings_for(tmp_path)).units[unit.id]
    assert spec.install_argv == install


def test_static_export_and_unsupported_native_are_distinct(tmp_path):
    root, _, artifact = fixture_plan(tmp_path, "nextjs_fullstack", {"nextjs": "native"})
    unit = artifact.units[0]
    unit.role = "static_frontend"
    unit.runtime.output_directory = "out"
    artifact.strategy_by_unit[unit.id] = "static"
    spec = build_execution_plan(root, artifact, settings_for(tmp_path)).units[unit.id]
    assert spec.required_level == "static_output_verified"
    assert spec.probe is None and spec.start_argv == []
    artifact.strategy_by_unit[unit.id] = "native"
    with pytest.raises(AppError, match="server evidence"):
        build_execution_plan(root, artifact, settings_for(tmp_path))


@pytest.mark.parametrize("script", ["custom-server", "next start && migrate", "next start -p 4000"])
def test_ambiguous_native_start_is_blocked(tmp_path, script):
    root, _, artifact = fixture_plan(tmp_path, "nextjs_fullstack", {"nextjs": "native"})
    data = json.loads((root / "package.json").read_text())
    data["scripts"]["start"] = script
    (root / "package.json").write_text(json.dumps(data))
    with pytest.raises(AppError):
        build_execution_plan(root, artifact, settings_for(tmp_path))


def test_changed_candidate_cannot_execute_stale_plan(tmp_path):
    root, _, artifact = fixture_plan(tmp_path, "vite_only", {"react_vite": "static"})
    settings = settings_for(tmp_path)
    plan = build_execution_plan(root, artifact, settings)
    (root / "index.html").write_text("changed")
    with pytest.raises(AppError, match="changed after planning"):
        freeze_plan_contexts(root, plan, settings)


class ReadyDriver:
    def __init__(self):
        self.cancel = asyncio.Event()
        self.alive = True

    async def inspect_runtime(self, runtime):
        return {"Running": self.alive, "ExitCode": 0, "OOMKilled": False}


@pytest.mark.parametrize(
    ("status", "health", "passes"),
    [
        (200, False, True),
        (404, False, True),
        (302, False, True),
        (500, False, False),
        (404, True, False),
        (204, True, True),
    ],
)
def test_http_status_semantics_and_no_redirect_following(status, health, passes):
    async def run():
        driver, urls = ReadyDriver(), []

        async def respond(request):
            urls.append(str(request.url))
            return httpx.Response(status, headers={"Location": "http://external.invalid/private"})

        operation = probe_http(
            driver,
            RuntimeHandle("id", "name", "task", host_port=1234),
            ProbeSpec(port=8000, health_contract=health, success_statuses=[200, 204]),
            deadline=time.monotonic() + 0.04,
            request_timeout=0.01,
            stability_seconds=0,
            transport=httpx.MockTransport(respond),
        )
        if passes:
            result = await operation
            assert result.status == status
        else:
            with pytest.raises(AppError, match="readiness"):
                await operation
        assert all(url == "http://127.0.0.1:1234/" for url in urls)

    asyncio.run(run())


def test_process_exit_after_response_is_never_success():
    async def run():
        driver = ReadyDriver()

        async def respond(request):
            driver.alive = False
            return httpx.Response(200)

        with pytest.raises(AppError, match="exited during observation"):
            await probe_http(
                driver,
                RuntimeHandle("id", "name", "task", host_port=1234),
                ProbeSpec(port=8000),
                deadline=time.monotonic() + 1,
                request_timeout=0.1,
                transport=httpx.MockTransport(respond),
            )

    asyncio.run(run())


def test_native_handoff_retains_volume_and_is_single_use(tmp_path):
    (tmp_path / "input").write_text("source")
    context = freeze_context(tmp_path)

    async def run():
        driver = SimulatedDocker()
        session = SecretSession(EnvFormSchema(source_digest="0" * 64))
        builder = await driver.workspace_builder("node", "build", session, "web", context)
        try:
            assert isinstance(builder, WorkspaceHandle)
            server = await driver.native_runtime(
                builder, "startup", ["node", "server.js"], session, "web", 3000
            )
            assert builder.name in driver.removed
            assert server.image_id == builder.image_id
            args = driver.created[server.name][1]
            assert any(builder.workspace_volume in arg and "volume-nocopy" in arg for arg in args)
            assert "HOSTNAME=0.0.0.0" in args
            with pytest.raises(AppError, match="not active"):
                await driver.native_runtime(builder, "again", ["node"], session, "web", 3000)
        finally:
            await driver.cleanup()
        assert all(item.state == "removed" for item in driver.resources)

    try:
        asyncio.run(run())
    finally:
        remove_context(context)


def test_native_handoff_deadline_prevents_late_runtime_creation(tmp_path):
    (tmp_path / "input").write_text("source")
    context = freeze_context(tmp_path)

    async def run():
        driver = SimulatedDocker(Settings(startup_timeout_seconds=1))
        session = SecretSession(EnvFormSchema(source_digest="0" * 64))
        builder = await driver.workspace_builder("node", "build", session, "web", context)
        original = driver.command
        first_inspect = True

        async def slow_inspection(args, timeout=30, **kwargs):
            nonlocal first_inspect
            if first_inspect and args[1] == "inspect":
                first_inspect = False
                await asyncio.sleep(min(1.25, timeout))
                if timeout <= 1.25:
                    return CommandOutcome(None, "", timeout, timed_out=True)
            return await original(args, timeout, **kwargs)

        driver.command = slow_inspection
        try:
            with pytest.raises(AppError) as error:
                await driver.native_runtime(
                    builder, "startup", ["node", "server.js"], session, "web", 3000
                )
            assert error.value.category == "TIMEOUT"
            assert len(driver.created) == 1, "Expired handoff must not create a runtime"
            assert all(item.state == "removed" for item in driver.resources)
        finally:
            await driver.cleanup()

    try:
        asyncio.run(run())
    finally:
        remove_context(context)


@pytest.mark.parametrize("mutation", ["unit", "volume", "image", "workdir"])
def test_native_handoff_refuses_changed_ownership(tmp_path, mutation):
    (tmp_path / "input").write_text("source")
    context = freeze_context(tmp_path)

    async def run():
        driver = SimulatedDocker()
        session = SecretSession(EnvFormSchema(source_digest="0" * 64))
        builder = await driver.workspace_builder("node", "build", session, "web", context)
        if mutation in {"unit", "volume", "image"}:
            builder = replace(
                builder,
                **{
                    {"unit": "unit_id", "volume": "workspace_volume", "image": "image_id"}[
                        mutation
                    ]: "foreign"
                },
            )
        with pytest.raises(AppError):
            await driver.native_runtime(
                builder,
                "startup",
                ["node"],
                session,
                "web",
                3000,
                workdir="/workspace/../outside" if mutation == "workdir" else "/workspace",
            )
        assert len(driver.created) == 1
        assert all(item.state == "removed" for item in driver.resources)

    try:
        asyncio.run(run())
    finally:
        remove_context(context)


class AdapterDocker(SimulatedDocker):
    async def exec(self, runtime, argv, timeout, *, workdir="/workspace"):
        if argv[-1] == "--version":
            return CommandOutcome(0, "v22.15.0", 0, _stdout="v22.15.0")
        return CommandOutcome(0, "synthetic-build-config synthetic-runtime-secret", 0)


def test_static_runner_records_prepared_identity_and_sanitized_callbacks(tmp_path):
    root, _, artifact = fixture_plan(tmp_path, "vite_only", {"react_vite": "static"})
    settings = settings_for(tmp_path)
    plan = build_execution_plan(root, artifact, settings)
    unit_id = artifact.units[0].id
    contexts = freeze_plan_contexts(root, plan, settings)
    schema = EnvFormSchema(
        source_digest="0" * 64,
        requirements=[
            EnvRequirement(
                field_id="public",
                name="VITE_PUBLIC",
                visibility="public",
                necessity="required",
                consumers=[EnvConsumer(unit_id=unit_id, phase="build")],
            ),
            EnvRequirement(
                field_id="secret",
                name="TOKEN",
                visibility="secret",
                necessity="required",
                consumers=[EnvConsumer(unit_id=unit_id, phase="runtime")],
            ),
        ],
    )
    session = SecretSession(
        schema, {"public": "synthetic-build-config", "secret": "synthetic-runtime-secret"}
    )
    callbacks, preparations, drivers = [], [], []

    def factory(_):
        driver = AdapterDocker()
        drivers.append(driver)
        return driver

    try:
        result = asyncio.run(
            ExecutionRunner(settings, factory).run(
                plan,
                contexts,
                session,
                config_generations={unit_id: "opaque-authority-1"},
                on_attempt=callbacks.append,
                on_prepared=preparations.append,
            )
        )
        assert result.units[unit_id].status == "passed"
        assert result.units[unit_id].achieved_level == "static_output_verified"
        assert result.units[unit_id].input_digest == unit_input_digest(result.plan, unit_id)
        assert len(preparations) == 1
        assert [a.status for a in callbacks] == ["running", "passed", "running", "passed"]
        assert all(a.input_digest == preparations[0].task_input_digest for a in result.attempts)
        assert all(
            value not in repr(callbacks)
            for value in ("synthetic-build-config", "synthetic-runtime-secret")
        )
        assert all(r.state == "removed" for d in drivers for r in d.resources)
    finally:
        for context in contexts.values():
            remove_context(context)


def test_global_runtime_budget_obeys_strictest_active_policy_across_loops():
    first = SimulatedDocker(Settings(execution_max_parallel=1))
    session = SecretSession(EnvFormSchema(source_digest="0" * 64))
    asyncio.run(first.runtime("node", "a", ["sleep", "20"], session, "a"))

    async def run():
        second = SimulatedDocker(Settings(execution_max_parallel=4, startup_timeout_seconds=1))
        pending = asyncio.create_task(second.runtime("node", "b", ["sleep", "20"], session, "b"))
        await asyncio.sleep(0.02)
        assert not pending.done()
        assert not second.created
        second.cancel.set()
        with pytest.raises(AppError, match="cancelled"):
            await pending
        assert not second.created
        await first.cleanup()

    asyncio.run(run())


@pytest.mark.parametrize("upstream_fails", [False, True])
def test_cross_unit_dependency_finishes_before_dependent_starts(tmp_path, upstream_fails):
    root, _, artifact = fixture_plan(tmp_path, "vite_only", {"react_vite": "static"})
    other = root / "other"
    other.mkdir()
    (other / "package.json").write_bytes((root / "package.json").read_bytes())
    (other / "index.html").write_text("<main>Other app</main>")
    dependent = artifact.units[0].id
    upstream = "upstream"
    copied = artifact.units[0].model_copy(deep=True)
    copied.id, copied.root_path = upstream, "other"
    copied.runtime.dependency_files = ["other/package.json"]
    artifact.units.append(copied)
    artifact.strategy_by_unit[upstream] = "static"
    settings = settings_for(tmp_path)
    plan = build_execution_plan(root, artifact, settings)
    plan.tasks[f"{dependent}:build"].depends_on = [f"{upstream}:static_output"]
    contexts = freeze_plan_contexts(root, plan, settings)
    calls, events = [], []

    class UpstreamDocker(AdapterDocker):
        async def exec(self, runtime, argv, timeout, *, workdir="/workspace"):
            if upstream_fails and argv == plan.units[upstream].install_argv:
                return CommandOutcome(1, "Production build failed", 0)
            return await super().exec(runtime, argv, timeout, workdir=workdir)

    def factory(unit_id):
        calls.append(unit_id)
        return UpstreamDocker() if unit_id == upstream else AdapterDocker()

    try:
        outcome = asyncio.run(
            ExecutionRunner(settings, factory).run(
                plan,
                contexts,
                SecretSession(EnvFormSchema(source_digest="0" * 64)),
                on_attempt=events.append,
            )
        )
        if upstream_fails:
            assert calls == [upstream]
            assert outcome.units[upstream].status == "failed"
            assert outcome.units[dependent].status == "blocked"
            assert not any(attempt.unit_id == dependent for attempt in events)
        else:
            assert calls == [upstream, dependent]
            assert all(unit.status == "passed" for unit in outcome.units.values())
            finished = next(
                i
                for i, attempt in enumerate(events)
                if attempt.task_id == f"{upstream}:static_output" and attempt.status == "passed"
            )
            started = next(
                i
                for i, attempt in enumerate(events)
                if attempt.task_id == f"{dependent}:build" and attempt.status == "running"
            )
            assert finished < started
    finally:
        for context in contexts.values():
            remove_context(context)


def test_reuse_checks_current_toolchain_and_configuration(tmp_path):
    root, _, artifact = fixture_plan(tmp_path, "vite_only", {"react_vite": "static"})
    settings = settings_for(tmp_path)
    plan = build_execution_plan(root, artifact, settings)
    contexts = freeze_plan_contexts(root, plan, settings)
    unit_id = artifact.units[0].id
    drivers = []
    changed_toolchain = False

    class VersionedDocker(AdapterDocker):
        async def resolve_image(self, image, **kwargs):
            metadata = await super().resolve_image(image, **kwargs)
            if changed_toolchain:
                metadata["Id"] = "sha256:changed"
            return metadata

    def factory(_):
        driver = VersionedDocker()
        drivers.append(driver)
        return driver

    async def run():
        nonlocal changed_toolchain
        session = SecretSession(EnvFormSchema(source_digest="0" * 64))
        runner = ExecutionRunner(settings, factory)
        first = await runner.run(plan, contexts, session)
        second = await runner.run(plan, contexts, session, previous=first)
        assert len(second.attempts) == len(first.attempts)
        assert not drivers[-1].created
        assert all(args[1] in {"info", "version", "image"} for args, _ in drivers[-1].events)
        changed_toolchain = True
        third = await runner.run(plan, contexts, session, previous=second)
        assert drivers[-1].created
        assert len(third.attempts) > len(second.attempts)
        assert third.units[unit_id].input_digest != second.units[unit_id].input_digest

    try:
        asyncio.run(run())
    finally:
        for context in contexts.values():
            remove_context(context)


def test_required_env_cannot_be_reused_after_secret_session_loss(tmp_path):
    root, _, artifact = fixture_plan(tmp_path, "vite_only", {"react_vite": "static"})
    settings = settings_for(tmp_path)
    plan = build_execution_plan(root, artifact, settings)
    unit_id = artifact.units[0].id
    contexts = freeze_plan_contexts(root, plan, settings)
    schema = EnvFormSchema(
        source_digest="0" * 64,
        requirements=[
            EnvRequirement(
                field_id="config",
                name="VITE_CONFIG",
                visibility="public",
                necessity="required",
                consumers=[EnvConsumer(unit_id=unit_id, phase="build")],
            )
        ],
    )
    drivers = []

    def factory(_):
        driver = AdapterDocker()
        drivers.append(driver)
        return driver

    async def run():
        runner = ExecutionRunner(settings, factory)
        first = await runner.run(
            plan,
            contexts,
            SecretSession(schema, {"config": "supplied-value"}),
            config_generations={unit_id: "old-authority"},
        )
        assert first.units[unit_id].status == "passed"
        second = await runner.run(
            plan,
            contexts,
            SecretSession(schema),
            previous=first,
            config_generations={unit_id: "old-authority"},
        )
        assert second.units[unit_id].status == "blocked"
        assert second.units[unit_id].diagnostics[0].category == "MISSING_ENV"
        assert not drivers[-1].created

    try:
        asyncio.run(run())
    finally:
        for context in contexts.values():
            remove_context(context)


def test_cross_unit_dag_waits_and_blocks_failed_prerequisite(tmp_path):
    root, _, artifact = fixture_plan(tmp_path, "vite_only", {"react_vite": "static"})
    original = artifact.units[0]
    sibling = original.model_copy(deep=True)
    sibling.id = "dependent-web"
    artifact.units.append(sibling)
    artifact.strategy_by_unit[sibling.id] = "static"
    settings = settings_for(tmp_path)
    plan = build_execution_plan(root, artifact, settings)
    task = plan.tasks[f"{sibling.id}:build"]
    task.depends_on.append(f"{original.id}:static_output")
    plan = type(plan).model_validate(plan.model_dump())
    contexts = freeze_plan_contexts(root, plan, settings)
    factories = []

    class FailingDocker(AdapterDocker):
        async def exec(self, runtime, argv, timeout, **kwargs):
            if argv[-1] != "--version":
                return CommandOutcome(2, "deterministic fixture failure", 0)
            return await super().exec(runtime, argv, timeout, **kwargs)

    def factory(unit_id):
        factories.append(unit_id)
        return FailingDocker()

    try:
        result = asyncio.run(
            ExecutionRunner(settings, factory).run(
                plan, contexts, SecretSession(EnvFormSchema(source_digest="0" * 64))
            )
        )
        assert factories == [original.id]
        assert result.units[original.id].status == "failed"
        assert result.units[sibling.id].status == "blocked"
    finally:
        for context in contexts.values():
            remove_context(context)


@pytest.mark.parametrize("failure", ["before_toolchain", "build", "external_service"])
def test_explicit_retry_rechecks_failed_and_incomplete_results(tmp_path, monkeypatch, failure):
    """Retry the same inputs after a failed check, then after recovery.

    Each call represents one explicit request to verify again.
    Keep old attempts and clean up resources from every new execution.
    """
    from voxops.execution.readiness import HTTPObservation

    external = failure == "external_service"
    root, _, artifact = fixture_plan(
        tmp_path,
        "fastapi_only" if external else "vite_only",
        {"fastapi": "container"} if external else {"react_vite": "static"},
    )
    container_files(root, artifact)
    settings = settings_for(tmp_path)
    plan = build_execution_plan(root, artifact, settings)
    contexts = freeze_plan_contexts(root, plan, settings)
    unit_id = artifact.units[0].id
    drivers = []
    broken = True

    class RecoveringDocker(AdapterDocker):
        async def workspace_builder(self, *args, **kwargs):
            if broken and failure == "before_toolchain":
                raise AppError("BUILD_FAILED", "Builder could not start.", 409)
            return await super().workspace_builder(*args, **kwargs)

        async def exec(self, runtime, argv, timeout, **kwargs):
            if broken and failure == "build" and argv == plan.units[unit_id].build_argv:
                return CommandOutcome(1, "Production build failed", 0)
            return await super().exec(runtime, argv, timeout, **kwargs)

        async def runtime(self, *args, **kwargs):
            if broken and external:
                raise AppError("UNKNOWN_RUNTIME_FAILURE", "DATABASE_URL connection refused", 409)
            return await super().runtime(*args, **kwargs)

    def factory(_):
        driver = RecoveringDocker()
        drivers.append(driver)
        return driver

    async def probe(*args, **kwargs):
        return HTTPObservation(200, 0, ("Local readiness response",))

    monkeypatch.setattr("voxops.execution.runner.probe_http", probe)
    schema = EnvFormSchema(source_digest="0" * 64)
    if external:
        schema.requirements = [
            EnvRequirement(
                field_id="db",
                name="DATABASE_URL",
                necessity="required",
                resolution="supplied",
                consumers=[EnvConsumer(unit_id=unit_id, phase="runtime")],
            )
        ]
    session = SecretSession(schema, {"db": "postgresql://fixture.invalid/db"} if external else {})

    async def exercise():
        nonlocal broken
        runner = ExecutionRunner(settings, factory)
        first = await runner.run(
            plan, contexts, session, config_generations=session.unit_generations
        )
        expected = "verification_incomplete" if external else "failed"
        assert first.units[unit_id].status == expected
        second = await runner.run(
            plan,
            contexts,
            session,
            config_generations=session.unit_generations,
            previous=first,
        )
        assert second.units[unit_id].status == expected
        first_task = f"{unit_id}:startup" if external else f"{unit_id}:build"
        assert [a.attempt_number for a in second.attempts if a.task_id == first_task] == [1, 2]
        broken = False
        third = await runner.run(
            plan,
            contexts,
            session,
            config_generations=session.unit_generations,
            previous=second,
        )
        assert third.units[unit_id].status == "passed"
        assert [a.attempt_number for a in third.attempts if a.task_id == first_task] == [1, 2, 3]
        cached = await runner.run(
            plan,
            contexts,
            session,
            config_generations=session.unit_generations,
            previous=third,
        )
        assert cached.attempts == third.attempts
        assert not drivers[-1].created
        assert all(r.state == "removed" for d in drivers for r in d.resources)

    try:
        asyncio.run(exercise())
    finally:
        for context in contexts.values():
            remove_context(context)
