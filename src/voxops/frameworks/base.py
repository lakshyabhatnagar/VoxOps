from pathlib import PurePosixPath
from typing import Protocol

from voxops.domain.models import (
    DeployableUnit,
    Evidence,
    ManifestSummary,
    RepoScan,
)


# Defines how a framework is recognized in a project.
# Each adapter can return a matching project unit or decline the package.
class FrameworkAdapter(Protocol):
    # Checks a package for the adapter's framework.
    # Uses the supplied scan facts without running project code.
    def detect(self, manifest: ManifestSummary, scan: RepoScan) -> DeployableUnit | None: ...


# Finds whether a file belongs to a particular package.
# Uses the closest enclosing manifest directory to avoid assigning it twice.
def owns(root: str, file: str, scan: RepoScan) -> bool:
    path = PurePosixPath(file)
    roots = {item.root_path for item in scan.manifests}
    parents = [candidate for candidate in roots if path.is_relative_to(candidate)]
    nearest = max(parents, key=lambda item: len(PurePosixPath(item).parts), default=".")
    return nearest == root


# Collects scan facts belonging to one package.
# Keeps only facts whose files belong to that package.
def facts(manifest: ManifestSummary, scan: RepoScan) -> list[Evidence]:
    return [item for item in scan.evidence if owns(manifest.root_path, item.path, scan)]
