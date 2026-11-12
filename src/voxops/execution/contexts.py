"""Private immutable input snapshots for isolated execution."""

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from voxops.artifacts.security import safe_read
from voxops.core.errors import AppError
from voxops.repository.snapshot import capture_source
from voxops.storage.artifact_store import digest


# Describe the private copy of files used for one build.
# Remember its directory, included paths, file modes, and content comparison codes.
# Later checks use this record to detect changes before Docker consumes the copy.
@dataclass(frozen=True)
class FrozenContext:
    path: Path
    identity: str
    files: tuple[str, ...]
    inventory: tuple[tuple[str, str, str], ...] = ()
    max_file_bytes: int = 1048576


# Copy the selected build inputs into a private temporary directory.
# Check that required inputs exist and that the originals did not change while copying.
# Make the copy read-only and record its exact contents so the build uses a known set of files.
def freeze_context(
    source: Path,
    *,
    context: str = ".",
    selected: tuple[str, ...] | None = None,
    required: tuple[str, ...] = (),
    identity_config: dict | None = None,
    max_file_bytes: int = 1048576,
    parent: Path | None = None,
) -> FrozenContext:
    """Copy eligible inputs and fail if their contents or modes change during capture.

    ``selected`` contains repository-relative files or directories. None includes the
    entire context, as required for Docker COPY semantics. Docker applies .dockerignore
    to the already sanitized snapshot, so negation cannot reintroduce private files.
    """
    root = PurePosixPath(context)
    if root.is_absolute() or ".." in root.parts or str(root) != context:
        raise AppError("invalid_context", "Build context path is unsafe.", 409)
    _, before = capture_source(source, max_file_bytes)
    prefix = "" if context == "." else context + "/"
    nested_repositories = {
        parent.as_posix() + "/"
        for path in before
        for parent in PurePosixPath(path).parents
        if str(parent) != "." and (source / parent / ".git").exists()
    }

    # Decide whether a repository path belongs in this build copy.
    # Respect the requested context and selected paths, and exclude nested repositories.
    # The same decision is used before and after copying to detect changed inputs consistently.
    def included(path: str) -> bool:
        if not path.startswith(prefix) or any(path.startswith(p) for p in nested_repositories):
            return False
        return selected is None or any(
            PurePosixPath(path).is_relative_to(PurePosixPath(item)) for item in selected
        )

    paths = sorted(path for path in before if included(path))
    if not paths or any(
        not any(p == item or p.startswith(item + "/") for p in paths) for item in required
    ):
        raise AppError("invalid_context", "Required build inputs are missing or excluded.", 409)
    directory = Path(tempfile.mkdtemp(prefix="voxops-context-", dir=parent))
    os.chmod(directory, 0o700)
    try:
        for path in paths:
            data = safe_read(source, path, max_file_bytes)
            if data is None or digest(data) != before[path]["hash"]:
                raise AppError("context_changed", "Build input changed during capture.", 409)
            target = directory / path[len(prefix) :]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(0o555 if before[path]["mode"] == "100755" else 0o444)
        _, after = capture_source(source, max_file_bytes)
        if {p: facts for p, facts in after.items() if included(p)} != {p: before[p] for p in paths}:
            raise AppError("context_changed", "Build input changed during capture.", 409)
        for folder, _, _ in os.walk(directory, topdown=False):
            Path(folder).chmod(0o500)
        identity = digest(
            {"files": [(p, before[p]) for p in paths], "config": identity_config or {}}
        )
        inventory = tuple((p[len(prefix) :], before[p]["hash"], before[p]["mode"]) for p in paths)
        return FrozenContext(directory, identity, tuple(paths), inventory, max_file_bytes)
    except BaseException:
        remove_context(FrozenContext(directory, "", ()))
        raise


# Compare the private build copy with the recorded files and modes.
# Reject added or missing paths, links, and changed contents.
# Run this before using cached results or handing inputs to Docker.
def verify_context(context: FrozenContext) -> None:
    expected_files = {path for path, _, _ in context.inventory}
    expected_dirs = {
        str(parent)
        for path in expected_files
        for parent in PurePosixPath(path).parents
        if str(parent) != "."
    }
    actual_files, actual_dirs = set(), set()
    for folder, dirs, files in os.walk(context.path, followlinks=False):
        for name in dirs + files:
            path = Path(folder) / name
            if path.is_symlink():
                raise AppError("context_changed", "Frozen build input changed before use.", 409)
            relative = path.relative_to(context.path).as_posix()
            (actual_dirs if name in dirs else actual_files).add(relative)
    if context.path.is_symlink() or actual_files != expected_files or actual_dirs != expected_dirs:
        raise AppError("context_changed", "Frozen build input changed before use.", 409)
    _, actual = capture_source(context.path, context.max_file_bytes)
    expected = {
        path: {"hash": checksum, "mode": mode} for path, checksum, mode in context.inventory
    }
    if actual != expected:
        raise AppError("context_changed", "Frozen build input changed before use.", 409)


# Delete the temporary build copy after it is no longer needed.
# Restore directory write permissions first because the copy was made read-only.
# If the path is a link, remove only the link rather than following it.
def remove_context(context: FrozenContext) -> None:
    if context.path.is_symlink():
        context.path.unlink()
        return
    if not context.path.exists():
        return
    for folder, dirs, _ in os.walk(context.path):
        Path(folder).chmod(0o700)
        for name in dirs:
            path = Path(folder) / name
            if not path.is_symlink():
                path.chmod(0o700)
    shutil.rmtree(context.path)
