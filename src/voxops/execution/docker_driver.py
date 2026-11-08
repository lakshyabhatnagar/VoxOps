"""Owned Docker execution. Only Docker CLI arguments run on the host."""

import asyncio
import inspect
import json
import math
import os
import re
import signal
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from uuid import uuid4

from voxops.artifacts.security import safe_read
from voxops.core.errors import AppError
from voxops.core.settings import Settings
from voxops.domain.execution_models import OwnedResourceIntent
from voxops.execution.contexts import FrozenContext, verify_context
from voxops.execution.environment import SecretSession


@dataclass(frozen=True)
class ResourceDemand:
    phase: str
    cpus: float
    memory_bytes: int


@dataclass(frozen=True)
class _Lease:
    demand: ResourceDemand
    capacity: "Capacity"
    concurrency: int


class _ExecutionBudget:
    """Reservations include pending creates, across policies and event loops."""

    def __init__(self):
        self.lock = threading.Lock()
        self.leases: dict[str, _Lease] = {}

    def acquire(self, capacity: "Capacity", settings: Settings, phase: str) -> str | None:
        demand = ResourceDemand(
            phase,
            getattr(settings, f"execution_{phase}_cpus"),
            getattr(settings, f"execution_{phase}_memory_mb") * 1024**2,
        )
        candidate = _Lease(demand, capacity, settings.execution_max_parallel)
        with self.lock:
            leases = [*self.leases.values(), candidate]
            if (
                len(leases) > min(lease.concurrency for lease in leases)
                or sum(lease.demand.phase == "build" for lease in leases) > 2
                or sum(lease.demand.cpus for lease in leases)
                > min(lease.capacity.cpus for lease in leases) - 1
                or sum(lease.demand.memory_bytes for lease in leases)
                > min(lease.capacity.memory_bytes for lease in leases) - 1024**3
            ):
                return None
            token = uuid4().hex
            self.leases[token] = candidate
            return token

    def release(self, token: str) -> None:
        with self.lock:
            self.leases.pop(token, None)


_execution_budget = _ExecutionBudget()


@dataclass(frozen=True)
class CommandOutcome:
    exit_code: int | None
    output: str
    duration: float
    timed_out: bool = False
    cancelled: bool = False
    _stdout: str = field(default="", repr=False)


@dataclass(frozen=True)
class Capacity:
    cpus: int
    memory_bytes: int
    cgroup: str
    build_slots: int
    docker_version: str


@dataclass(frozen=True)
class ImageHandle:
    id: str
    name: str
    task_id: str


@dataclass(frozen=True)
class RuntimeHandle:
    id: str
    name: str
    task_id: str
    host_port: int | None = None
    build_deadline: float | None = None
    image_id: str | None = None
    tool_version: str | None = None


@dataclass(frozen=True, kw_only=True)
class WorkspaceHandle(RuntimeHandle):
    workspace_volume: str
    unit_id: str


class DockerDriver:
    def __init__(
        self,
        settings: Settings,
        run_id: str,
        owner: str,
        journal: Callable,
        cancel: asyncio.Event,
        redact: Callable[[str], str],
    ):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", run_id) or not re.fullmatch(
            r"[A-Za-z0-9_.-]{1,80}", owner
        ):
            raise ValueError("Invalid ownership identifier")
        self.settings, self.run_id, self.owner = settings, run_id, owner
        self.journal, self.cancel, self.redact = journal, cancel, redact
        self.resources: list[OwnedResourceIntent] = []
        self._slot = False
        self._acquiring = False
        self._slot_resource: str | None = None
        self._build_deadline: float | None = None
        self._lease: str | None = None
        self._phase: str | None = None
        self.capacity: Capacity | None = None
        self._phase_deadline: float | None = None

    def _labels(self, task: str) -> dict[str, str]:
        return {"app": "voxops-stage3", "owner": self.owner, "run": self.run_id, "task": task}

    def _label_args(self, task: str) -> list[str]:
        return [
            part
            for key, value in self._labels(task).items()
            for part in ("--label", f"{key}={value}")
        ]

    async def _record(self, item: OwnedResourceIntent) -> None:
        result = self.journal(item)
        if inspect.isawaitable(result):
            await result

    async def _intent(self, kind: str, task: str, name: str) -> OwnedResourceIntent:
        if self.cancel.is_set():
            raise AppError("CANCELLED", "Execution was cancelled.", 409)
        item = OwnedResourceIntent(kind=kind, task_id=task, name=name, ownership_token=self.owner)
        await self._record(item)
        self.resources.append(item)
        return item

    async def _active(self, item: OwnedResourceIntent, resource_id: str) -> None:
        item.resource_id, item.state = resource_id, "active"
        await self._record(item)

    async def command(
        self,
        args: list[str],
        timeout: float = 30,
        *,
        ignore_cancel: bool = False,
        machine_output: bool = False,
    ) -> CommandOutcome:
        if not args or args[0] != "docker":
            raise ValueError("Only Docker CLI is permitted")
        started = time.monotonic()
        if self.cancel.is_set() and not ignore_cancel:
            return CommandOutcome(None, "", 0, cancelled=True)
        if timeout <= 0:
            return CommandOutcome(None, "", 0, timed_out=True)
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except FileNotFoundError:
            raise AppError("DOCKER_UNAVAILABLE", "Docker CLI is not installed.", 409) from None

        async def drain(stream, machine=False) -> tuple[str, str]:
            retained: deque[str] = deque()
            used = 0
            pending = bytearray()
            discard = False
            machine_bytes = bytearray()

            def retain(line):
                nonlocal used
                encoded = len(line.encode())
                if encoded > self.settings.execution_log_max_bytes:
                    return
                retained.append(line)
                used += encoded
                while (
                    len(retained) > self.settings.execution_log_max_lines
                    or used > self.settings.execution_log_max_bytes
                ):
                    used -= len(retained.popleft().encode())

            while chunk := await stream.read(4096):
                if machine and len(machine_bytes) <= self.settings.execution_log_max_bytes:
                    machine_bytes.extend(chunk)
                for byte in chunk:
                    if byte == 10:
                        if not discard:
                            line = self.redact(pending.decode("utf-8", errors="replace")) + "\n"
                            retain(line)
                        pending.clear()
                        discard = False
                    elif not discard:
                        pending.append(byte)
                        if len(pending) > self.settings.execution_log_max_bytes:
                            pending.clear()
                            discard = True
            # A bounded final line is complete at EOF. Oversized lines are never retained.
            if pending and not discard:
                line = self.redact(pending.decode("utf-8", errors="replace"))
                retain(line)
            raw = ""
            if machine:
                if len(machine_bytes) > self.settings.execution_log_max_bytes:
                    raise AppError(
                        "DOCKER_OUTPUT_INVALID", "Docker metadata exceeds its limit.", 409
                    )
                try:
                    raw = machine_bytes.decode("utf-8", errors="strict")
                except UnicodeError:
                    raise AppError(
                        "DOCKER_OUTPUT_INVALID", "Docker metadata is not UTF-8.", 409
                    ) from None
            return "".join(retained), raw

        readers = [
            asyncio.create_task(drain(proc.stdout, machine_output)),
            asyncio.create_task(drain(proc.stderr)),
        ]
        waiter = asyncio.create_task(proc.wait())
        complete = asyncio.gather(waiter, *readers)
        cancel = asyncio.create_task(self.cancel.wait()) if not ignore_cancel else None
        timed_out = cancelled = False
        try:
            done, _ = await asyncio.wait(
                [complete] + ([cancel] if cancel else []),
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            cancelled = bool(cancel and cancel in done and self.cancel.is_set())
            timed_out = not done
            if cancelled or timed_out:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            await complete
        except BaseException:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await asyncio.gather(waiter, *readers, return_exceptions=True)
            raise
        finally:
            if cancel:
                cancel.cancel()
        (stdout, machine), (stderr, _) = (task.result() for task in readers)
        combined = (
            "".join(
                (("" if machine_output else stdout) + stderr).splitlines(keepends=True)[
                    -self.settings.execution_log_max_lines :
                ]
            )
            .encode()[-self.settings.execution_log_max_bytes :]
            .decode("utf-8", "ignore")
        )
        return CommandOutcome(
            proc.returncode,
            combined,
            time.monotonic() - started,
            timed_out,
            cancelled,
            machine if machine_output else stdout,
        )

    async def _run(
        self,
        args: list[str],
        timeout: float = 30,
        *,
        ignore_cancel: bool = False,
        machine_output: bool = False,
        deadline: float | None = None,
    ) -> str:
        if deadline is not None:
            timeout = min(timeout, max(0, deadline - time.monotonic()))
        result = await self.command(
            ["docker", *args], timeout, ignore_cancel=ignore_cancel, machine_output=machine_output
        )
        if result.cancelled:
            if not ignore_cancel:
                await self.cleanup()
            raise AppError("CANCELLED", "Execution was cancelled.", 409)
        if result.timed_out:
            if not ignore_cancel:
                await self.cleanup()
            raise AppError("TIMEOUT", "Docker operation timed out. " + result.output[-1500:], 409)
        if result.exit_code:
            raise AppError(
                "DOCKER_OPERATION_FAILED", "Docker operation failed: " + result.output[-1500:], 409
            )
        return result._stdout.strip()

    async def preflight(self, phase: str = "build", *, deadline: float | None = None) -> Capacity:
        if phase not in {"build", "runtime"}:
            raise ValueError("Unknown execution phase")
        if deadline is None:
            deadline = time.monotonic() + (
                self.settings.build_timeout_seconds
                if phase == "build"
                else self.settings.startup_timeout_seconds
            )
        result = await self.command(
            [
                "docker",
                "info",
                "--format",
                '{"NCPU":{{.NCPU}},"MemTotal":{{.MemTotal}},'
                '"CgroupVersion":{{json .CgroupVersion}}}',
            ],
            timeout=min(30, max(0, deadline - time.monotonic())),
            machine_output=True,
        )
        if result.cancelled:
            raise AppError("CANCELLED", "Execution was cancelled.", 409)
        if result.timed_out:
            raise AppError("TIMEOUT", "Docker daemon preflight timed out.", 409)
        if result.exit_code:
            category = (
                "DOCKER_PERMISSION"
                if "permission denied" in result.output.lower()
                else "DOCKER_UNAVAILABLE"
            )
            raise AppError(category, "Docker daemon is unavailable or inaccessible.", 409)
        try:
            info = json.loads(result._stdout)
            cpus, memory = int(info["NCPU"]), int(info["MemTotal"])
        except (ValueError, KeyError, TypeError):
            raise AppError(
                "DOCKER_OUTPUT_INVALID", "Docker host metadata is invalid.", 409
            ) from None
        slots = min(
            2,
            self.settings.execution_max_parallel,
            math.floor(max(0, cpus - 1) / self.settings.execution_build_cpus),
            max(0, memory - 1024**3) // (self.settings.execution_build_memory_mb * 1024**2),
        )
        if (
            cpus - 1 < getattr(self.settings, f"execution_{phase}_cpus")
            or memory - 1024**3 < getattr(self.settings, f"execution_{phase}_memory_mb") * 1024**2
        ):
            raise AppError(
                "RESOURCE_LIMIT", f"Docker host lacks capacity for {phase} and reserve.", 409
            )
        version = await self._run(
            ["version", "--format", "{{.Server.Version}}"],
            machine_output=True,
            deadline=deadline,
        )
        self.capacity = Capacity(
            cpus, memory, str(info.get("CgroupVersion", "unknown")), slots, version
        )
        return self.capacity

    async def resolve_image(self, image: str, *, deadline: float | None = None) -> dict:
        try:
            return await self._inspect("image", image, deadline=deadline)
        except AppError as error:
            if "no such image" not in error.message.lower():
                raise
        await self._run(
            ["image", "pull", image], self.settings.build_timeout_seconds, deadline=deadline
        )
        return await self._inspect("image", image, deadline=deadline)

    async def _inspect(
        self,
        kind: str,
        name: str,
        *,
        ignore_cancel: bool = False,
        deadline: float | None = None,
    ) -> dict:
        prefix = (
            ["image", "inspect"]
            if kind == "image"
            else ["volume", "inspect"]
            if kind == "volume"
            else ["inspect"]
        )
        if kind == "volume":
            template = '{"Labels":{{json .Labels}}}'
        elif kind == "image":
            template = (
                '{"Id":{{json .Id}},"Config":{"Labels":{{json (index .Config "Labels")}},'
                '"Volumes":{{json (index .Config "Volumes")}}}}'
            )
        else:
            template = (
                '{"Id":{{json .Id}},"Image":{{json .Image}},'
                '"Config":{"Labels":{{json .Config.Labels}}},'
                '"Mounts":{{json .Mounts}},'
                '"HostConfig":{"Privileged":{{json .HostConfig.Privileged}},'
                '"Memory":{{.HostConfig.Memory}},"NanoCpus":{{.HostConfig.NanoCpus}},'
                '"PidsLimit":{{.HostConfig.PidsLimit}}},'
                '"NetworkSettings":{"Ports":{{json .NetworkSettings.Ports}}},'
                '"State":{"Running":{{json .State.Running}},'
                '"ExitCode":{{json .State.ExitCode}},"OOMKilled":{{json .State.OOMKilled}},'
                '"StartedAt":{{json .State.StartedAt}},"FinishedAt":{{json .State.FinishedAt}},'
                '"Error":{{json .State.Error}}}}'
            )
        output = await self._run(
            [*prefix, "--format", template, name],
            ignore_cancel=ignore_cancel,
            machine_output=True,
            deadline=deadline,
        )
        try:
            data = json.loads(output)
        except (ValueError, TypeError):
            raise AppError(
                "DOCKER_OUTPUT_INVALID", "Docker resource metadata is invalid.", 409
            ) from None
        if not isinstance(data, dict):
            raise AppError("DOCKER_OUTPUT_INVALID", "Docker resource metadata is invalid.", 409)
        if "State" in data:
            data["State"] = {
                key: value
                for key, value in data["State"].items()
                if key in {"Running", "ExitCode", "OOMKilled", "StartedAt", "FinishedAt", "Error"}
            }
            if isinstance(data["State"].get("Error"), str):
                data["State"]["Error"] = self.redact(data["State"]["Error"])[:1000]
        return data

    async def _verify_limits(
        self, name: str, memory: int, cpus: float, pids: int, deadline: float | None = None
    ) -> dict:
        data = await self._inspect("container", name, deadline=deadline)
        host = data["HostConfig"]
        if (
            host["Privileged"]
            or host["Memory"] != memory * 1024**2
            or host["NanoCpus"] != int(cpus * 1e9)
            or host["PidsLimit"] != pids
        ):
            raise AppError(
                "RESOURCE_LIMIT", "Docker did not enforce requested resource limits.", 409
            )
        return data

    async def _acquire_slot(self, phase: str = "build", *, deadline: float | None = None) -> None:
        if self._slot or self._acquiring:
            raise AppError(
                "RESOURCE_LIMIT",
                "Clean up this driver's active builder or runtime before creating another.",
                409,
            )
        self._acquiring = True
        phase_deadline = time.monotonic() + (
            self.settings.build_timeout_seconds
            if phase == "build"
            else self.settings.startup_timeout_seconds
        )
        deadline = min(deadline, phase_deadline) if deadline is not None else phase_deadline
        try:
            capacity = await self.preflight(phase, deadline=deadline)
            while True:
                if self.cancel.is_set():
                    raise AppError("CANCELLED", "Execution was cancelled.", 409)
                if time.monotonic() >= deadline:
                    raise AppError("RESOURCE_LIMIT", "Execution capacity admission timed out.", 409)
                token = _execution_budget.acquire(capacity, self.settings, phase)
                if token:
                    self._lease, self._phase, self._slot = token, phase, True
                    self._phase_deadline = deadline
                    self._build_deadline = deadline if phase == "build" else None
                    return
                try:
                    await asyncio.wait_for(self.cancel.wait(), timeout=0.1)
                except TimeoutError:
                    pass
        finally:
            self._acquiring = False

    async def builder(self, task: str) -> RuntimeHandle:
        await self._acquire_slot()
        try:
            builder = await self._create_builder(task)
            self._slot_resource = builder.id
            return builder
        except BaseException:
            await self.cleanup()
            raise

    @staticmethod
    def _created_id(output: str) -> str:
        candidate = output.splitlines()[-1] if output else ""
        if not re.fullmatch(r"[a-f0-9]{64}", candidate):
            raise AppError(
                "DOCKER_OPERATION_FAILED", "Docker returned an invalid container ID.", 409
            )
        return candidate

    async def _create_builder(self, task: str) -> RuntimeHandle:
        deadline = self._build_deadline
        suffix = uuid4().hex
        volume = await self._intent("volume", task, f"voxops-cache-{suffix}")
        await self._run(
            ["volume", "create", *self._label_args(task), volume.name], deadline=deadline
        )
        await self._active(volume, volume.name)
        item = await self._intent("container", task, f"voxops-builder-{suffix}")
        self._slot_resource = item.name
        s = self.settings
        image = await self.resolve_image(s.execution_buildkit_image, deadline=deadline)
        container_id = await self._run(
            [
                "create",
                "--name",
                item.name,
                *self._label_args(task),
                f"--memory={s.execution_build_memory_mb}m",
                f"--cpus={s.execution_build_cpus}",
                f"--pids-limit={s.execution_build_pids}",
                "--security-opt",
                "seccomp=unconfined",
                "--security-opt",
                "apparmor=unconfined",
                "--mount",
                f"type=volume,src={volume.name},dst=/home/user/.local/share/buildkit",
                image["Id"],
                "--oci-worker-no-process-sandbox",
                "--oci-worker-snapshotter=native",
            ],
            self.settings.build_timeout_seconds,
            machine_output=True,
            deadline=deadline,
        )
        container_id = self._created_id(container_id)
        await self._active(item, container_id)
        data = await self._verify_limits(
            container_id,
            s.execution_build_memory_mb,
            s.execution_build_cpus,
            s.execution_build_pids,
            deadline,
        )
        await self._run(["start", container_id], deadline=deadline)
        for _ in range(30):
            ready = await self.command(
                [
                    "docker",
                    "exec",
                    container_id,
                    "buildctl",
                    "--addr",
                    "unix:///run/user/1000/buildkit/buildkitd.sock",
                    "debug",
                    "workers",
                ],
                min(2, max(0, deadline - time.monotonic())),
            )
            if ready.exit_code == 0:
                version = await self._run(
                    ["exec", container_id, "buildctl", "--version"],
                    deadline=deadline,
                    machine_output=True,
                )
                return RuntimeHandle(
                    container_id,
                    item.name,
                    task,
                    build_deadline=deadline,
                    image_id=data["Image"],
                    tool_version=version,
                )
            if ready.cancelled:
                raise AppError("CANCELLED", "Execution was cancelled.", 409)
            if time.monotonic() >= deadline:
                raise AppError("TIMEOUT", "Builder preparation exceeded its build deadline.", 409)
            await asyncio.sleep(1)
        raise AppError(
            "BUILDKIT_ISOLATION_UNAVAILABLE",
            "Rootless BuildKit failed readiness with the required unconfined "
            "seccomp and AppArmor profiles.",
            409,
        )

    async def build(
        self,
        builder: RuntimeHandle,
        context: FrozenContext,
        dockerfile: str,
        session: SecretSession,
        unit: str,
    ) -> ImageHandle:
        try:
            return await self._build(builder, context, dockerfile, session, unit)
        except BaseException:
            await self.cleanup()
            raise

    async def _build(self, builder, context, dockerfile, session, unit) -> ImageHandle:
        path = PurePosixPath(dockerfile)
        if (
            path.is_absolute()
            or str(path) != dockerfile
            or ".." in path.parts
            or not re.fullmatch(r"[A-Za-z0-9_./-]+", dockerfile)
        ):
            raise AppError("invalid_context", "Dockerfile path is unsafe.", 409)
        self._require_owned(builder)
        if not self._slot or self._slot_resource != builder.id or builder.build_deadline is None:
            raise AppError("RESOURCE_LIMIT", "Build requires this driver's active builder.", 409)
        verify_context(context)
        deadline = builder.build_deadline
        secrets = session.values_for(unit, "build", "secret") | session.values_for(
            unit, "build", "unknown"
        )
        if secrets:
            try:
                definition = safe_read(context.path, dockerfile, context.max_file_bytes).decode()
            except (OSError, UnicodeError, AttributeError):
                raise AppError("invalid_context", "Dockerfile is unavailable.", 409) from None
            for name in secrets:
                # Deliberately accept only the unambiguous single-line Dockerfile form.
                pattern = (
                    rf"(?im)^[ \t]*RUN[ \t]+--mount=type=secret,id={re.escape(name)}(?:[, \t])"
                )
                if not re.search(pattern, definition):
                    raise AppError(
                        "BUILD_SECRET_INJECTION_UNSUPPORTED",
                        "Each build secret needs a supported RUN secret mount.",
                        409,
                    )
        task = builder.task_id
        tag = f"voxops-stage3:{uuid4().hex}"
        item = await self._intent("image", task, tag)
        verify_context(context)
        build_nonce = uuid4().hex
        internal_context = f"/tmp/context-{build_nonce}"
        internal_archive = f"/tmp/image-{build_nonce}.tar"
        await self._run(
            ["cp", str(context.path), f"{builder.id}:{internal_context}"], deadline=deadline
        )
        verify_context(context)
        args = [
            "exec",
            builder.id,
            "buildctl",
            "--addr",
            "unix:///run/user/1000/buildkit/buildkitd.sock",
            "build",
            "--frontend",
            "dockerfile.v0",
            "--local",
            f"context={internal_context}",
            "--local",
            f"dockerfile={internal_context}",
            "--opt",
            f"filename={dockerfile}",
        ]
        for key, value in self._labels(task).items():
            args += ["--opt", f"label:{key}={value}"]
        for key, value in session.values_for(unit, "build", "public").items():
            args += ["--opt", f"build-arg:{key}={value}"]
        args += ["--output", f"type=docker,name={tag},dest={internal_archive}"]
        secret_dir = f"/tmp/secrets-{build_nonce}"
        try:
            if secrets:
                await self._run(
                    ["exec", "--user", "0", builder.id, "mkdir", "-m", "700", secret_dir],
                    deadline=deadline,
                )
                with tempfile.TemporaryDirectory(prefix="voxops-build-secrets-") as tmp:
                    for name, value in secrets.items():
                        path = Path(tmp) / name
                        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                        with os.fdopen(fd, "w") as stream:
                            stream.write(value)
                        await self._run(
                            ["cp", str(path), f"{builder.id}:{secret_dir}/{name}"],
                            deadline=deadline,
                        )
                    await self._run(
                        ["exec", "--user", "0", builder.id, "chown", "-R", "1000:1000", secret_dir],
                        deadline=deadline,
                    )
                    for name in secrets:
                        args += ["--secret", f"id={name},src={secret_dir}/{name}"]
                    await self._run(args, self.settings.build_timeout_seconds, deadline=deadline)
            else:
                await self._run(args, self.settings.build_timeout_seconds, deadline=deadline)
        finally:
            if secrets:
                await self.command(
                    ["docker", "exec", "--user", "0", builder.id, "rm", "-rf", secret_dir],
                    ignore_cancel=True,
                )
        with tempfile.TemporaryDirectory(prefix="voxops-image-") as tmp:
            archive = Path(tmp) / "image.tar"
            await self._run(
                ["cp", f"{builder.id}:{internal_archive}", str(archive)],
                self.settings.build_timeout_seconds,
                deadline=deadline,
            )
            await self._run(
                ["load", "-i", str(archive)], self.settings.build_timeout_seconds, deadline=deadline
            )
        image = await self._inspect("image", tag, deadline=deadline)
        if any(
            (image["Config"].get("Labels") or {}).get(k) != v for k, v in self._labels(task).items()
        ):
            raise AppError(
                "DOCKER_OPERATION_FAILED", "Built image ownership labels do not match.", 409
            )
        await self._active(item, image["Id"])
        return ImageHandle(image["Id"], tag, task)

    async def runtime(
        self,
        image: str,
        task: str,
        argv: list[str],
        session: SecretSession,
        unit: str,
        port: int | None = None,
        workspace: FrozenContext | None = None,
        *,
        deadline: float | None = None,
    ) -> RuntimeHandle:
        await self._acquire_slot("runtime", deadline=deadline)
        try:
            return await self._runtime(image, task, argv, session, unit, port, workspace, False)
        except BaseException:
            await self.cleanup()
            raise

    async def _runtime(
        self,
        image,
        task,
        argv,
        session,
        unit,
        port,
        workspace,
        _build,
        *,
        workspace_volume=None,
        workdir="/workspace",
    ) -> RuntimeHandle:
        if _build and not self._slot:
            raise AppError("RESOURCE_LIMIT", "Workspace build requires a reserved build slot.", 409)
        deadline = self._build_deadline if _build else self._phase_deadline
        if _build and (
            session.values_for(unit, "build", "secret")
            or session.values_for(unit, "build", "unknown")
        ):
            raise AppError(
                "BUILD_SECRET_INJECTION_UNSUPPORTED",
                "Workspace builds do not support build secrets.",
                409,
            )
        s = self.settings
        memory = s.execution_build_memory_mb if _build else s.execution_runtime_memory_mb
        cpus = s.execution_build_cpus if _build else s.execution_runtime_cpus
        pids = s.execution_build_pids if _build else s.execution_runtime_pids
        args = [
            *self._label_args(task),
            f"--memory={memory}m",
            f"--cpus={cpus}",
            f"--pids-limit={pids}",
        ]
        # Image-declared volumes otherwise create unjournaled anonymous Docker volumes.
        image_info = await self.resolve_image(image, deadline=deadline)
        image = image_info["Id"]
        for mount in sorted(image_info["Config"].get("Volumes") or {}):
            if workspace_volume and (
                PurePosixPath(mount).is_relative_to("/workspace")
                or PurePosixPath("/workspace").is_relative_to(mount)
            ):
                raise AppError("invalid_context", "Image volumes conflict with the workspace.", 409)
            if (
                "," in mount
                or not PurePosixPath(mount).is_absolute()
                or ".." in PurePosixPath(mount).parts
            ):
                raise AppError("DOCKER_OUTPUT_INVALID", "Image volume path is unsafe.", 409)
            volume = await self._intent("volume", task, f"voxops-workspace-{uuid4().hex}")
            await self._run(
                ["volume", "create", *self._label_args(task), volume.name], deadline=deadline
            )
            await self._active(volume, volume.name)
            args += ["--mount", f"type=volume,src={volume.name},dst={mount}"]
        if port is not None:
            if not 1 <= port <= 65535:
                raise ValueError("Invalid container port")
            args += ["-p", f"127.0.0.1::{port}"]
        if workspace is not None or workspace_volume is not None:
            args += ["--workdir", workdir]
        if workspace_volume is not None:
            args += ["--mount", f"type=volume,src={workspace_volume},dst=/workspace,volume-nocopy"]
            if not _build:
                args += ["--env", "HOSTNAME=0.0.0.0", "--env", f"PORT={port}"]
        item = await self._intent("container", task, f"voxops-runtime-{uuid4().hex}")
        self._slot_resource = item.name
        args = ["create", "--name", item.name, *args]
        if _build:
            with tempfile.TemporaryDirectory(prefix="voxops-build-env-") as tmp:
                env = Path(tmp) / "env"
                fd = os.open(env, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "w") as stream:
                    for name, value in sorted(session.values_for(unit, "build", "public").items()):
                        stream.write(f"{name}={value}\n")
                args += ["--env-file", str(env), image, *argv]
                resource_id = await self._run(
                    args,
                    self.settings.build_timeout_seconds,
                    machine_output=True,
                    deadline=deadline,
                )
        else:
            with session.runtime_file(unit) as env:
                args += ["--env-file", str(env), image, *argv]
                resource_id = await self._run(
                    args,
                    self.settings.build_timeout_seconds,
                    machine_output=True,
                    deadline=deadline,
                )
        resource_id = self._created_id(resource_id)
        await self._active(item, resource_id)
        self._slot_resource = resource_id
        await self._verify_limits(resource_id, memory, cpus, pids, deadline)
        if workspace is not None:
            verify_context(workspace)
            await self._run(
                ["cp", str(workspace.path) + "/.", f"{resource_id}:/workspace"], deadline=deadline
            )
            verify_context(workspace)
        await self._run(["start", resource_id], deadline=deadline)
        host_port = None
        if port is not None:
            data = await self._inspect("container", resource_id, deadline=deadline)
            bindings = data["NetworkSettings"]["Ports"][f"{port}/tcp"]
            if len(bindings) != 1 or bindings[0]["HostIp"] != "127.0.0.1":
                raise AppError(
                    "DOCKER_OPERATION_FAILED", "Runtime port did not bind to localhost.", 409
                )
            host_port = int(bindings[0]["HostPort"])
        return RuntimeHandle(
            resource_id, item.name, task, host_port, deadline if _build else None, image_info["Id"]
        )

    async def workspace_builder(
        self, image: str, task: str, session: SecretSession, unit: str, workspace: FrozenContext
    ) -> WorkspaceHandle:
        await self._acquire_slot()
        try:
            if session.values_for(unit, "build", "secret") or session.values_for(
                unit, "build", "unknown"
            ):
                raise AppError(
                    "BUILD_SECRET_INJECTION_UNSUPPORTED",
                    "Workspace builds do not support build secrets.",
                    409,
                )
            volume = await self._intent("volume", task, f"voxops-workspace-{uuid4().hex}")
            await self._run(
                ["volume", "create", *self._label_args(task), volume.name],
                deadline=self._build_deadline,
            )
            await self._active(volume, volume.name)
            runtime = await self._runtime(
                image,
                task,
                ["sleep", "infinity"],
                session,
                unit,
                None,
                workspace,
                True,
                workspace_volume=volume.name,
            )
            return WorkspaceHandle(**vars(runtime), workspace_volume=volume.name, unit_id=unit)
        except BaseException:
            await self.cleanup()
            raise

    async def native_runtime(
        self,
        builder: WorkspaceHandle,
        task: str,
        argv: list[str],
        session: SecretSession,
        unit: str,
        port: int,
        *,
        workdir: str = "/workspace",
        deadline: float | None = None,
    ) -> RuntimeHandle:
        startup_deadline = time.monotonic() + self.settings.startup_timeout_seconds
        deadline = min(deadline, startup_deadline) if deadline is not None else startup_deadline
        try:
            if not isinstance(builder, WorkspaceHandle) or builder.unit_id != unit:
                raise AppError("ownership_mismatch", "Native workspace unit does not match.", 409)
            path = PurePosixPath(workdir)
            if str(path) != workdir or ".." in path.parts or not path.is_relative_to("/workspace"):
                raise AppError("invalid_context", "Native working directory is unsafe.", 409)
            self._require_owned(builder)
            volume = next(
                (
                    item
                    for item in self.resources
                    if (
                        item.kind == "volume"
                        and item.name == builder.workspace_volume
                        and item.task_id == builder.task_id
                        and item.ownership_token == self.owner
                        and item.state == "active"
                        and item.resource_id == item.name
                    )
                ),
                None,
            )
            if volume is None or not builder.image_id:
                raise AppError("ownership_mismatch", "Native workspace volume is not owned.", 409)
            if builder.build_deadline is None:
                raise AppError("TIMEOUT", "Native builder deadline is unavailable.", 409)
            handoff_deadline = min(deadline, builder.build_deadline)
            metadata = await self._inspect("container", builder.id, deadline=handoff_deadline)
            volume_info = await self._inspect("volume", volume.name, deadline=handoff_deadline)
            if (
                metadata.get("Id") != builder.id
                or metadata.get("Image") != builder.image_id
                or any(
                    metadata.get("Config", {}).get("Labels", {}).get(k) != v
                    for k, v in self._labels(builder.task_id).items()
                )
                or any(
                    (volume_info.get("Labels") or {}).get(k) != v
                    for k, v in self._labels(builder.task_id).items()
                )
                or not any(
                    m.get("Type") == "volume"
                    and m.get("Name") == volume.name
                    and m.get("Destination") == "/workspace"
                    for m in metadata.get("Mounts", [])
                )
            ):
                raise AppError("ownership_mismatch", "Native workspace ownership changed.", 409)
            await self.cleanup(
                [item for item in self.resources if item.resource_id == builder.id],
                deadline=handoff_deadline,
            )
            if time.monotonic() >= handoff_deadline:
                raise AppError("TIMEOUT", "Native handoff exceeded its deadline.", 409)
            await self._acquire_slot("runtime", deadline=deadline)
            return await self._runtime(
                builder.image_id,
                task,
                argv,
                session,
                unit,
                port,
                None,
                False,
                workspace_volume=volume.name,
                workdir=workdir,
            )
        except BaseException:
            await self.cleanup()
            raise

    def _require_owned(self, runtime: RuntimeHandle) -> None:
        if not any(
            item.kind == "container"
            and item.resource_id == runtime.id
            and item.name == runtime.name
            and item.task_id == runtime.task_id
            and item.ownership_token == self.owner
            and item.state == "active"
            for item in self.resources
        ):
            raise AppError(
                "ownership_mismatch", "Container is not active in this driver's journal.", 409
            )

    async def exec(
        self,
        runtime: RuntimeHandle,
        argv: list[str],
        timeout: float,
        *,
        workdir: str | None = "/workspace",
    ) -> CommandOutcome:
        self._require_owned(runtime)
        if not argv:
            raise ValueError("Runtime command cannot be empty")
        if runtime.build_deadline is not None:
            timeout = min(timeout, max(0, runtime.build_deadline - time.monotonic()))
        args = ["docker", "exec"]
        if workdir is not None:
            args += ["--workdir", workdir]
        try:
            outcome = await self.command([*args, runtime.id, *argv], timeout)
        except BaseException:
            await self.cleanup([item for item in self.resources if item.resource_id == runtime.id])
            raise
        if outcome.cancelled or outcome.timed_out:
            owned = [item for item in self.resources if item.resource_id == runtime.id]
            await self.cleanup(owned)
        return outcome

    async def logs(self, runtime: RuntimeHandle) -> CommandOutcome:
        self._require_owned(runtime)
        owned = [item for item in self.resources if item.resource_id == runtime.id]
        try:
            outcome = await self.command(["docker", "logs", runtime.id])
        except BaseException:
            await self.cleanup(owned)
            raise
        if outcome.cancelled or outcome.timed_out:
            await self.cleanup(owned)
        return outcome

    async def inspect_runtime(self, runtime: RuntimeHandle) -> dict:
        self._require_owned(runtime)
        return (await self._inspect("container", runtime.id))["State"]

    async def copy_output(self, runtime: RuntimeHandle, source: str, destination: Path) -> None:
        self._require_owned(runtime)
        path = PurePosixPath(source)
        if not path.is_absolute() or not path.is_relative_to("/workspace") or ".." in path.parts:
            raise ValueError("Output must be inside the owned workspace")
        await self._run(
            ["cp", f"{runtime.id}:{source}", str(destination)], deadline=runtime.build_deadline
        )

    async def cleanup(
        self,
        resources: list[OwnedResourceIntent] | None = None,
        *,
        deadline: float | None = None,
    ) -> None:
        errors = []
        absent_creates = set()
        owned = resources if resources is not None else self.resources
        for item in sorted(reversed(owned), key=lambda item: item.kind != "container"):
            if (
                item.kind not in {"container", "image", "volume"}
                or item.ownership_token != self.owner
                or item.state == "removed"
            ):
                continue
            if item.kind == "volume" and any(
                container.kind == "container" and container.state != "removed"
                for container in self.resources
            ):
                continue
            name = item.resource_id or item.name
            try:
                data = await self._inspect(item.kind, name, ignore_cancel=True, deadline=deadline)
            except AppError as error:
                if "no such" not in error.message.lower():
                    errors.append(error)
                elif item.resource_id is None and item.state == "creating":
                    # A timed-out daemon create may finish after this absence query.
                    absent_creates.add(item.name)
                else:
                    item.state = "removed"
                    await self._record(item)
                continue
            labels = (
                data.get("Labels")
                if item.kind == "volume"
                else data.get("Config", {}).get("Labels")
            )
            if (
                labels is None
                or any(labels.get(k) != v for k, v in self._labels(item.task_id).items())
                or (
                    item.resource_id
                    and item.kind != "volume"
                    and data.get("Id") != item.resource_id
                )
            ):
                errors.append(
                    AppError(
                        "ownership_mismatch",
                        "Refusing to remove a resource with different ownership.",
                        409,
                    )
                )
                continue
            item.state = "removing"
            await self._record(item)
            target = data.get("Id", name)
            command = (
                ["rm", "-f", target] if item.kind == "container" else [item.kind, "rm", target]
            )
            try:
                await self._run(command, ignore_cancel=True, deadline=deadline)
                try:
                    await self._inspect(item.kind, name, ignore_cancel=True, deadline=deadline)
                except AppError as missing:
                    if "no such" not in missing.message.lower():
                        raise
                else:
                    raise AppError(
                        "DOCKER_OPERATION_FAILED",
                        "Docker resource still exists after removal.",
                        409,
                    )
            except AppError as error:
                errors.append(error)
                continue
            item.state = "removed"
            await self._record(item)
        if self._slot and (
            self._slot_resource is None
            or self._slot_resource in absent_creates
            or any(
                self._slot_resource in {item.resource_id, item.name} and item.state == "removed"
                for item in self.resources
            )
        ):
            if self._lease:
                _execution_budget.release(self._lease)
            self._lease = self._phase = None
            self._slot = False
            self._slot_resource = None
            self._build_deadline = None
            self._phase_deadline = None
        if errors:
            raise errors[0]
