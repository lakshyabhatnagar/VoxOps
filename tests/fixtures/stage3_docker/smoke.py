"""Opt-in disposable Docker smoke; run with PYTHONPATH=src."""

import asyncio
import tempfile
from pathlib import Path
from uuid import uuid4

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
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Dockerfile").write_text(
                'FROM alpine:3.21\nRUN printf "driver-smoke-ok\\n" > /proof\n'
                'CMD ["cat", "/proof"]\n'
            )
            context = freeze_context(root, required=("Dockerfile",))
            try:
                builder = await driver.builder("smoke")
                image = await driver.build(builder, context, "Dockerfile", session, "smoke")
                builder_limits = (await driver._inspect("container", builder.id))["HostConfig"]
                await driver.cleanup([item for item in driver.resources if item.kind != "image"])
                runtime = await driver.runtime(image.id, "smoke", [], session, "smoke")
                await driver._run(["wait", runtime.id], 30)
                result = await driver.logs(runtime)
                assert "driver-smoke-ok" in result.output
                print("builder", builder.id, "image", image.id, "runtime", runtime.id)
                print(
                    "limits",
                    builder_limits,
                    (await driver._inspect("container", runtime.id))["HostConfig"],
                )
            finally:
                remove_context(context)
        await driver.cleanup()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(
                '{"name":"smoke","version":"1.0.0","type":"module",'
                '"scripts":{"build":"vite build"},"devDependencies":{"vite":"5.4.21"}}'
            )
            (root / "index.html").write_text('<div id="app">Vite smoke</div>')
            context = freeze_context(root, required=("package.json", "index.html"))
            try:
                runtime = await driver.workspace_builder(
                    driver.settings.execution_node_image, "vite", session, "vite", context
                )
                print(
                    "workspace limits",
                    (await driver._inspect("container", runtime.id))["HostConfig"],
                )
                install = await driver.exec(
                    runtime, ["npm", "install", "--no-audit", "--no-fund"], 180
                )
                assert install.exit_code == 0, install.output
                build = await driver.exec(runtime, ["npm", "run", "build"], 180)
                assert build.exit_code == 0, build.output
                output = Path(tmp) / "output"
                await driver.copy_output(runtime, "/workspace/dist", output)
                assert "Vite smoke" in (output / "index.html").read_text()
                print("Vite build passed without production Dockerfile", runtime.id)
            finally:
                remove_context(context)
        await driver.cleanup()
        await driver._inspect("volume", sentinel)
        for item in driver.resources:
            assert item.state == "removed", item
        print("owned cleanup passed; sentinel preserved", sentinel)
    finally:
        await driver.cleanup()
        await driver._run(["volume", "rm", sentinel], ignore_cancel=True)


asyncio.run(main())
