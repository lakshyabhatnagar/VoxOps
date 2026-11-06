import difflib
import os
from pathlib import Path

from voxops.artifacts.security import check_secret, safe_read
from voxops.core.errors import AppError
from voxops.domain.artifact_models import ReviewedFile, artifact_path
from voxops.storage.artifact_store import ArtifactStore, digest

EXCLUDED_DIRS = {
    ".git",
    ".voxops",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".next",
    "dist",
    "build",
    "coverage",
    ".pytest_cache",
    ".ruff_cache",
}


# Lists project paths omitted from a source snapshot.
# Records excluded names without reading the files inside them.
def source_exclusions(source: Path) -> list[str]:
    excluded = []
    visited = 0
    for folder, dirs, files in os.walk(source, followlinks=False):
        descend = []
        for name in sorted(dirs):
            path = Path(folder) / name
            if (
                name in EXCLUDED_DIRS
                or (name.startswith(".") and name != ".github")
                or path.is_symlink()
            ):
                excluded.append(path.relative_to(source).as_posix() + "/")
            else:
                descend.append(name)
        dirs[:] = descend
        for name in sorted(files):
            visited += 1
            if visited > 20000:
                raise AppError(
                    "source_too_large", "Source exclusion inventory exceeds its limit.", 409
                )
            relative = (Path(folder) / name).relative_to(source).as_posix()
            try:
                artifact_path(relative)
            except ValueError:
                excluded.append(relative)
    return sorted(excluded)


# Records the files eligible for a source snapshot.
# Skips excluded paths and links while recording file checksums and permissions.
def capture_source(source: Path, max_bytes: int = 131072) -> tuple[str, dict[str, dict]]:
    inventory = {}
    visited = 0
    for folder, dirs, files in os.walk(source, followlinks=False):
        dirs[:] = sorted(
            name
            for name in dirs
            if name not in EXCLUDED_DIRS
            and (not name.startswith(".") or name == ".github")
            and not (Path(folder) / name).is_symlink()
        )
        for name in sorted(files):
            visited += 1
            if visited > 20000:
                raise AppError("source_too_large", "Source inventory exceeds the file limit.", 409)
            path = Path(folder) / name
            relative = path.relative_to(source).as_posix()
            try:
                artifact_path(relative)
            except ValueError:
                continue
            if path.is_symlink():
                raise AppError("unsafe_source", "Source symlinks require manual resolution.", 409)
            data = safe_read(source, relative, max_bytes)
            if data is not None:
                inventory[relative] = {
                    "hash": digest(data),
                    "mode": "100755" if path.stat().st_mode & 0o111 else "100644",
                }
    exclusions = [path for path in source_exclusions(source) if path not in {".git", ".git/"}]
    return digest({"files": inventory, "excluded_paths": exclusions}), inventory


# Creates an identifier for the current source files.
# Returns the checksum of their contents and permissions without retaining them.
def source_fingerprint(source: Path) -> str:
    return capture_source(source)[0]


# Saves the source files selected for publication.
# Checks their contents for secrets and stores copies for later review.
def snapshot_files(
    source: Path,
    selected: list[str],
    store: ArtifactStore,
    before: dict[str, dict] | None = None,
    category: str = "baseline",
) -> list[ReviewedFile]:
    if not selected or len(set(selected)) != len(selected):
        raise AppError("source_selection_required", "Select distinct source files explicitly.", 409)
    original = before or {}
    result = []
    for path in sorted(selected):
        try:
            artifact_path(path)
        except ValueError:
            raise AppError(
                "unsafe_source", "Selected source path is not safe to publish.", 409
            ) from None
        if any(part in EXCLUDED_DIRS for part in Path(path).parts):
            raise AppError("unsafe_source", "Generated or metadata paths cannot be selected.", 409)
        data = safe_read(source, path, store.max_file_bytes)
        old = original.get(path)
        if data is None and old is None:
            raise AppError("source_missing", "Selected source file is unavailable.", 409)
        if data is not None:
            check_secret(path, data, allow_binary=True)
        result.append(
            ReviewedFile(
                path=path,
                category=category,
                before_hash=old["hash"] if old else None,
                after_hash=digest(data) if data is not None else None,
                staged_ref=store.stage(data) if data is not None else None,
                mode=("100755" if (source / path).stat().st_mode & 0o111 else "100644")
                if data is not None
                else old["mode"],
            )
        )
    return result


# Describes the changes between two versions of a file.
# Shows text differences along with checksums, deletion and permission changes.
def file_diff(
    files: list[ReviewedFile], store: ArtifactStore, before_bytes: dict[str, bytes] | None = None
) -> str:
    parts = []
    for file in files:
        old = (before_bytes or {}).get(file.path, b"")
        new = store.read_blob(file.staged_ref) if file.staged_ref else b""
        parts.append(
            f"diff --voxops a/{file.path} b/{file.path}\n"
            f"mode {file.mode}; before {file.before_hash}; after {file.after_hash}\n"
        )
        try:
            old_text, new_text = old.decode("utf-8"), new.decode("utf-8")
            if "\x00" in old_text + new_text:
                raise UnicodeError
            for line in difflib.unified_diff(
                old_text.splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile=f"a/{file.path}",
                tofile=f"b/{file.path}",
            ):
                parts.append(
                    line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                )
        except UnicodeError:
            parts.append(f"Binary content ({len(old)} -> {len(new)} bytes); hashes shown above.\n")
    result = "".join(parts)
    if len(result) > 2097152:
        raise AppError("review_too_large", "Selected changes exceed the review size limit.", 409)
    return result
