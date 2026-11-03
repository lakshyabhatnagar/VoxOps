from pathlib import PurePosixPath
from typing import Protocol

from voxops.domain.models import (
    DeployableUnit,
    Evidence,
    ManifestSummary,
    RepoScan,
)


# Why: Keep independent framework adapters separate from future cloud service adapters.
# How: Require each adapter to return a typed unit or decline the package.
class FrameworkAdapter(Protocol):
    # Why: Give the registry one bounded detection contract per package root.
    # How: Consume sanitized scan facts and never perform filesystem or execution side effects.
    def detect(self, manifest: ManifestSummary, scan: RepoScan) -> DeployableUnit | None: ...


# Why: Assign files to their nearest package and avoid duplicate parent detections.
# How: Select the deepest manifest directory containing the relative file.
def owns(root: str, file: str, scan: RepoScan) -> bool:
    path = PurePosixPath(file)
    roots = {item.root_path for item in scan.manifests}
    parents = [candidate for candidate in roots if path.is_relative_to(candidate)]
    nearest = max(parents, key=lambda item: len(PurePosixPath(item).parts), default=".")
    return nearest == root


# Why: Keep adapters focused on evidence within their own build context.
# How: Filter safe facts through nearest-package ownership.
def facts(manifest: ManifestSummary, scan: RepoScan) -> list[Evidence]:
    return [item for item in scan.evidence if owns(manifest.root_path, item.path, scan)]
