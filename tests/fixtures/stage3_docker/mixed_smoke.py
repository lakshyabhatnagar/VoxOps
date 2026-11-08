"""Opt-in real mixed-monorepo proof, including shared-package build inputs."""

import asyncio
import json
import tempfile
from pathlib import Path
from uuid import uuid4

from frameworks_smoke import dockerfile, fixture

from voxops.artifacts.planner import build_plan
from voxops.core.settings import Settings
from voxops.domain.execution_models import EnvFormSchema, ProbeSpec
from voxops.execution.contexts import remove_context
from voxops.execution.docker_driver import DockerDriver
from voxops.execution.environment import SecretSession
from voxops.execution.planner import build_execution_plan, freeze_plan_contexts
from voxops.execution.runner import ExecutionRunner
from voxops.repository.snapshot import capture_source
from voxops.storage.scan_store import ScanStore
from voxops.workflows.stage1_graph import Stage1Workflow


async def main():
    owner = uuid4().hex
    print("START mixed-monorepo owner", owner, flush=True)
    with tempfile.TemporaryDirectory(prefix="voxops-mixed-proof-") as directory:
        base = Path(directory)
        root = base / "source"
        api, web, shared = (root / p for p in ("apps/api", "apps/web", "packages/shared"))
        for path in (api, web, shared):
            path.mkdir(parents=True)
        fixture(api, "fastapi-container")
        fixture(web, "vite-static")
        (root / "package.json").write_text(
            json.dumps(
                {
                    "name": "voxops-mixed",
                    "private": True,
                    "workspaces": ["apps/web", "packages/*"],
                }
            )
        )
        (shared / "package.json").write_text(
            json.dumps(
                {
                    "name": "@voxops/shared",
                    "version": "1.0.0",
                    "type": "module",
                    "exports": "./index.js",
                }
            )
        )
        (shared / "index.js").write_text('export const label = "shared-output-proof";')
        manifest = json.loads((web / "package.json").read_text())
        manifest["dependencies"]["@voxops/shared"] = "file:../../packages/shared"
        (web / "package.json").write_text(json.dumps(manifest))
        (web / "main.js").write_text(
            (web / "main.js").read_text()
            + '\nimport {label} from "@voxops/shared"; document.body.dataset.shared = label;\n'
        )
        settings = Settings(
            allowed_roots=[base],
            data_dir=base / "records",
            build_timeout_seconds=600,
            startup_timeout_seconds=45,
        )
        analysis = await Stage1Workflow(settings, ScanStore(settings.data_dir)).analyze(str(root))
        assert {unit.framework for unit in analysis.detected_units} == {"fastapi", "react_vite"}
        assert len(analysis.detected_units) == 2
        strategies = {
            unit.id: "container" if unit.framework == "fastapi" else "static"
            for unit in analysis.detected_units
        }
        artifact = build_plan(root, analysis, strategies)
        backend = next(unit for unit in artifact.units if unit.framework == "fastapi")
        frontend = next(unit for unit in artifact.units if unit.framework == "react_vite")
        definition = dockerfile("fastapi-container")
        assert artifact.tasks[f"docker-{backend.id}"].build_context == "."
        definition = definition.replace(
            "COPY requirements.txt .", "COPY apps/api/requirements.txt ."
        )
        definition = definition.replace("COPY main.py .", "COPY apps/api/main.py .")
        (api / "Dockerfile").write_text(definition)
        plan = build_execution_plan(
            root,
            artifact,
            settings,
            probes={
                backend.id: ProbeSpec(port=8000, path="/health", health_contract=True),
            },
        )
        contexts = freeze_plan_contexts(root, plan, settings)
        assert "packages/shared/index.js" in contexts[frontend.id].files
        assert "apps/api/main.py" not in contexts[frontend.id].files
        before = capture_source(root)
        session = SecretSession(EnvFormSchema(source_digest="0" * 64))
        drivers = {}

        def factory(unit_id):
            driver = DockerDriver(
                settings, owner, owner, lambda _: None, asyncio.Event(), session.redact
            )
            drivers[unit_id] = driver
            return driver

        def report(attempt):
            print(attempt.task_id, attempt.status, flush=True)

        try:
            outcome = await ExecutionRunner(settings, factory).run(
                plan, contexts, session, on_attempt=report
            )
            for unit_id, result in outcome.units.items():
                print(json.dumps(result.model_dump(mode="json")), flush=True)
                assert result.status == "passed", unit_id
            assert capture_source(root) == before
            assert not (web / "Dockerfile").exists()
            assert not (root / "node_modules").exists()
            assert not (web / "dist").exists()
            assert all(item.state == "removed" for d in drivers.values() for item in d.resources)
            print(
                "PASS mixed-monorepo source unchanged and owned cleanup complete", owner, flush=True
            )
        finally:
            for driver in drivers.values():
                await driver.cleanup()
            for context in contexts.values():
                remove_context(context)


if __name__ == "__main__":
    asyncio.run(main())
