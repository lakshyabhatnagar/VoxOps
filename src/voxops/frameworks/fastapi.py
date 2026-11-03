from pathlib import PurePosixPath

from voxops.domain.models import (
    DeployableUnit,
    Evidence,
    Framework,
    ManifestSummary,
    RepoScan,
    Role,
    RuntimeSpec,
)
from voxops.frameworks.base import facts
from voxops.frameworks.registry import make_unit, package_manager


# Why: Detect Python HTTP services without importing or instantiating applications.
# How: Combine dependency declarations, constructor syntax and explicit ASGI commands.
class FastAPIAdapter:
    # Why: Infer only credible application startup metadata for a Python package.
    # How: Prefer Procfile commands and derive module paths solely from proven app instances.
    def detect(self, manifest: ManifestSummary, scan: RepoScan) -> DeployableUnit | None:
        if manifest.kind != "python":
            return None
        local = facts(manifest, scan)
        instances = [item for item in local if item.kind in {"fastapi_instance", "fastapi_factory"}]
        explicit = [
            item
            for item in local
            if item.kind == "start_command"
            and (
                item.field.startswith(("uvicorn ", "gunicorn "))
                or instances
                and item.field.startswith(("python ", "python3 "))
            )
        ]
        if "fastapi" not in manifest.dependencies and not instances:
            return None
        if manifest.workspace and not instances:
            return None
        if not instances and not explicit:
            return None
        evidence = [
            Evidence(path=manifest.path, kind="dependency", field="fastapi", confidence=0.8)
        ]
        if "fastapi" not in manifest.dependencies:
            evidence = []
        evidence += instances
        runtime = RuntimeSpec(
            language="python",
            version=manifest.runtime_version,
            package_manager=package_manager(manifest, scan),
            dependency_files=[
                item.path
                for item in scan.manifests
                if item.root_path == manifest.root_path and item.kind == "python"
            ],
        )
        questions = []
        commands = explicit
        if commands:
            runtime.start_command = commands[0].field
            runtime.command_source = "explicit"
            evidence += commands
        elif len(instances) == 1:
            relative = (
                PurePosixPath(instances[0].path).relative_to(manifest.root_path).with_suffix("")
            )
            if all(part.isidentifier() for part in relative.parts):
                parts = relative.parts[1:] if relative.parts[0] == "src" else relative.parts
                if not parts:
                    return None
                runtime.start_command = f"uvicorn {'.'.join(parts)}:{instances[0].field}"
                if instances[0].kind == "fastapi_factory":
                    runtime.start_command += " --factory"
                runtime.command_source = "inferred"
                if relative.parts[0] == "src":
                    runtime.start_command += " --app-dir src"
        if not runtime.start_command:
            questions.append("Confirm the ASGI application module and start command.")
        return make_unit(manifest, scan, Framework.FASTAPI, Role.HTTP, evidence, runtime, questions)
