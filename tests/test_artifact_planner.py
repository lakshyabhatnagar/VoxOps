import asyncio

import pytest
from conftest import copy_fixture, settings_for
from pydantic import ValidationError

from voxops.artifacts.planner import build_plan
from voxops.core.errors import AppError
from voxops.domain.artifact_models import ArtifactPlan, ArtifactTask
from voxops.storage.scan_store import ScanStore
from voxops.workflows.stage1_graph import Stage1Workflow


# Scan a copied sample repository and choose the requested deployment options.
# Return the repository, its analysis, and the resulting file plan.
def fixture_plan(tmp_path, name, strategies):
    root = copy_fixture(tmp_path, name)
    settings = settings_for(tmp_path)
    analysis = asyncio.run(
        Stage1Workflow(settings, ScanStore(settings.data_dir)).analyze(str(root))
    )
    choices = {unit.id: strategies[unit.framework.value] for unit in analysis.detected_units}
    return root, analysis, build_plan(root, analysis, choices)


@pytest.mark.parametrize(
    ("fixture", "strategies", "containers"),
    [
        ("fastapi_only", {"fastapi": "container"}, 1),
        ("vite_only", {"react_vite": "static"}, 0),
        ("nextjs_fullstack", {"nextjs": "native"}, 0),
        ("nextjs_fullstack", {"nextjs": "container"}, 1),
        ("monorepo_fastapi_vite", {"fastapi": "container", "react_vite": "static"}, 1),
    ],
)
# Plan files for sample apps using different deployment choices.
# Check the file types, locations, and dependencies for each app.
def test_framework_strategy_plans(tmp_path, fixture, strategies, containers):
    _, analysis, plan = fixture_plan(tmp_path, fixture, strategies)
    docker = [task for task in plan.tasks.values() if task.artifact_type == "dockerfile"]
    assert len(docker) == containers
    assert len(plan.units) == len(analysis.detected_units)
    assert len({task.target_path for task in plan.tasks.values()}) == len(plan.tasks)
    assert sum(task.artifact_type == "ci" for task in plan.tasks.values()) == 1
    assert sum(task.artifact_type == "gitignore" for task in plan.tasks.values()) == 1
    assert set(plan.tasks["ci"].depends_on) == {task.task_id for task in docker} | (
        {"dockerignore"} if containers else set()
    )
    assert all(task.build_context == "." for task in docker)
    assert plan.tasks["provider"].operation == "skip"
    assert analysis.schema_version == "1"
    assert "tasks" not in analysis.model_dump()


# Plan a Next.js static export with no API routes.
# Check that the plan uses static hosting without a Dockerfile.
def test_next_static_export_has_no_container(tmp_path):
    root = copy_fixture(tmp_path, "nextjs_fullstack")
    (root / "app/api/hello/route.ts").unlink()
    (root / "app/page.tsx").write_text("export default function Page() { return <p>Static</p>; }\n")
    (root / "next.config.js").write_text('module.exports = { output: "export" };\n')
    settings = settings_for(tmp_path)
    analysis = asyncio.run(
        Stage1Workflow(settings, ScanStore(settings.data_dir)).analyze(str(root))
    )
    assert analysis.detected_units[0].role.value == "static_frontend"
    plan = build_plan(root, analysis, {analysis.detected_units[0].id: "static"})
    assert not plan.unresolved_questions
    assert not any(task.artifact_type == "dockerfile" for task in plan.tasks.values())
    assert plan.units[0].runtime.output_directory == "out"


# Request static hosting for a Next.js app with server features.
# Check that planning rejects the conflicting choice.
def test_next_fullstack_rejects_static(tmp_path):
    root, analysis, _ = fixture_plan(tmp_path, "nextjs_fullstack", {"nextjs": "native"})
    with pytest.raises(AppError, match="Strategy conflicts"):
        build_plan(root, analysis, {analysis.detected_units[0].id: "static"})


# Plan a workspace that already contains a Dockerfile.
# Check that the existing file and shared package inputs are recorded.
def test_existing_and_workspace_inputs(tmp_path):
    root, analysis, _ = fixture_plan(
        tmp_path, "monorepo_fastapi_vite", {"fastapi": "container", "react_vite": "static"}
    )
    unit = next(unit for unit in analysis.detected_units if unit.framework.value == "fastapi")
    path = root / unit.root_path / "Dockerfile"
    path.write_text('FROM python:3.12\nUSER 1000\nCMD ["python", "main.py"]\n')
    plan = build_plan(
        root,
        analysis,
        {
            unit.id: "container" if unit.framework.value == "fastapi" else "static"
            for unit in analysis.detected_units
        },
    )
    task = plan.tasks[f"docker-{unit.id}"]
    assert task.operation == "validate" and len(task.existing_hash) == 64
    assert task.build_context == "."


# Add an ignore rule that excludes the app a container needs to run.
# Check that validation rejects the rule.
def test_container_ignore_cannot_exclude_entrypoint(tmp_path):
    from voxops.artifacts.validators import validate_artifact, validate_cross_artifacts

    root, _, plan = fixture_plan(tmp_path, "fastapi_only", {"fastapi": "container"})
    ignore = ".env\n.env.*\n.git/\n*.pem\n*.key\n*.py\n"
    assert "main.py" in plan.tasks["dockerignore"].required_paths
    assert (
        validate_artifact(plan.tasks["dockerignore"], ignore, plan, root=root)[0].status == "failed"
    )
    results = validate_cross_artifacts(plan, {"Dockerfile": "COPY . .\n", ".dockerignore": ignore})
    assert results[0].status == "failed"


# Change a file plan to contain duplicate paths or invalid dependencies.
# Check that validation rejects each conflicting plan.
def test_plan_rejects_conflicting_dag(tmp_path):
    _, _, plan = fixture_plan(tmp_path, "vite_only", {"react_vite": "static"})
    data = plan.model_dump()
    data["tasks"]["second"] = ArtifactTask(
        task_id="second",
        artifact_type="ci",
        target_path=".github/workflows/ci.yml",
        reason="Deliberate duplicate writer",
    ).model_dump()
    with pytest.raises(ValidationError, match="ownership conflict"):
        ArtifactPlan.model_validate(data)
    data = plan.model_dump()
    data["tasks"]["ci"]["depends_on"] = ["missing"]
    with pytest.raises(ValidationError, match="Unknown task dependency"):
        ArtifactPlan.model_validate(data)
    data["tasks"]["ci"]["depends_on"] = ["gitignore"]
    data["tasks"]["gitignore"]["depends_on"] = ["ci"]
    with pytest.raises(ValidationError, match="cycle"):
        ArtifactPlan.model_validate(data)
