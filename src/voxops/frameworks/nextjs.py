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


# Recognizes Next.js applications in a project.
# Uses dependencies, export settings and server routes to describe the application.
class NextJSAdapter:
    # Determines how a Next.js application can run.
    # Checks server features and reports conflicts with static export settings.
    def detect(self, manifest: ManifestSummary, scan: RepoScan) -> DeployableUnit | None:
        if manifest.kind != "node" or "next" not in manifest.dependencies:
            return None
        local = facts(manifest, scan)
        application_files = any(
            owns(manifest.root_path, file, scan)
            and any(part in {"app", "pages"} for part in PurePosixPath(file).parts)
            for file in scan.relevant_files
        )
        if not application_files and not any(
            value.startswith("next ") for value in manifest.scripts.values()
        ):
            return None
        api = any(item.kind in {"next_api", "next_action"} for item in local)
        server = any(item.kind == "next_server" for item in local)
        export = any(item.kind == "next_static_export" for item in local)
        questions = []
        if export and (api or server):
            questions.append("Static export conflicts with server capability; review Next config.")
        elif not export and not api and not server:
            questions.append("No static export: confirm SSR/runtime requirements.")
        role = Role.FULLSTACK if api else Role.SSR if server or not export else Role.STATIC
        manager = package_manager(manifest, scan)
        runtime = RuntimeSpec(
            language="javascript",
            version=manifest.runtime_version,
            package_manager=manager,
            dependency_files=[manifest.path],
            build_command=f"{manager} run build" if "build" in manifest.scripts else None,
            start_command=f"{manager} run start" if "start" in manifest.scripts else None,
            command_source="explicit",
            output_directory="out" if export and not (api or server) else None,
        )
        evidence = [Evidence(path=manifest.path, kind="dependency", field="next", confidence=0.95)]
        return make_unit(manifest, scan, Framework.NEXT, role, evidence + local, runtime, questions)
