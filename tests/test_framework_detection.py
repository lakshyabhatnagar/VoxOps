import json

import pytest
from conftest import copy_fixture, settings_for

from voxops.frameworks.registry import detect_units
from voxops.repository.filesystem_scanner import scan_repository


@pytest.mark.parametrize(
    ("fixture", "frameworks", "roles"),
    [
        ("fastapi_only", ["fastapi"], ["http_service"]),
        ("vite_only", ["react_vite"], ["static_frontend"]),
        ("nextjs_fullstack", ["nextjs"], ["fullstack_web"]),
        ("monorepo_fastapi_vite", ["fastapi", "react_vite"], ["http_service", "static_frontend"]),
        ("monorepo_shared_package", ["react_vite"], ["static_frontend"]),
    ],
)
# Why: Protect valid topologies from forced pairing or library deployment.
# How: Scan real fixture layouts and assert exact frameworks, roles and stable repeated-scan IDs.
def test_fixture_classification(tmp_path, fixture, frameworks, roles):
    root = copy_fixture(tmp_path, fixture)
    settings = settings_for(tmp_path)
    first = detect_units(scan_repository(root, settings))
    second = detect_units(scan_repository(root, settings))
    assert [unit.framework for unit in first] == frameworks
    assert [unit.role for unit in first] == roles
    assert [unit.id for unit in first] == [unit.id for unit in second]
    assert all(unit.detection.evidence for unit in first)
    if fixture == "vite_only":
        assert first[0].runtime.output_directory == "public-build"
        assert first[0].runtime.package_manager == "pnpm"
        assert first[0].config_requirements[0].visibility == "public"
    if fixture == "monorepo_shared_package":
        assert first[0].shared_packages == ["packages/shared"]
    if fixture == "monorepo_fastapi_vite":
        assert first[1].runtime.workspace_root == "."
        assert first[1].runtime.lockfile_path == "pnpm-lock.yaml"


# Why: Distinguish explicit static exports from Next's server default without hiding conflicts.
# How: Mutate one fixture's config and compare static, SSR and conflicting API classifications.
def test_next_export_and_server_precedence(tmp_path):
    root = copy_fixture(tmp_path, "nextjs_fullstack")
    route = root / "app/api/hello/route.ts"
    config = root / "next.config.mjs"
    config.write_text("export default { output: 'export' }")
    settings = settings_for(tmp_path)
    conflict = detect_units(scan_repository(root, settings))[0]
    assert conflict.role == "fullstack_web"
    assert conflict.detection.unresolved_questions
    route.unlink()
    assert detect_units(scan_repository(root, settings))[0].role == "static_frontend"
    config.unlink()
    assert detect_units(scan_repository(root, settings))[0].role == "server_rendered_web"
    config.write_text("// output: 'export'\nexport default {}")
    assert detect_units(scan_repository(root, settings))[0].role == "server_rendered_web"


# Why: Recognize exported factories without inventing importable module-level app variables.
# How: Prove a returned constructor instance and infer the explicit Uvicorn factory form.
def test_fastapi_local_factory_does_not_invent_module_app(tmp_path):
    (tmp_path / "requirements.txt").write_text("fastapi\n")
    (tmp_path / "factory.py").write_text(
        "from fastapi import FastAPI\n"
        "# Why: Supply a factory-local app that is not importable as module:app.\n"
        "# How: Return an application only if the factory is explicitly called.\n"
        "def create_app():\n    app = FastAPI()\n    return app\n"
    )
    unit = detect_units(scan_repository(tmp_path, settings_for(tmp_path)))[0]
    assert unit.runtime.start_command == "uvicorn factory:create_app --factory"
    (tmp_path / "factory.py").write_text("from fastapi import FastAPI\n")
    assert detect_units(scan_repository(tmp_path, settings_for(tmp_path))) == []


# Why: Preserve Vite CLI output overrides and avoid calling an SSR build static.
# How: Scan explicit and dynamic output flags plus a server build in the same real fixture.
def test_vite_script_overrides_and_ssr(tmp_path):
    root = copy_fixture(tmp_path, "vite_only")
    manifest = root / "package.json"
    data = json.loads(manifest.read_text())
    data["scripts"]["build"] = "vite build --outDir client-output --ssr src/server.ts"
    manifest.write_text(json.dumps(data))
    unit = detect_units(scan_repository(root, settings_for(tmp_path)))[0]
    assert unit.runtime.output_directory == "client-output"
    assert unit.role == "unknown" and unit.detection.unresolved_questions
    data["scripts"]["build"] = "vite build --outDir $OUTPUT"
    manifest.write_text(json.dumps(data))
    unit = detect_units(scan_repository(root, settings_for(tmp_path)))[0]
    assert unit.runtime.output_directory is None
