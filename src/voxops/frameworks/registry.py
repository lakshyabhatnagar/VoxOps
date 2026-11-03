import hashlib
from pathlib import PurePosixPath

from voxops.domain.models import (
    ConfigRequirement,
    DeployableUnit,
    Evidence,
    Framework,
    FrameworkDetection,
    ManifestSummary,
    RepoScan,
    Role,
    RuntimeSpec,
)
from voxops.frameworks.base import owns

LOCK_MANAGERS = {
    "pnpm-lock.yaml": "pnpm",
    "yarn.lock": "yarn",
    "bun.lock": "bun",
    "bun.lockb": "bun",
    "package-lock.json": "npm",
    "uv.lock": "uv",
    "poetry.lock": "poetry",
}


# Why: Preserve package-manager evidence inherited from workspace ancestors.
# How: Prefer explicit package metadata, then the nearest compatible lockfile.
def package_manager(manifest: ManifestSummary, scan: RepoScan) -> str:
    if manifest.package_manager:
        return manifest.package_manager
    path = PurePosixPath(manifest.root_path)
    for parent in [path, *path.parents]:
        inherited = next(
            (
                item.package_manager
                for item in scan.manifests
                if item.root_path == str(parent)
                and item.kind == manifest.kind
                and item.package_manager
            ),
            None,
        )
        if inherited:
            return inherited
        for filename, manager in LOCK_MANAGERS.items():
            if str(parent / filename) in scan.relevant_files and (manifest.kind == "python") == (
                manager in {"uv", "poetry"}
            ):
                return manager
    return "pip" if manifest.kind == "python" else "npm"


# Why: Keep configuration names scoped to units while inheriting root example declarations.
# How: Merge owned source references and ancestor env keys with strongest presence winning.
def unit_config(manifest: ManifestSummary, scan: RepoScan) -> list[ConfigRequirement]:
    keys = {}
    priority = {"unknown": 0, "referenced": 1, "example_only": 2, "present": 3}
    for file, requirements in scan.config_keys.items():
        env_ancestor = PurePosixPath(file).name.startswith(".env") and PurePosixPath(
            manifest.root_path
        ).is_relative_to(PurePosixPath(file).parent)
        if not owns(manifest.root_path, file, scan) and not env_ancestor:
            continue
        for item in requirements:
            if (
                item.name not in keys
                or priority[item.presence] > priority[keys[item.name].presence]
            ):
                keys[item.name] = item
    return [keys[key] for key in sorted(keys)]


# Why: Build consistent deterministic unit contracts for all independent adapters.
# How: Derive IDs from normalized root/role and combine scoped config with adapter evidence.
def make_unit(
    manifest: ManifestSummary,
    scan: RepoScan,
    framework: Framework,
    role: Role,
    evidence: list[Evidence],
    runtime: RuntimeSpec,
    questions: list[str] | None = None,
) -> DeployableUnit:
    identity = hashlib.sha256(f"{manifest.root_path}:{role}".encode()).hexdigest()[:16]
    confidence = max((item.confidence for item in evidence), default=0.5)
    path = PurePosixPath(manifest.root_path)
    runtime.workspace_root = next(
        (str(parent) for parent in [path, *path.parents] if str(parent) in scan.workspace_roots),
        None,
    )
    runtime.lockfile_path = next(
        (
            str(parent / name)
            for parent in [path, *path.parents]
            for name, manager in LOCK_MANAGERS.items()
            if manager == runtime.package_manager and str(parent / name) in scan.relevant_files
        ),
        None,
    )
    configuration = unit_config(manifest, scan)
    if framework in {Framework.FASTAPI, Framework.NEXT}:
        configuration = [
            item.model_copy(update={"stage": "runtime"}) if item.visibility != "public" else item
            for item in configuration
        ]
    return DeployableUnit(
        id=identity,
        name=PurePosixPath(manifest.root_path).name or scan.display_name,
        root_path=manifest.root_path,
        role=role,
        framework=framework,
        language=runtime.language,
        runtime=runtime,
        config_requirements=configuration,
        detection=FrameworkDetection(
            framework=framework,
            root_path=manifest.root_path,
            role_candidate=role,
            evidence=evidence,
            confidence=confidence,
            unresolved_questions=questions or [],
        ),
    )


# Why: Assemble framework detection without coupling adapters to combinations or cloud services.
# How: Run fixed adapters on consolidated manifests, taking the first match.
def detect_units(scan: RepoScan) -> list[DeployableUnit]:
    from voxops.frameworks.fastapi import FastAPIAdapter
    from voxops.frameworks.nextjs import NextJSAdapter
    from voxops.frameworks.react_vite import ReactViteAdapter

    adapters = (NextJSAdapter(), ReactViteAdapter(), FastAPIAdapter())
    grouped: dict[tuple[str, str], ManifestSummary] = {}
    for manifest in scan.manifests:
        key = (manifest.root_path, manifest.kind)
        if key in grouped:
            combined = grouped[key]
            combined.dependencies = sorted(set(combined.dependencies + manifest.dependencies))
        else:
            grouped[key] = manifest.model_copy(deep=True)
    units = []
    for manifest in grouped.values():
        for adapter in adapters:
            unit = adapter.detect(manifest, scan)
            if unit:
                units.append(unit)
                break
    package_roots = {
        item.package_name: item.root_path for item in scan.manifests if item.package_name
    }
    deployed = {unit.root_path for unit in units}
    for unit in units:
        manifest = grouped.get((unit.root_path, "node"))
        if manifest:
            unit.shared_packages = sorted(
                {
                    package_roots[name]
                    for name in manifest.local_dependencies
                    if name in package_roots and package_roots[name] not in deployed
                }
            )
    return sorted(units, key=lambda item: (item.root_path, item.framework))
