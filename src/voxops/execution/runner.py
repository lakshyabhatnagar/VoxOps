"""Framework verification engine. The caller owns persistence and candidate mutation."""

import asyncio
import inspect
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from uuid import uuid4

from voxops.core.errors import AppError
from voxops.core.settings import Settings
from voxops.domain.execution_models import (
    LEVEL_CHECKS,
    CheckResult,
    ExecutionAttempt,
    ExecutionDiagnostic,
    ExecutionPlan,
    UnitExecutionSpec,
    UnitVerification,
)
from voxops.execution.candidate import execution_policy_fingerprint, unit_input_digest
from voxops.execution.contexts import FrozenContext, verify_context
from voxops.execution.docker_driver import CommandOutcome, DockerDriver
from voxops.execution.environment import SecretSession
from voxops.execution.readiness import observe_process, probe_http
from voxops.storage.artifact_store import digest


@dataclass(frozen=True)
class PreparedUnit:
    spec: UnitExecutionSpec
    task_input_digest: str


@dataclass(frozen=True)
class ExecutionOutcome:
    plan: ExecutionPlan
    units: dict[str, UnitVerification]
    attempts: tuple[ExecutionAttempt, ...]


class ExecutionRunner:
    def __init__(self, settings: Settings, driver_factory: Callable[[str], DockerDriver]):
        self.settings, self.driver_factory = settings, driver_factory

    async def run(
        self,
        plan: ExecutionPlan,
        contexts: Mapping[str, FrozenContext],
        session: SecretSession,
        *,
        config_generations: Mapping[str, str] | None = None,
        on_attempt: Callable | None = None,
        on_prepared: Callable | None = None,
        attempt_numbers: Mapping[str, int] | None = None,
        previous: ExecutionOutcome | None = None,
    ) -> ExecutionOutcome:
        if set(contexts) != plan.units.keys():
            raise AppError(
                "INVALID_ARTIFACT", "Frozen contexts must cover exactly the planned units.", 409
            )
        prepared = plan.model_copy(deep=True)
        generations, numbers = config_generations or {}, dict(attempt_numbers or {})
        attempts = list(previous.attempts) if previous else []
        for attempt in attempts:
            numbers[attempt.task_id] = max(numbers.get(attempt.task_id, 0), attempt.attempt_number)
        limit = asyncio.Semaphore(self.settings.execution_max_parallel)

        async def notify(callback, item):
            if callback:
                result = callback(item)
                if inspect.isawaitable(result):
                    await result

        dependencies = {unit_id: set() for unit_id in prepared.units}
        for task in prepared.tasks.values():
            dependencies[task.unit_id].update(
                prepared.tasks[key].unit_id
                for key in task.depends_on
                if prepared.tasks[key].unit_id != task.unit_id
            )
        pending = {key: set(value) for key, value in dependencies.items()}
        while pending:
            ready = {key for key, value in pending.items() if not value}
            if not ready:
                raise AppError(
                    "INVALID_ARTIFACT",
                    "Cross-unit task topology requires interleaved runtimes and is unsupported.",
                    409,
                )
            pending = {key: value - ready for key, value in pending.items() if key not in ready}
        unit_futures = {}

        async def unit_worker(unit_id):
            prerequisites = [await unit_futures[key] for key in sorted(dependencies[unit_id])]
            if any(result.status != "passed" for result in prerequisites):
                spec = prepared.units[unit_id]
                diagnostic = ExecutionDiagnostic(
                    diagnostic_id=uuid4().hex,
                    unit_id=unit_id,
                    category="INVALID_ARTIFACT",
                    stage="build",
                    message="A required upstream verification task did not pass.",
                )
                return UnitVerification(
                    unit_id=unit_id,
                    input_digest=unit_input_digest(prepared, unit_id),
                    required_level=spec.required_level,
                    status="blocked",
                    diagnostics=[diagnostic],
                    checks={
                        kind: CheckResult(
                            check_id=f"{unit_id}:{kind}",
                            kind=kind,
                            status="blocked",
                            diagnostic_ids=[diagnostic.diagnostic_id],
                        )
                        for kind in LEVEL_CHECKS[spec.required_level]
                    },
                )
            async with limit:
                verify_context(contexts[unit_id])
                driver = None
                if previous and unit_id in previous.units and unit_id in previous.plan.units:
                    spec, prior_spec = prepared.units[unit_id], previous.plan.units[unit_id]
                    prior = previous.units[unit_id]
                    if (
                        prior.status == "passed"
                        and prior_spec.toolchain
                        and self._environment_error(session, unit_id, generations.get(unit_id))
                        is None
                        and spec.model_dump(exclude={"toolchain"})
                        == prior_spec.model_dump(exclude={"toolchain"})
                    ):
                        driver = self.driver_factory(unit_id)
                        try:
                            capacity = await driver.preflight("build")
                            image = await driver.resolve_image(
                                self.settings.execution_buildkit_image
                                if spec.strategy == "container"
                                else spec.builder_image,
                                deadline=time.monotonic() + self.settings.startup_timeout_seconds,
                            )
                            matches = image["Id"] == prior_spec.toolchain.get(
                                "builder_image_id"
                            ) and capacity.docker_version == prior_spec.toolchain.get(
                                "docker_server"
                            )
                        except AppError:
                            matches = False
                        if matches:
                            spec.toolchain = dict(prior_spec.toolchain)
                            checksum = self._input_digest(
                                spec, contexts[unit_id], session, generations.get(unit_id)
                            )
                            for task in prepared.tasks.values():
                                if task.unit_id == unit_id:
                                    task.input_digest = checksum
                            if prior.input_digest == unit_input_digest(prepared, unit_id):
                                await notify(
                                    on_prepared, PreparedUnit(spec.model_copy(deep=True), checksum)
                                )
                                return prior.model_copy(deep=True)
                return await self._unit(
                    prepared,
                    unit_id,
                    contexts[unit_id],
                    session,
                    generations.get(unit_id),
                    numbers,
                    attempts,
                    on_attempt,
                    on_prepared,
                    notify,
                    driver,
                )

        for unit_id in prepared.units:
            unit_futures[unit_id] = asyncio.create_task(unit_worker(unit_id))
        workers = list(unit_futures.values())
        try:
            results = await asyncio.gather(*workers)
        finally:
            for worker in workers:
                if not worker.done():
                    worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        return ExecutionOutcome(
            prepared, {result.unit_id: result for result in results}, tuple(attempts)
        )

    async def _unit(
        self,
        plan,
        unit_id,
        context,
        session,
        generation,
        numbers,
        attempts,
        on_attempt,
        on_prepared,
        notify,
        driver=None,
    ) -> UnitVerification:
        spec = plan.units[unit_id]
        driver = driver or self.driver_factory(unit_id)
        original_redactor = driver.redact
        known_values = {
            value
            for phase in ("build", "runtime")
            for visibility in ("public", "secret", "unknown")
            for value in session.values_for(unit_id, phase, visibility).values()
            if value
        }

        def redact(text):
            for value in sorted(known_values, key=len, reverse=True):
                text = text.replace(value, "[REDACTED]")
            return session.redact(original_redactor(text))

        driver.redact = redact
        checks, diagnostics = {}, []
        runtime = builder = image = None
        active = None
        current_operation = "build"
        prepared_input = False

        async def prepare():
            nonlocal prepared_input
            checksum = self._input_digest(spec, context, session, generation)
            for task in plan.tasks.values():
                if task.unit_id == unit_id:
                    task.input_digest = checksum
            prepared_input = True
            await notify(on_prepared, PreparedUnit(spec.model_copy(deep=True), checksum))

        async def start(operation):
            nonlocal active, current_operation
            current_operation = operation
            key = f"{unit_id}:{operation}"
            number = numbers.get(key, 0) + 1
            if number > 8:
                raise AppError("RESOURCE_LIMIT", "Execution task attempt budget is exhausted.", 409)
            active = ExecutionAttempt(
                attempt_id=uuid4().hex,
                task_id=key,
                unit_id=unit_id,
                input_digest=plan.tasks[key].input_digest,
                attempt_number=number,
                status="running",
            )
            await notify(on_attempt, active.model_copy(deep=True))

        async def finish(evidence, *, exit_code=0, duration=None, oom=None):
            nonlocal active
            active.status, active.ended_at = "passed", datetime.now(UTC)
            active.evidence = [redact(str(item))[:2000] for item in evidence][:30]
            active.exit_code, active.duration_seconds, active.oom_killed = exit_code, duration, oom
            attempts.append(active.model_copy(deep=True))
            await notify(on_attempt, active.model_copy(deep=True))
            result = active
            active = None
            return result

        def check(kind, attempt, evidence, status=None):
            checks[kind] = CheckResult(
                check_id=f"{unit_id}:{kind}",
                kind=kind,
                status="passed",
                attempt_id=attempt.attempt_id,
                evidence=[redact(item)[:2000] for item in evidence][:20],
                http_status=status,
            )

        try:
            verify_context(context)
            environment_error = self._environment_error(session, unit_id, generation)
            if environment_error:
                raise environment_error
            if spec.strategy == "container":
                builder = await driver.builder(f"{unit_id}:build")
                if not builder.image_id or not builder.tool_version:
                    raise AppError("INVALID_ARTIFACT", "Builder provenance is unavailable.", 409)
                spec.toolchain = {
                    "builder_image_id": builder.image_id,
                    "buildctl": redact(builder.tool_version)[:300],
                    "docker_server": driver.capacity.docker_version,
                }
            else:
                builder = await driver.workspace_builder(
                    spec.builder_image, f"{unit_id}:build", session, unit_id, context
                )
                if not builder.image_id:
                    raise AppError("INVALID_ARTIFACT", "Builder provenance is unavailable.", 409)
                toolchain = {
                    "builder_image_id": builder.image_id,
                    "docker_server": driver.capacity.docker_version,
                }
                tools = {"node": ["node", "--version"], "npm": ["npm", "--version"]}
                if spec.install_argv[:1] == ["corepack"]:
                    tools[spec.install_argv[1]] = [*spec.install_argv[:2], "--version"]
                for tool, argv in tools.items():
                    result = await driver.exec(builder, argv, self.settings.build_timeout_seconds)
                    self._require_success(result, "build")
                    version = result._stdout.strip()
                    if not re.fullmatch(
                        r"v?[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", version
                    ):
                        raise AppError(
                            "INVALID_ARTIFACT",
                            "Builder tool version could not be established.",
                            409,
                        )
                    toolchain[tool] = version
                spec.toolchain = toolchain
            await prepare()
            await start("build")
            started = time.monotonic()
            evidence = [
                f"Pinned builder image {builder.image_id}.",
                *[
                    f"{tool} version {value}."
                    for tool, value in spec.toolchain.items()
                    if tool != "builder_image_id"
                ],
            ]
            if spec.strategy == "container":
                image = await driver.build(builder, context, spec.dockerfile_path, session, unit_id)
                evidence.append(
                    f"Built owned application image {image.id} from {spec.dockerfile_path}."
                )
                await driver.cleanup(
                    [item for item in driver.resources if item.resource_id == builder.id]
                )
            else:
                for argv, workdir in (
                    (spec.install_argv, spec.install_workdir),
                    (spec.build_argv, spec.command_workdir),
                ):
                    result = await driver.exec(
                        builder, argv, self.settings.build_timeout_seconds, workdir=workdir
                    )
                    self._require_success(result, "build")
                    if result.output:
                        evidence.append(result.output)
                state = await driver.inspect_runtime(builder)
                if not state.get("Running") or state.get("OOMKilled"):
                    raise AppError(
                        "BUILD_FAILED", "Workspace builder exited or exceeded memory.", 409
                    )
                evidence.append(
                    "Install and production build exited successfully inside the bounded builder."
                )
            attempt = await finish(evidence, duration=time.monotonic() - started)
            check("build", attempt, evidence)
            if spec.strategy == "static":
                await start("static_output")
                output = str(PurePosixPath(spec.command_workdir) / spec.output_directory)
                result = await driver.exec(
                    builder,
                    ["node", "-e", _STATIC_CHECK, output],
                    self.settings.build_timeout_seconds,
                    workdir=spec.command_workdir,
                )
                self._require_success(result, "static_output")
                evidence = [
                    "Nonempty regular index.html and contained output assets verified in "
                    f"{spec.output_directory}."
                ]
                attempt = await finish(evidence, duration=result.duration)
                check("static_output", attempt, evidence)
            else:
                await start("startup")
                port = self._runtime_port(spec, session, unit_id)
                deadline = time.monotonic() + self.settings.startup_timeout_seconds
                if spec.strategy == "container":
                    runtime = await driver.runtime(
                        image.id,
                        f"{unit_id}:startup",
                        [],
                        session,
                        unit_id,
                        port,
                        deadline=deadline,
                    )
                else:
                    argv = list(spec.start_argv)
                    if "--port" in argv:
                        argv[argv.index("--port") + 1] = str(port)
                    runtime = await driver.native_runtime(
                        builder,
                        f"{unit_id}:startup",
                        argv,
                        session,
                        unit_id,
                        port,
                        workdir=spec.command_workdir,
                        deadline=deadline,
                    )
                state = await observe_process(driver, runtime, deadline=deadline)
                evidence = [
                    "Owned runtime process remained running for at least 0.5s.",
                    f"Runtime uses pinned image {runtime.image_id} and an allocated loopback port.",
                    *spec.limitations,
                ]
                attempt = await finish(evidence, oom=bool(state.get("OOMKilled")))
                check("startup", attempt, evidence)
                await start("probe")
                observation = await probe_http(
                    driver,
                    runtime,
                    spec.probe.model_copy(update={"port": port}),
                    deadline=deadline,
                    request_timeout=self.settings.execution_probe_timeout_seconds,
                )
                attempt = await finish(observation.evidence, duration=observation.duration_seconds)
                check("http", attempt, list(observation.evidence), observation.status)
                if spec.probe.health_contract:
                    check("health", attempt, list(observation.evidence), observation.status)
        except (AppError, TimeoutError, asyncio.CancelledError) as error:
            if not prepared_input:
                await prepare()
            category = (
                "CANCELLED"
                if isinstance(error, asyncio.CancelledError)
                else ("TIMEOUT" if isinstance(error, TimeoutError) else error.category)
            )
            allowed = ExecutionDiagnostic.model_fields["category"].annotation.__args__
            if category not in allowed:
                category = (
                    "BUILD_FAILED" if current_operation == "build" else "UNKNOWN_RUNTIME_FAILURE"
                )
            status = (
                "cancelled"
                if category == "CANCELLED"
                else (
                    "blocked"
                    if category
                    in {
                        "MISSING_ENV",
                        "AMBIGUOUS_ENV",
                        "DOCKER_UNAVAILABLE",
                        "DOCKER_PERMISSION",
                        "RESOURCE_LIMIT",
                        "BUILDKIT_UNAVAILABLE",
                        "BUILDKIT_ISOLATION_UNAVAILABLE",
                        "BUILD_SECRET_INJECTION_UNSUPPORTED",
                    }
                    else "failed"
                )
            )
            if active is None and numbers.get(f"{unit_id}:{current_operation}", 0) < 8:
                await start(current_operation)
            diagnostic = ExecutionDiagnostic(
                diagnostic_id=uuid4().hex,
                unit_id=unit_id,
                task_id=f"{unit_id}:{current_operation}",
                category=category,
                stage=current_operation,
                message=redact(str(error) or "Execution was cancelled.")[:2000],
            )
            diagnostics.append(diagnostic)
            if active:
                active.status, active.ended_at = status, datetime.now(UTC)
                active.diagnostics = [diagnostic]
                observed_runtime = runtime or builder
                if observed_runtime:
                    try:
                        state = await driver.inspect_runtime(observed_runtime)
                        active.exit_code = (
                            state.get("ExitCode") if not state.get("Running") else None
                        )
                        active.oom_killed = bool(state.get("OOMKilled"))
                        logs = await driver.logs(observed_runtime)
                        active.evidence = (
                            [redact(logs.output)[: self.settings.execution_log_max_bytes]]
                            if logs.output
                            else []
                        )
                    except (AppError, TimeoutError):
                        pass
                attempts.append(active.model_copy(deep=True))
                await notify(on_attempt, active.model_copy(deep=True))
            for kind in LEVEL_CHECKS[spec.required_level]:
                if kind not in checks:
                    checks[kind] = CheckResult(
                        check_id=f"{unit_id}:{kind}",
                        kind=kind,
                        status="blocked" if status in {"blocked", "cancelled"} else "failed",
                        diagnostic_ids=[diagnostic.diagnostic_id],
                        attempt_id=active.attempt_id if active else None,
                    )
            if isinstance(error, asyncio.CancelledError):
                raise
        finally:
            cleanup = asyncio.create_task(driver.cleanup())
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise
            except AppError as error:
                diagnostic = ExecutionDiagnostic(
                    diagnostic_id=uuid4().hex,
                    unit_id=unit_id,
                    category="RESOURCE_LIMIT",
                    stage=current_operation,
                    message=redact(error.message)[:2000],
                    evidence=["Owned resource cleanup is pending; journal must be reconciled."],
                )
                diagnostics.append(diagnostic)
        passed = {kind for kind, result in checks.items() if result.status == "passed"}
        achieved = "not_attempted"
        for level, kinds in LEVEL_CHECKS.items():
            if kinds and set(kinds) <= passed:
                achieved = level
        status = (
            "passed"
            if set(LEVEL_CHECKS[spec.required_level]) <= passed
            else (
                "cancelled"
                if driver.cancel.is_set()
                else "blocked"
                if any(result.status == "blocked" for result in checks.values())
                else "failed"
            )
        )
        if diagnostics and status == "passed":
            status = "blocked"
        return UnitVerification(
            unit_id=unit_id,
            input_digest=unit_input_digest(plan, unit_id),
            required_level=spec.required_level,
            achieved_level=achieved,
            status=status,
            checks=checks,
            diagnostics=diagnostics,
        )

    def _input_digest(self, spec, context, session, generation):
        schema = []
        for item in session.schema.requirements:
            consumers = [c for c in item.consumers if c.unit_id == spec.unit_id]
            if consumers:
                data = item.model_dump(mode="json", exclude={"resolution"})
                data["consumers"] = [c.model_dump(mode="json") for c in consumers]
                schema.append(data)
        configured = any(
            session.values_for(spec.unit_id, phase, visibility)
            for phase in ("build", "runtime")
            for visibility in ("public", "secret", "unknown")
        )
        return digest(
            {
                "context": context.identity,
                "inventory": context.inventory,
                "spec": spec.model_dump(mode="json"),
                "schema": schema,
                "policy": execution_policy_fingerprint(self.settings),
                "configuration_generation": generation if configured else None,
            }
        )

    @staticmethod
    def _environment_error(session, unit_id, generation):
        if session.schema.ambiguities:
            return AppError("AMBIGUOUS_ENV", "Environment ambiguity requires resolution.", 409)
        if any(
            item.necessity == "required"
            and item.resolution != "default"
            and any(c.unit_id == unit_id for c in item.consumers)
            and not session.contains(item.field_id)
            for item in session.schema.requirements
        ):
            return AppError("MISSING_ENV", "Required unit configuration is missing.", 409)
        configured = any(
            session.values_for(unit_id, phase, visibility)
            for phase in ("build", "runtime")
            for visibility in ("public", "secret", "unknown")
        )
        if configured and (
            not generation or not isinstance(generation, str) or len(generation) > 200
        ):
            return AppError(
                "MISSING_ENV", "Current per-unit configuration authority is required.", 409
            )
        return None

    @staticmethod
    def _require_success(result: CommandOutcome, stage: str):
        if result.cancelled:
            raise AppError("CANCELLED", "Execution was cancelled.", 409)
        if result.timed_out:
            raise AppError("TIMEOUT", f"{stage} exceeded its deadline.", 409)
        if result.exit_code != 0:
            raise AppError(
                "BUILD_FAILED" if stage == "build" else "INVALID_ARTIFACT",
                f"{stage} command failed with exit {result.exit_code}. " + result.output[:1500],
                409,
            )

    @staticmethod
    def _runtime_port(spec, session, unit_id):
        values = {}
        for visibility in ("public", "secret", "unknown"):
            values.update(session.values_for(unit_id, "runtime", visibility))
        supplied = values.get("PORT")
        if supplied is None:
            return spec.probe.port
        if not re.fullmatch(r"[0-9]+", supplied) or not 1 <= int(supplied) <= 65535:
            raise AppError("INVALID_ARTIFACT", "Runtime PORT must be a valid port number.", 409)
        if not spec.port_from_env and int(supplied) != spec.probe.port:
            raise AppError(
                "INVALID_ARTIFACT", "Runtime PORT differs from the validated image port.", 409
            )
        return int(supplied)


_STATIC_CHECK = r"""
const fs = require('node:fs');
const path = require('node:path');
const root = process.argv[1];
function fail() {
  throw new Error('Static output is absent, empty, malformed or contains unsafe links');
}
if (!root.startsWith('/workspace/') || path.normalize(root) !== root) fail();
for (let p = root; p !== '/workspace'; p = path.dirname(p)) {
  if (fs.lstatSync(p).isSymbolicLink() || !fs.lstatSync(p).isDirectory()) fail();
}
const files = [];
function walk(dir) {
  for (const entry of fs.readdirSync(dir)) {
    const file = path.join(dir, entry);
    const stat = fs.lstatSync(file);
    if (stat.isSymbolicLink() || (!stat.isFile() && !stat.isDirectory())) fail();
    if (stat.isDirectory()) walk(file);
    else { if (stat.size > 0) files.push(file); }
  }
}
walk(root);
const index = path.join(root, 'index.html');
if (!files.includes(index)) fail();
const html = fs.readFileSync(index, 'utf8');
if (!/<(?:html|head|body|div|script|main)\b/i.test(html)) fail();
for (const match of html.matchAll(/(?:src|href)\s*=\s*["']([^"']+)["']/gi)) {
  let value = match[1];
  if (/^(?:[a-z][a-z0-9+.-]*:|\/\/)/i.test(value)) continue;
  value = value.split(/[?#]/, 1)[0];
  if (!value) continue;
  try { value = decodeURIComponent(value); } catch { fail(); }
  const asset = path.resolve(root, value.replace(/^\//, ''));
  if (!asset.startsWith(root + path.sep)) fail();
  if (/\.(?:js|mjs|css|png|svg|ico|webp|woff2?)$/i.test(value) && !files.includes(asset)) fail();
}
console.log('Static output verified');
"""
