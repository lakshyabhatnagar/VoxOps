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


# Why: Identify standalone React/Vite applications while excluding shared UI packages.
# How: Require both dependencies and use application files/scripts for executable evidence.
class ReactViteAdapter:
    # Why: Preserve actual Vite build conventions and flag server-capable ambiguity.
    # How: Read sanitized script/config facts and inherit workspace package-manager evidence.
    def detect(self, manifest: ManifestSummary, scan: RepoScan) -> DeployableUnit | None:
        if manifest.kind != "node" or not {"react", "vite"}.issubset(manifest.dependencies):
            return None
        local = facts(manifest, scan)
        if any(item.kind == "vite_library" for item in local):
            return None
        files = [file for file in scan.relevant_files if owns(manifest.root_path, file, scan)]
        index = str(PurePosixPath(manifest.root_path) / "index.html") in scan.shallow_tree
        if not index and not any("vite" in value for value in manifest.scripts.values()):
            return None
        evidence = [
            Evidence(path=manifest.path, kind="dependency", field="react + vite", confidence=0.95)
        ]
        evidence += [
            Evidence(path=file, kind="config", field="Vite config", confidence=0.9)
            for file in files
            if PurePosixPath(file).name.startswith("vite.config.")
        ]
        output = next((item.field for item in local if item.kind == "vite_output"), "dist")
        if manifest.build_output_directory:
            output = manifest.build_output_directory
        questions = []
        if manifest.build_output_dynamic or (
            not manifest.build_output_directory
            and any(item.kind == "vite_output_dynamic" for item in local)
        ):
            output = None
            questions.append("Vite output directory is computed; confirm its value.")
        server = manifest.server_build or any(item.kind == "vite_server" for item in local)
        if server:
            if manifest.server_build:
                evidence.append(
                    Evidence(
                        path=manifest.path,
                        kind="vite_server",
                        field="SSR build script",
                        confidence=0.95,
                    )
                )
            questions.append("Vite SSR configuration requires server runtime review.")
        manager = package_manager(manifest, scan)
        runtime = RuntimeSpec(
            language="javascript",
            version=manifest.runtime_version,
            package_manager=manager,
            dependency_files=[manifest.path],
            build_command=f"{manager} run build" if "build" in manifest.scripts else None,
            output_directory=output,
            command_source="explicit",
        )
        return make_unit(
            manifest,
            scan,
            Framework.VITE,
            Role.UNKNOWN if server else Role.STATIC,
            evidence + local,
            runtime,
            questions,
        )
