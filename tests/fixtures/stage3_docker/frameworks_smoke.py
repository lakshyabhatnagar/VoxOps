"""Opt-in real Docker proof of the production framework planner and runner."""

import argparse
import asyncio
import json
import tempfile
from pathlib import Path
from uuid import uuid4

from voxops.artifacts.planner import build_plan
from voxops.core.settings import Settings
from voxops.domain.execution_models import EnvConsumer, EnvFormSchema, EnvRequirement, ProbeSpec
from voxops.execution.contexts import remove_context
from voxops.execution.docker_driver import DockerDriver
from voxops.execution.environment import SecretSession
from voxops.execution.planner import build_execution_plan, freeze_plan_contexts
from voxops.execution.runner import ExecutionRunner
from voxops.storage.mongodb import database_for
from voxops.storage.scan_store import ScanStore
from voxops.workflows.stage1_graph import Stage1Workflow

CASES = ("fastapi-container", "vite-static", "next-native", "next-container", "next-static")


def fixture(root, case):
    if case == "fastapi-container":
        (root / "requirements.txt").write_text("fastapi==0.115.12\nuvicorn==0.34.3\n")
        (root / "main.py").write_text(
            "from fastapi import FastAPI\napp = FastAPI()\n"
            '@app.get("/health")\ndef health():\n    return {"ok": True}\n'
        )
        return
    if case == "vite-static":
        (root / "package.json").write_text(
            json.dumps(
                {
                    "name": "voxops-real-vite",
                    "private": True,
                    "type": "module",
                    "scripts": {"build": "vite build"},
                    "dependencies": {"vite": "5.4.21", "react": "19.1.0"},
                }
            )
        )
        (root / "index.html").write_text(
            '<html><body><div id="root"></div>'
            '<script type="module" src="/main.js"></script></body></html>'
        )
        (root / "main.js").write_text(
            'import React from "react"; '
            'document.querySelector("#root").textContent = React.version;'
        )
        return
    (root / "app").mkdir()
    (root / "package.json").write_text(
        json.dumps(
            {
                "name": "voxops-real-next",
                "private": True,
                "scripts": {"build": "node build-check.cjs && next build", "start": "next start"},
                "dependencies": {"next": "15.5.9", "react": "19.1.0", "react-dom": "19.1.0"},
            }
        )
    )
    (root / "build-check.cjs").write_text(
        'if (process.env.RUNTIME_ONLY_SENTINEL) throw new Error("Runtime env reached builder");\n'
        'console.log("Builder phase separation passed");\n'
    )
    (root / "app/layout.js").write_text(
        "export default function Layout({children}) { "
        "return <html><body>{children}</body></html>; }"
    )
    (root / "app/page.js").write_text(
        "export default function Page() { return <main>Real Next verification</main>; }"
    )
    output = 'output: "export",' if case == "next-static" else ""
    (root / "next.config.mjs").write_text(
        f"export default {{ {output} experimental: {{ cpus: 1 }}, }};"
    )
    if case != "next-static":
        route = root / "app/api/phase"
        route.mkdir(parents=True)
        (route / "route.js").write_text(
            'export const dynamic = "force-dynamic";\n'
            "export async function GET() {\n"
            "  const ok = !process.env.BUILD_ONLY_SENTINEL && "
            'process.env.RUNTIME_ONLY_SENTINEL === "runtime-phase-fixture";\n'
            "  return Response.json({ok}, {status: ok ? 200 : 500});\n}\n"
        )


def dockerfile(case):
    if case == "fastapi-container":
        return (
            "FROM python:3.12-slim\nWORKDIR /app\nCOPY requirements.txt .\n"
            "RUN pip install --no-cache-dir -r requirements.txt\nCOPY main.py .\n"
            "USER 1000\nEXPOSE 8000\n"
            'CMD ["uvicorn","main:app","--host","0.0.0.0","--port","8000"]\n'
        )
    return (
        "FROM node:22-bookworm-slim\nWORKDIR /app\nCOPY . .\n"
        "ENV NEXT_TELEMETRY_DISABLED=1\nRUN npm install --no-audit --no-fund && npm run build\n"
        "ENV HOSTNAME=0.0.0.0\nENV PORT=3000\nUSER node\nEXPOSE 3000\n"
        'CMD ["npm","run","start","--","--hostname","0.0.0.0","--port","3000"]\n'
    )


async def run_case(case, build_timeout=600):
    owner = uuid4().hex
    print("START", case, "owner", owner, flush=True)
    with tempfile.TemporaryDirectory(prefix="voxops-framework-proof-") as directory:
        base = Path(directory)
        root = base / "source"
        root.mkdir()
        fixture(root, case)
        settings = Settings(
            allowed_roots=[base],
            mongo_database="voxops_smoke_" + base.name.replace("-", "_"),
            build_timeout_seconds=build_timeout,
            startup_timeout_seconds=45,
        )
        analysis = await Stage1Workflow(settings, ScanStore(database_for(settings))).analyze(
            str(root)
        )
        assert len(analysis.detected_units) == 1
        unit = analysis.detected_units[0]
        strategy = case.split("-", 1)[1]
        strategy = "container" if strategy == "container" else strategy
        artifact = build_plan(root, analysis, {unit.id: strategy})
        if strategy == "container":
            (root / "Dockerfile").write_text(dockerfile(case))
        probes = {}
        if case == "fastapi-container":
            probes[unit.id] = ProbeSpec(port=8000, path="/health", health_contract=True)
        elif case in {"next-native", "next-container"}:
            probes[unit.id] = ProbeSpec(port=3000, path="/api/phase", health_contract=True)
        plan = build_execution_plan(root, artifact, settings, probes=probes)
        contexts = freeze_plan_contexts(root, plan, settings)
        requirements, values = [], {}
        if case in {"next-native", "next-container"}:
            for phase, name, visibility, value in (
                ("build", "BUILD_ONLY_SENTINEL", "public", "build-phase-fixture"),
                ("runtime", "RUNTIME_ONLY_SENTINEL", "secret", "runtime-phase-fixture"),
            ):
                # Container builds need ARG only for public config. This sentinel is injected
                # only in native mode; the production container preserves its Dockerfile CMD.
                if phase == "build" and strategy == "container":
                    continue
                requirements.append(
                    EnvRequirement(
                        field_id=name,
                        name=name,
                        visibility=visibility,
                        necessity="required",
                        consumers=[EnvConsumer(unit_id=unit.id, phase=phase)],
                    )
                )
                values[name] = value
        session = SecretSession(
            EnvFormSchema(source_digest="0" * 64, requirements=requirements), values
        )
        drivers, journal, events = {}, [], []

        def factory(unit_id):
            driver = DockerDriver(
                settings,
                owner,
                owner,
                lambda item: journal.append(item.model_copy(deep=True)),
                asyncio.Event(),
                session.redact,
            )
            drivers[unit_id] = driver
            return driver

        async def on_attempt(attempt):
            events.append(attempt.model_dump(mode="json"))
            print(case, attempt.task_id, attempt.status, flush=True)
            if attempt.status != "passed" or not attempt.task_id.endswith(":startup"):
                return
            driver = drivers[attempt.unit_id]
            runtime = next(
                r for r in driver.resources if r.kind == "container" and r.state == "active"
            )
            metadata = await driver._inspect("container", runtime.resource_id)
            print(
                "RUNTIME",
                case,
                runtime.resource_id,
                metadata["Image"],
                metadata["HostConfig"],
                metadata["NetworkSettings"]["Ports"],
                flush=True,
            )
            assert metadata["State"]["Running"]
            assert (
                metadata["HostConfig"]["Memory"] == settings.execution_runtime_memory_mb * 1024**2
            )
            env = json.loads(
                await driver._run(
                    ["inspect", "--format", "{{json .Config.Env}}", runtime.resource_id],
                    machine_output=True,
                )
            )
            assert not any(item.startswith("BUILD_ONLY_SENTINEL=") for item in env)
            if case in {"next-native", "next-container"}:
                assert "RUNTIME_ONLY_SENTINEL=runtime-phase-fixture" in env
            if case == "next-native":
                assert any(r.kind == "container" and r.state == "removed" for r in driver.resources)
                assert any(
                    m.get("Destination") == "/workspace" and m.get("Type") == "volume"
                    for m in metadata["Mounts"]
                )

        try:
            outcome = await ExecutionRunner(settings, factory).run(
                plan,
                contexts,
                session,
                config_generations={unit.id: "fixture-authority"} if values else {},
                on_attempt=on_attempt,
            )
            result = outcome.units[unit.id]
            if result.status != "passed":
                print(json.dumps(result.model_dump(mode="json"), indent=2), flush=True)
            assert result.status == "passed", case
            assert not any(value in json.dumps(events) for value in values.values())
            assert all(r.state == "removed" for d in drivers.values() for r in d.resources)
            print(
                "PASS",
                case,
                result.achieved_level,
                "toolchain",
                outcome.plan.units[unit.id].toolchain,
                "resources",
                [
                    (r.kind, r.name, r.resource_id, r.state)
                    for d in drivers.values()
                    for r in d.resources
                ],
                flush=True,
            )
        finally:
            for driver in drivers.values():
                await driver.cleanup()
            for context in contexts.values():
                remove_context(context)
        assert not (root / "Dockerfile").exists() if strategy != "container" else True


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("cases", nargs="*", choices=CASES)
    parser.add_argument("--build-timeout", type=float, default=600)
    options = parser.parse_args()
    for case in options.cases or CASES:
        await run_case(case, options.build_timeout)


if __name__ == "__main__":
    asyncio.run(main())
