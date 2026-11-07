import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from voxops.core.errors import AppError
from voxops.core.settings import Settings
from voxops.domain.execution_models import (
    EnvConsumer,
    EnvFormSchema,
    EnvRequirement,
    OwnedResourceIntent,
)
from voxops.execution.contexts import freeze_context, remove_context, verify_context
from voxops.execution.docker_driver import CommandOutcome, DockerDriver, RuntimeHandle
from voxops.execution.environment import SecretSession


def test_context_selection_excludes_private_and_tracks_modes(tmp_path):
    (tmp_path / "frontend").mkdir()
    (tmp_path / "backend").mkdir()
    (tmp_path / "frontend" / "app.js").write_text("hello")
    (tmp_path / "frontend" / ".env.local").write_text("secret")
    (tmp_path / "backend" / "Dockerfile").write_text("FROM alpine")
    first = freeze_context(tmp_path, selected=("frontend",), required=("frontend/app.js",))
    try:
        assert first.files == ("frontend/app.js",)
        assert not (first.path / "frontend" / ".env.local").exists()
        (tmp_path / "backend" / "Dockerfile").write_text("changed")
        second = freeze_context(tmp_path, selected=("frontend",))
        try:
            assert first.identity == second.identity
        finally:
            remove_context(second)
        (tmp_path / "frontend" / "app.js").chmod(0o755)
        second = freeze_context(tmp_path, selected=("frontend",))
        try:
            assert first.identity != second.identity
        finally:
            remove_context(second)
    finally:
        remove_context(first)


def test_context_rejects_missing_required_and_symlink(tmp_path):
    (tmp_path / "file").write_text("x")
    with pytest.raises(AppError):
        freeze_context(tmp_path, required=("missing",))
    (tmp_path / "link").symlink_to(tmp_path / "file")
    with pytest.raises(AppError):
        freeze_context(tmp_path)


def test_capture_rejects_added_input_and_removes_private_partial_copy(tmp_path, monkeypatch):
    import voxops.execution.contexts as contexts

    source = tmp_path / "source"
    source.mkdir()
    (source / "first").write_text("input")
    original = contexts.safe_read

    def changing_read(*args):
        data = original(*args)
        (source / "added").write_text("new input")
        return data

    monkeypatch.setattr(contexts, "safe_read", changing_read)
    with pytest.raises(AppError, match="changed during capture"):
        freeze_context(source, parent=tmp_path)
    assert not list(tmp_path.glob("voxops-context-*"))


def test_context_cleanup_does_not_chmod_symlink_targets(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "input").write_text("input")
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o500)
    context = freeze_context(source)
    context.path.chmod(0o700)
    (context.path / "link").symlink_to(outside, target_is_directory=True)
    remove_context(context)
    remove_context(context)
    assert outside.stat().st_mode & 0o777 == 0o500
    assert not context.path.exists()


def test_output_redacts_split_secrets_and_drops_oversize_line(tmp_path, monkeypatch):
    executable = tmp_path / "docker"
    executable.write_text(
        '#!/usr/bin/python3\nimport os, time\nos.write(1,b"pass-" )\ntime.sleep(.01)\n'
        'os.write(1,b"word\\n" + b"A" * 2000 + b"password\\nend\\n")\n'
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])

    async def run():
        driver = DockerDriver(
            Settings(execution_log_max_bytes=1024),
            "run",
            "owner",
            lambda _: None,
            asyncio.Event(),
            lambda text: text.replace("pass-word", "[REDACTED]").replace("password", "[REDACTED]"),
        )
        result = await driver.command(["docker"])
        assert result.exit_code == 0
        assert result.output == "[REDACTED]\nend\n"

    asyncio.run(run())


def test_refuses_foreign_resource_cleanup():
    async def run():
        driver = DockerDriver(
            Settings(), "run", "owner", lambda _: None, asyncio.Event(), lambda s: s
        )
        item = OwnedResourceIntent(
            kind="container", name="name", task_id="task", ownership_token="owner"
        )

        async def foreign(*args, **kwargs):
            return {"Config": {"Labels": {"owner": "someone-else"}}}

        driver._inspect = foreign
        with pytest.raises(AppError) as error:
            await driver.cleanup([item])
        assert error.value.category == "ownership_mismatch"

    asyncio.run(run())


def test_timeout_during_cleanup_does_not_reenter_cleanup():
    async def run():
        from voxops.execution.docker_driver import CommandOutcome

        driver = DockerDriver(
            Settings(), "run", "owner", lambda _: None, asyncio.Event(), lambda s: s
        )
        item = OwnedResourceIntent(
            kind="container", name="name", task_id="task", ownership_token="owner"
        )
        calls = []

        async def timeout(*args, **kwargs):
            calls.append(args)
            return CommandOutcome(None, "", 0, timed_out=True)

        driver.command = timeout
        with pytest.raises(AppError) as error:
            await driver.cleanup([item])
        assert error.value.category == "TIMEOUT"
        assert len(calls) == 1
        assert item.state == "creating"

    asyncio.run(run())


def test_exec_timeout_cleans_its_container():
    async def run():
        from voxops.execution.docker_driver import CommandOutcome, RuntimeHandle

        driver = DockerDriver(
            Settings(), "run", "owner", lambda _: None, asyncio.Event(), lambda s: s
        )
        item = OwnedResourceIntent(
            kind="container",
            name="name",
            resource_id="abc",
            task_id="task",
            ownership_token="owner",
            state="active",
        )
        driver.resources.append(item)
        cleaned = []

        async def timeout(*args, **kwargs):
            return CommandOutcome(None, "", 0, timed_out=True)

        async def cleanup(resources):
            cleaned.extend(resources)

        driver.command = timeout
        driver.cleanup = cleanup
        result = await driver.exec(RuntimeHandle("abc", "name", "task"), ["npm", "run", "build"], 1)
        assert result.timed_out
        assert cleaned == [item]

    asyncio.run(run())


class SimulatedDocker(DockerDriver):
    """Deterministic Docker boundary for unit tests, never a product execution mode."""

    def __init__(self, settings=None, cancel=None):
        self.events = []
        self.removed = []
        self.created = {}
        self.image_volumes = {}
        self.clock_step = 0
        self.now = 0
        super().__init__(
            settings or Settings(),
            "run",
            "owner",
            lambda item: None,
            cancel or asyncio.Event(),
            lambda s: s,
        )

    async def command(self, args, timeout=30, **kwargs):
        self.events.append((args, timeout))
        self.now += self.clock_step
        if timeout <= 0:
            return CommandOutcome(None, "", 0, timed_out=True)
        output = ""
        if args[1] == "info":
            output = json.dumps({"NCPU": 8, "MemTotal": 8 * 1024**3, "CgroupVersion": "2"})
        elif args[1] == "version":
            output = "29.test"
        elif args[1] == "create":
            name = args[args.index("--name") + 1]
            output = f"{len(self.created) + 1:064x}"
            self.created[name] = (output, args)
        elif "inspect" in args:
            name = args[-1]
            item = next(
                (r for r in self.resources if r.name == name or r.resource_id == name), None
            )
            if args[1] == "image" and item is None:
                data = {"Id": "sha256:stock", "Config": {"Volumes": self.image_volumes}}
            elif item is None or item.name in self.removed:
                return CommandOutcome(1, "No such resource", 0)
            elif item.kind == "container" and item.name not in self.created:
                return CommandOutcome(1, "No such resource", 0)
            elif item.kind == "volume":
                data = {"Labels": self._labels(item.task_id)}
            elif item.kind == "image":
                data = {"Id": "sha256:built", "Config": {"Labels": self._labels(item.task_id)}}
            else:
                resource_id, create = self.created[item.name]
                data = {
                    "Id": resource_id,
                    "Image": "sha256:stock",
                    "Config": {"Labels": self._labels(item.task_id)},
                    "HostConfig": {
                        "Privileged": False,
                        "Memory": int(next(a[9:-1] for a in create if a.startswith("--memory=")))
                        * 1024**2,
                        "NanoCpus": int(
                            float(next(a[7:] for a in create if a.startswith("--cpus="))) * 1e9
                        ),
                        "PidsLimit": int(
                            next(a[13:] for a in create if a.startswith("--pids-limit="))
                        ),
                    },
                }
            output = json.dumps(data)
        elif args[1] == "rm" or args[1:3] in (["volume", "rm"], ["image", "rm"]):
            self.removed.extend(
                r.name for r in self.resources if args[-1] in {r.name, r.resource_id}
            )
        elif args[1] == "exec" and args[-1] == "--version":
            output = "buildctl test"
        return CommandOutcome(0, output, 0, _stdout=output)


@pytest.mark.parametrize("added", ["new", ".env.local", ".git/hidden"])
def test_frozen_inventory_detects_added_file_and_nested_repository(tmp_path, added):
    (tmp_path / "file").write_text("x")
    nested = tmp_path / "subrepo"
    nested.mkdir()
    (nested / ".git").write_text("gitdir: elsewhere")
    (nested / "private-source").write_text("excluded")
    context = freeze_context(tmp_path)
    try:
        assert context.files == ("file",)
        context.path.chmod(0o700)
        target = context.path / added
        target.parent.mkdir(exist_ok=True)
        target.write_text("tampered")
        with pytest.raises(AppError, match="Frozen build input changed"):
            verify_context(context)
    finally:
        remove_context(context)
    with pytest.raises(AppError, match="Required build inputs"):
        freeze_context(tmp_path, required=("subrepo/private-source",))


def test_machine_stdout_survives_redactor_and_missing_newline(tmp_path, monkeypatch):
    executable = tmp_path / "docker"
    executable.write_text(
        '#!/usr/bin/python3\nimport os\nos.write(1,b"abcdef")\nos.write(2,b"warning\\n")'
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])

    async def run():
        driver = DockerDriver(
            Settings(),
            "run",
            "owner",
            lambda _: None,
            asyncio.Event(),
            lambda text: text.replace("abcdef", "[REDACTED]"),
        )
        outcome = await driver.command(["docker"], machine_output=True)
        assert outcome._stdout == "abcdef"
        assert outcome.output == "warning\n"

    asyncio.run(run())


def test_cli_deadline_includes_pipe_drain_and_kills_child_process_group(tmp_path, monkeypatch):
    executable = tmp_path / "docker"
    leftover = tmp_path / "leftover"
    executable.write_text(
        "#!/usr/bin/python3\nimport os,time\nfrom pathlib import Path\n"
        'if os.fork() == 0:\n time.sleep(.3)\n Path("'
        + str(leftover)
        + '").write_text("escaped")\nelse:\n os._exit(0)\n'
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])

    async def run():
        driver = SimulatedDocker()
        driver.command = DockerDriver.command.__get__(driver)
        outcome = await driver.command(["docker"], timeout=0.03)
        assert outcome.timed_out
        await asyncio.sleep(0.35)
        assert not leftover.exists()

    asyncio.run(run())


def test_metadata_errors_redact_after_json_decode_without_health_logs(tmp_path, monkeypatch):
    secret = 'credential-with-"quote-and-\\slash'
    metadata = {
        "State": {"Running": False, "Error": secret, "Health": {"Log": [{"Output": secret}]}}
    }
    payload = json.dumps(metadata)
    executable = tmp_path / "docker"
    executable.write_text("#!/usr/bin/python3\nprint(" + repr(payload) + ")\n")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])

    async def run():
        driver = DockerDriver(
            Settings(),
            "run",
            "owner",
            lambda _: None,
            asyncio.Event(),
            lambda text: text.replace(secret, "[REDACTED]"),
        )
        outcome = await driver.command(["docker"], machine_output=True)
        assert not outcome.output
        assert secret not in repr(outcome) and payload not in repr(outcome)
        data = await driver._inspect("container", "owned")
        assert data == {"State": {"Running": False, "Error": "[REDACTED]"}}

    asyncio.run(run())


def test_cleanup_retains_uncertain_create_for_later_reconciliation_and_releases_budget():
    async def run():
        driver = SimulatedDocker()
        await driver._acquire_slot()
        item = await driver._intent("container", "task", "uncertain-create")
        driver._slot_resource = item.name
        await driver.cleanup()
        assert item.state == "creating"
        assert not driver._slot
        await driver._acquire_slot()
        await driver.cleanup()
        late_id = "a" * 64
        driver.created[item.name] = (
            late_id,
            ["create", "--memory=3072m", "--cpus=2.0", "--pids-limit=512"],
        )
        await driver.cleanup()
        assert item.state == "removed"
        assert any(args[1:] == ["rm", "-f", late_id] for args, _ in driver.events)

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["cancelled", "timed_out"])
def test_interrupted_log_read_removes_its_owned_container(failure):
    async def run():
        driver = SimulatedDocker()
        session = SecretSession(EnvFormSchema(source_digest="0" * 64))
        runtime = await driver.runtime("alpine", "task", ["sleep", "20"], session, "unit")
        original = driver.command

        async def interrupted(args, *positional, **kwargs):
            if args[1] == "logs":
                return CommandOutcome(None, "", 0, **{failure: True})
            return await original(args, *positional, **kwargs)

        driver.command = interrupted
        result = await driver.logs(runtime)
        assert getattr(result, failure)
        assert all(item.state == "removed" for item in driver.resources)
        assert runtime.name in driver.removed

    asyncio.run(run())


def test_shared_capacity_accounts_for_different_policies_and_cancellation():
    small = SimulatedDocker(Settings(execution_build_memory_mb=2048))
    asyncio.run(small._acquire_slot())

    async def run():
        large = SimulatedDocker(Settings(execution_build_memory_mb=6144))
        pending = asyncio.create_task(large._acquire_slot())
        try:
            await asyncio.sleep(0.02)
            assert not pending.done()
            large.cancel.set()
            with pytest.raises(AppError, match="cancelled"):
                await pending
            assert small._slot and not large._slot
        finally:
            await small.cleanup()

    asyncio.run(run())


def test_one_driver_cannot_create_second_heavy_container():
    async def run():
        driver = SimulatedDocker()
        builder = await driver.builder("task")
        try:
            count = len(driver.created)
            with pytest.raises(AppError, match="active builder"):
                await driver.builder("task2")
            with pytest.raises(AppError, match="active builder"):
                await driver.workspace_builder("node", "task2", None, "unit", None)
            assert len(driver.created) == count
            assert driver._slot_resource == builder.id
        finally:
            await driver.cleanup()

    asyncio.run(run())


def test_builder_refuses_unenforced_limits_before_project_code():
    async def run():
        driver = SimulatedDocker()
        original = driver.command

        async def no_memory_limit(args, *positional, **kwargs):
            outcome = await original(args, *positional, **kwargs)
            if args[1] == "inspect" and outcome.exit_code == 0:
                metadata = json.loads(outcome._stdout)
                metadata["HostConfig"]["Memory"] = 0
                return CommandOutcome(0, "", 0, _stdout=json.dumps(metadata))
            return outcome

        driver.command = no_memory_limit
        with pytest.raises(AppError, match="did not enforce"):
            await driver.builder("task")
        assert not any(args[1] == "exec" for args, _ in driver.events)
        assert all(item.state == "removed" for item in driver.resources)
        assert not driver._slot

    asyncio.run(run())


def test_runtime_and_workspace_build_use_separate_private_scoped_env_files(tmp_path):
    (tmp_path / "app").write_text("input")
    context = freeze_context(tmp_path)
    schema = EnvFormSchema(
        source_digest="0" * 64,
        requirements=[
            EnvRequirement(
                field_id="build",
                name="PUBLIC",
                visibility="public",
                necessity="required",
                consumers=[EnvConsumer(unit_id="unit", phase="build")],
            ),
            EnvRequirement(
                field_id="runtime",
                name="TOKEN",
                visibility="secret",
                necessity="required",
                consumers=[EnvConsumer(unit_id="unit", phase="runtime")],
            ),
            EnvRequirement(
                field_id="default",
                name="DEFAULT",
                visibility="secret",
                necessity="optional",
                consumers=[EnvConsumer(unit_id="unit", phase="runtime")],
            ),
        ],
    )
    session = SecretSession(
        schema,
        {"build": "client-config", "runtime": "private-config"},
        {"default": "default-is-not-injected"},
    )

    async def run():
        driver = SimulatedDocker()
        original = driver.command
        files = []

        async def capture(args, *positional, **kwargs):
            if args[1] == "create":
                path = Path(args[args.index("--env-file") + 1])
                files.append((path, path.read_text(), path.stat().st_mode & 0o777))
            return await original(args, *positional, **kwargs)

        driver.command = capture
        try:
            await driver.workspace_builder("node", "build", session, "unit", context)
            await driver.runtime("node", "runtime", ["sleep", "20"], session, "unit")
            assert [content for _, content, _ in files] == [
                "PUBLIC=client-config\n",
                "TOKEN=private-config\n",
            ]
            assert all(mode == 0o600 and not path.exists() for path, _, mode in files)
            assert not any("private-config" in str(args) for args, _ in driver.events)
        finally:
            await driver.cleanup()

    try:
        asyncio.run(run())
    finally:
        remove_context(context)


def test_runtime_journals_image_volumes_and_uses_runtime_limits():
    async def run():
        driver = SimulatedDocker()
        driver.image_volumes = {"/data": {}}
        session = SecretSession(EnvFormSchema(source_digest="0" * 64))
        handle = await driver.runtime("alpine", "task", ["sleep", "20"], session, "unit")
        args = driver.created[handle.name][1]
        assert "--memory=512m" in args and "--cpus=1.0" in args and "--pids-limit=128" in args
        volume = next(r for r in driver.resources if r.kind == "volume")
        assert f"type=volume,src={volume.name},dst=/data" in args
        assert not any("type=bind" in arg for arg in args)
        await driver.cleanup()
        assert driver.removed.index(handle.name) < driver.removed.index(volume.name)
        assert all(r.state == "removed" for r in driver.resources)

    asyncio.run(run())


@pytest.mark.parametrize(
    "dockerfile", ["/etc/passwd", "../Dockerfile", "./Dockerfile", "a/../Dockerfile"]
)
def test_build_rejects_noncanonical_dockerfile_before_host_read(tmp_path, dockerfile):
    (tmp_path / "Dockerfile").write_text("FROM alpine")
    context = freeze_context(tmp_path)

    async def run():
        driver = SimulatedDocker()
        with pytest.raises(AppError, match="path is unsafe"):
            await driver.build(
                RuntimeHandle("unused", "unused", "task"), context, dockerfile, None, "unit"
            )
        assert not driver.events

    try:
        asyncio.run(run())
    finally:
        remove_context(context)


def test_build_deadline_covers_context_build_export_and_load(tmp_path, monkeypatch):
    (tmp_path / "Dockerfile").write_text("FROM alpine")
    context = freeze_context(tmp_path)

    async def run():
        driver = SimulatedDocker(Settings(build_timeout_seconds=10))
        builder = await driver.builder("task")
        driver.now = time.monotonic()
        monkeypatch.setattr("voxops.execution.docker_driver.time.monotonic", lambda: driver.now)
        driver.clock_step = 4
        session = SecretSession(EnvFormSchema(source_digest="0" * 64))
        with pytest.raises(AppError, match="timed out"):
            await driver.build(builder, context, "Dockerfile", session, "unit")
        build_calls = [
            (args, timeout) for args, timeout in driver.events if args[1] in {"cp", "exec", "load"}
        ]
        assert any(args[1] == "load" and timeout == 0 for args, timeout in build_calls)
        assert not driver._slot
        assert all(r.state == "removed" for r in driver.resources)

    try:
        asyncio.run(run())
    finally:
        remove_context(context)


@pytest.mark.parametrize(
    "definition,supported",
    [
        ("FROM alpine\nRUN true\n", False),
        (
            "FROM alpine\nRUN --mount=type=secret,id=TOKEN cat /run/secrets/TOKEN >/dev/null\n",
            True,
        ),
        ("FROM alpine\nRUN\n--mount=type=secret,id=TOKEN cat /run/secrets/TOKEN\n", False),
    ],
)
def test_build_secrets_use_private_files_with_explicit_mounts(tmp_path, definition, supported):
    (tmp_path / "Dockerfile").write_text(definition)
    context = freeze_context(tmp_path)
    schema = EnvFormSchema(
        source_digest="0" * 64,
        requirements=[
            EnvRequirement(
                field_id="token",
                name="TOKEN",
                necessity="required",
                visibility="secret",
                consumers=[EnvConsumer(unit_id="unit", phase="build")],
            )
        ],
    )
    session = SecretSession(schema, {"token": "disposable-sensitive-value"})

    async def run():
        driver = SimulatedDocker()
        original = driver.command
        copied = []

        async def capture(args, *positional, **kwargs):
            if args[1] == "cp" and Path(args[2]).is_file():
                path = Path(args[2])
                copied.append((path, path.stat().st_mode & 0o777, path.read_text()))
            return await original(args, *positional, **kwargs)

        driver.command = capture
        builder = await driver.builder("task")
        try:
            if supported:
                image = await driver.build(builder, context, "Dockerfile", session, "unit")
                assert image.id == "sha256:built"
                assert copied and all(mode == 0o600 for _, mode, _ in copied)
                assert copied[0][2] == "disposable-sensitive-value"
                assert all(not path.exists() for path, _, _ in copied)
                build = next(args for args, _ in driver.events if "dockerfile.v0" in args)
                assert "--secret" in build
            else:
                with pytest.raises(AppError) as error:
                    await driver.build(builder, context, "Dockerfile", session, "unit")
                assert error.value.category == "BUILD_SECRET_INJECTION_UNSUPPORTED"
                assert not copied
                assert not any("dockerfile.v0" in args for args, _ in driver.events)
            assert not any("disposable-sensitive-value" in str(args) for args, _ in driver.events)
        finally:
            await driver.cleanup()

    try:
        asyncio.run(run())
    finally:
        remove_context(context)
