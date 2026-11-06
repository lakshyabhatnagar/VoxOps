import hashlib
from pathlib import Path

from voxops.core.errors import AppError


# Creates an identifier for a local project directory.
# Calculates a checksum of its resolved path without exposing the path.
def fingerprint(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:24]


# Checks that a requested project is in an allowed directory.
# Rejects paths containing ".." and checks where the directory actually lives.
def workspace_path(value: str, roots: list[Path]) -> Path:
    path = Path(value).expanduser()
    if ".." in path.parts:
        raise AppError("path_traversal", "Parent traversal is not permitted.")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise AppError("path_missing", "Workspace directory is unavailable.") from None
    if not resolved.is_dir() or not any(resolved.is_relative_to(root.resolve()) for root in roots):
        raise AppError("path_forbidden", "Directory is outside configured workspace roots.", 403)
    return resolved
