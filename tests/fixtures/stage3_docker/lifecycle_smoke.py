"""Opt-in cached-image cancellation proof. Requires access to the Docker daemon."""

import asyncio
import tempfile
from pathlib import Path
from uuid import uuid4

from voxops.core.errors import AppError
from voxops.core.settings import Settings
from voxops.domain.execution_models import EnvFormSchema
from voxops.execution.contexts import freeze_context, remove_context
from voxops.execution.docker_driver import DockerDriver
from voxops.execution.environment import SecretSession


async def main():
    token = uuid4().hex
    session = SecretSession(EnvFormSchema(source_digest="0" * 64))
    driver = DockerDriver(
        Settings(), token, token, lambda item: None, asyncio.Event(), session.redact
    )
    sentinel = "voxops-sentinel-" + token
    await driver._run(["volume", "create", sentinel])
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "input.txt").write_text("lifecycle proof")
            context = freeze_context(root)
            try:
                for kind in ("timeout", "cancel"):
                    driver.cancel.clear()
                    runtime = await driver.workspace_builder(
                        driver.settings.execution_node_image, kind, session, kind, context
                    )
                    if kind == "cancel":
                        asyncio.get_running_loop().call_later(0.2, driver.cancel.set)
                    outcome = await driver.exec(
                        runtime,
                        ["node", "-e", "setTimeout(() => {}, 60000)"],
                        0.2 if kind == "timeout" else 30,
                    )
                    assert getattr(outcome, "timed_out" if kind == "timeout" else "cancelled")
                    try:
                        await driver._inspect("container", runtime.id, ignore_cancel=True)
                    except AppError as error:
                        assert "no such" in error.message.lower()
                    else:
                        raise AssertionError("Timed-out project process container still exists")
                    await driver._inspect("volume", sentinel, ignore_cancel=True)
                    print(kind, "container removed", runtime.id, "sentinel retained", sentinel)
                    await driver.cleanup()
                    assert not driver._slot
            finally:
                remove_context(context)
    finally:
        await driver.cleanup()
        await driver._run(["volume", "rm", sentinel], ignore_cancel=True)
    assert all(item.state == "removed" for item in driver.resources)
    print("all owned lifecycle resources removed", token)


asyncio.run(main())
