import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from uuid import uuid4

from voxops.core.errors import AppError
from voxops.core.settings import Settings
from voxops.domain.models import WorktreeRecord
from voxops.repository.git_inspector import git, inspect_git
from voxops.repository.path_policy import fingerprint, workspace_path
from voxops.storage.scan_store import ScanStore


# Why: Isolate per-run committed content without changing or deleting the original working tree.
# How: Manage recorded Git worktrees outside source repositories with explicit cleanup policy.
class WorktreeManager:
    # Why: Retain ownership records across application restarts.
    # How: Use an application-specific temporary root and a private local registry.
    def __init__(self, settings: Settings, store: ScanStore):
        self.settings, self.store = settings, store
        self.root = (
            Path(tempfile.gettempdir()).resolve() / f"voxops-{fingerprint(store.directory)}"
        )
        self.lock = RLock()

    # Why: Prevent arbitrary identifiers from escaping the registry directory.
    # How: Require the exact generated hexadecimal worktree identifier format.
    def record_path(self, identifier: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise AppError("worktree_not_found", "Unknown worktree identifier.", 404)
        return self.store.directory / f"worktree-{identifier}.json"

    # Why: Never treat an unknown filesystem path as an application-owned worktree.
    # How: Load a private typed record and verify its canonical managed destination.
    def load(self, identifier: str) -> tuple[WorktreeRecord, Path]:
        try:
            raw = json.loads(self.record_path(identifier).read_text())
            record = WorktreeRecord.model_validate(raw["record"])
            source = Path(raw["source"])
        except FileNotFoundError:
            raise AppError("worktree_not_found", "Unknown worktree identifier.", 404) from None
        except (OSError, ValueError, KeyError):
            raise AppError(
                "registry_invalid", "Worktree ownership record is unavailable.", 409
            ) from None
        destination = self.root / identifier
        if (
            record.id != identifier
            or Path(record.location) != destination
            or destination.is_symlink()
            or self.root.is_symlink()
            or destination.resolve() != destination.absolute()
        ):
            raise AppError("ownership_invalid", "Worktree ownership could not be verified.", 409)
        return record, source

    # Why: Keep lifecycle transitions durable and source paths private to local storage.
    # How: Save the public typed record with a separately stored executor-only source path.
    def save(self, record: WorktreeRecord, source: Path) -> None:
        self.store.write(
            f"worktree-{record.id}.json",
            {"record": record.model_dump(mode="json"), "source": str(source)},
        )

    # Why: Enforce worktree prerequisites instead of silently initializing or committing user files.
    # How: Inspect source state, check branch/destination and create a branch at verified HEAD.
    def create(self, value: str) -> WorktreeRecord:
        with self.lock:
            source = workspace_path(value, self.settings.allowed_roots)
            metadata = inspect_git(source)
            if metadata.status != "committed":
                raise AppError(
                    metadata.status,
                    "Worktree creation requires an initialized repository with an existing commit.",
                    409,
                )
            if metadata.nested_path:
                raise AppError(
                    "nested_repository_path", "Use the Git repository root for worktrees.", 409
                )
            if metadata.detached:
                raise AppError(
                    "detached_head", "Check out a source branch before creating a worktree.", 409
                )
            if self.root.is_symlink() or self.root.resolve().is_relative_to(source):
                raise AppError(
                    "unsafe_destination", "Managed worktrees must be outside the source.", 409
                )
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            ownership = self.root.stat()
            if os.name == "posix" and (
                ownership.st_uid != os.getuid() or ownership.st_mode & 0o022
            ):
                raise AppError(
                    "unsafe_destination",
                    "Managed worktree root must be private and owned by the local executor user.",
                    409,
                )
            identifier = uuid4().hex
            destination, branch = self.root / identifier, f"voxops/{identifier}"
            if destination.exists() or git(
                source, "show-ref", "--verify", f"refs/heads/{branch}", check=False
            ):
                raise AppError(
                    "worktree_collision", "Worktree branch or destination already exists.", 409
                )
            warnings = ["Worktree contains committed tracked content only."]
            if metadata.dirty:
                warnings.append(
                    "Source has local changes; uncommitted/untracked content is not copied."
                )
            record = WorktreeRecord(
                id=identifier,
                source_repo_fingerprint=fingerprint(source),
                source_commit_sha=metadata.head,
                branch=branch,
                location=str(destination),
                created_at=datetime.now(UTC),
                warnings=warnings,
                status="creating",
            )
            # Record ownership first so a storage failure cannot orphan an unknown worktree.
            self.save(record, source)
            try:
                git(source, "worktree", "add", "-b", branch, str(destination), metadata.head)
            except AppError:
                record.status = "creation_failed"
                self.save(record, source)
                raise
            record.status = "active"
            self.save(record, source)
            return self.inspect(identifier)

    # Why: Report live status while refusing to operate on replaced or unregistered directories.
    # How: Cross-check Git's worktree listing, branch and commit against the ownership record.
    def inspect(self, identifier: str) -> WorktreeRecord:
        with self.lock:
            record, source = self.load(identifier)
            if record.status == "removed":
                return record
            blocks = git(source, "worktree", "list", "--porcelain").split("\n\n")
            expected = f"worktree {record.location}"
            block = next((item for item in blocks if item.splitlines()[0] == expected), "")
            if not block and record.status in {"creating", "creation_failed", "cleanup_failed"}:
                record.dirty = bool(
                    git(source, "rev-parse", "--verify", f"refs/heads/{record.branch}", check=False)
                    not in {"", record.source_commit_sha}
                )
                return record
            if not block or f"branch refs/heads/{record.branch}" not in block.splitlines():
                raise AppError(
                    "worktree_missing", "Recorded Git worktree is missing or changed.", 409
                )
            if not Path(record.location).is_dir():
                raise AppError(
                    "worktree_missing", "Recorded worktree directory is unavailable.", 409
                )
            record.dirty = bool(
                git(Path(record.location), "status", "--porcelain", "--untracked-files=all")
            )
            current_head = git(Path(record.location), "rev-parse", "--verify", "HEAD")
            if current_head != record.source_commit_sha:
                record.dirty = True
                record.warnings.append(
                    "Worktree has new commits; cleanup requires explicit discard consent."
                )
            return record

    # Why: Expose only recorded worktrees rather than arbitrary Git destinations.
    # How: Inspect each application registry entry and return its current lifecycle metadata.
    def list(self) -> list[WorktreeRecord]:
        return [
            self.inspect(path.stem.removeprefix("worktree-"))
            for path in sorted(self.store.directory.glob("worktree-*.json"))
        ]

    # Why: Protect changes in temporary worktrees and never force-remove unknown directories.
    # How: Verify ownership, require discard consent and remove only recorded branches.
    def remove(self, identifier: str, discard_changes: bool = False) -> WorktreeRecord:
        with self.lock:
            record = self.inspect(identifier)
            if record.status == "removed":
                return record
            if record.dirty and not discard_changes:
                raise AppError(
                    "dirty_worktree",
                    "Worktree contains changes; explicitly set "
                    "discard_changes=true to discard them.",
                    409,
                )
            _, source = self.load(identifier)
            args = ["worktree", "remove"]
            if discard_changes:
                args.append("--force")
            try:
                # Persist cleanup intent so a failed branch deletion can be retried safely.
                record.status = "cleanup_failed"
                self.save(record, source)
                if Path(record.location).exists():
                    git(source, *args, record.location)
                # Only the generated branch is deleted; the source branch is never changed.
                if git(source, "show-ref", "--verify", f"refs/heads/{record.branch}", check=False):
                    git(source, "branch", "-D", record.branch)
            except AppError:
                raise AppError(
                    "worktree_removal_failed",
                    "Cleanup failed; ownership record retained for manual inspection.",
                    409,
                ) from None
            record.status, record.dirty = "removed", False
            self.save(record, source)
            return record
