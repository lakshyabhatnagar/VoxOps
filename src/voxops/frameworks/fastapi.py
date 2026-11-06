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
from voxops.frameworks.base import facts, owns
from voxops.frameworks.registry import make_unit, package_manager
from voxops.repository.startup_reader import startup_target


# Recognizes FastAPI services in Python projects.
# Uses dependencies, application declarations and startup commands as evidence.
class FastAPIAdapter:
    # Finds a FastAPI application and its startup details.
    # Prefers existing commands and falls back to supported source declarations.
    def detect(self, manifest: ManifestSummary, scan: RepoScan) -> DeployableUnit | None:
        if manifest.kind != "python":
            return None
        local = facts(manifest, scan)
        instances = [item for item in local if item.kind in {"fastapi_instance", "fastapi_factory"}]
        targets = set()
        for instance in instances:
            path = PurePosixPath(instance.path).relative_to(manifest.root_path).with_suffix("")
            parts = path.parts[1:] if path.parts[0] == "src" else path.parts
            targets.add(f"{'.'.join(parts)}:{instance.field}")
        explicit = []
        for candidate in scan.startup_commands:
            if not owns(manifest.root_path, candidate.path, scan):
                continue
            target = startup_target(candidate.command)
            if target in targets or (
                PurePosixPath(candidate.path).name in {"Procfile", "Dockerfile"}
                and (target or instances)
            ):
                explicit.append(candidate)
        priority = {"Procfile": 0, "README.md": 1, "Dockerfile": 2}
        explicit.sort(key=lambda item: priority.get(PurePosixPath(item.path).name, 3))
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
            selected = commands[0]
            runtime.start_command = selected.command
            runtime.prerequisite_commands = selected.prerequisite_commands
            runtime.command_source = "explicit"
            evidence.append(
                Evidence(
                    path=selected.path, kind="start_command", field=selected.command, confidence=1
                )
            )
            evidence.extend(
                Evidence(
                    path=selected.path, kind="startup_prerequisite", field=command, confidence=1
                )
                for command in selected.prerequisite_commands
            )
            alternatives = {
                item.command
                for item in explicit
                if item.path == selected.path and item.command != selected.command
            }
            if alternatives:
                questions.append(
                    "Several startup commands match this app; confirm the selected example."
                )
        else:
            exported_paths = {item.path for item in instances if item.kind == "fastapi_instance"}
            instances = [
                item
                for item in instances
                if item.kind == "fastapi_instance" or item.path not in exported_paths
            ]
        if not commands and len(instances) == 1:
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
