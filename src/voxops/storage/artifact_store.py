import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from voxops.core.errors import AppError
from voxops.domain.artifact_models import ArtifactRun
from voxops.storage.scan_store import ScanStore


# Calculates a checksum for file bytes or structured data.
# Sorts JSON keys so equivalent records produce the same checksum.
def digest(value: bytes | dict) -> str:
    data = (
        value
        if isinstance(value, bytes)
        else json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    )
    return hashlib.sha256(data).hexdigest()


# Stores run records and generated files outside the project.
# Uses private files and permits one project operation at a time.
class ArtifactStore:
    # Sets up storage for runs and generated files.
    # Applies separate limits to each file and the total saved content.
    def __init__(
        self, directory: Path, max_file_bytes: int = 131072, max_staged_bytes: int = 8388608
    ):
        self.records = ScanStore(directory)
        self.directory = self.records.directory
        self.max_file_bytes = max_file_bytes
        self.max_staged_bytes = max_staged_bytes

    # Checks an identifier used in a storage filename.
    # Accepts only the expected generated IDs and checksums.
    def identifier(self, value: str) -> str:
        if not re.fullmatch(r"[a-f0-9]{32,64}", value):
            raise AppError("invalid_identifier", "Unknown Stage 2 identifier.", 404)
        return value

    # Saves the current state of a generation run.
    # Updates its timestamp and replaces the stored record in one step.
    def save_run(self, run: ArtifactRun) -> None:
        run.updated_at = datetime.now(UTC)
        self.records.write(
            f"artifact-run-{self.identifier(run.run_id)}.json", run.model_dump(mode="json")
        )

    # Loads a saved generation run.
    # Checks the record and reports missing or damaged data.
    def load_run(self, run_id: str) -> ArtifactRun:
        try:
            data = (self.directory / f"artifact-run-{self.identifier(run_id)}.json").read_text()
            return ArtifactRun.model_validate_json(data)
        except FileNotFoundError:
            raise AppError("run_not_found", "Unknown artifact run.", 404) from None
        except (OSError, ValueError):
            raise AppError("run_invalid", "Artifact run record is unavailable.", 409) from None

    # Saves the local source path associated with a run.
    # Keeps that path separate from the run's public details.
    def bind_source(self, run_id: str, source: Path) -> None:
        self.records.write(
            f"artifact-source-{self.identifier(run_id)}.json",
            {"schema_version": "1", "source": str(source)},
        )

    # Loads the saved source path for a run.
    # Returns the private binding for the caller to check against allowed directories.
    def source(self, run_id: str) -> Path:
        try:
            raw = json.loads(
                (self.directory / f"artifact-source-{self.identifier(run_id)}.json").read_text()
            )
            return Path(raw["source"])
        except (OSError, ValueError, KeyError):
            raise AppError(
                "source_unavailable", "Run source binding is unavailable.", 409
            ) from None

    # Saves a generated file for later review.
    # Names the copy after its checksum and enforces file and storage limits.
    def stage(self, content: bytes) -> str:
        if len(content) > self.max_file_bytes:
            raise AppError("artifact_too_large", "Artifact exceeds the staged file limit.", 409)
        key = digest(content)
        folder = self.directory / "artifact-blobs"
        with self.records.lock:
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            if folder.is_symlink() or folder.stat().st_mode & 0o077:
                raise AppError("unsafe_storage", "Staging directory must be owner-private.", 409)
            path = folder / key
            if path.exists():
                if self.read_blob(key) != content:
                    raise AppError("staged_content_changed", "Stored proposal is corrupt.", 409)
                return key
            size = sum(item.stat().st_size for item in folder.iterdir() if item.is_file())
            if size + len(content) > self.max_staged_bytes:
                raise AppError(
                    "staging_budget_exhausted", "Staged content budget is exhausted.", 409
                )
            with tempfile.NamedTemporaryFile(dir=folder, delete=False) as file:
                temporary = Path(file.name)
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            try:
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        return key

    # Reads a previously saved generated file.
    # Checks its size, permissions and checksum before returning its contents.
    def read_blob(self, key: str) -> bytes:
        self.identifier(key)
        path = self.directory / "artifact-blobs" / key
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as file:
                metadata = os.fstat(file.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
                    raise OSError("Unsafe blob")
                data = file.read(self.max_file_bytes + 1)
            if len(data) > self.max_file_bytes or digest(data) != key:
                raise OSError("Changed blob")
            return data
        except OSError:
            raise AppError(
                "staged_content_changed", "Staged proposal is unavailable.", 409
            ) from None

    @contextmanager
    # Allows one operation on a project at a time.
    # Holds a local file lock until the operation finishes.
    def project_lock(self, project: str):
        if not re.fullmatch(r"[a-f0-9]{16,64}", project):
            raise AppError("invalid_project", "Invalid repository fingerprint.")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.directory / f"artifact-lock-{project}"
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AppError(
                    "project_busy", "Another operation owns this repository.", 409
                ) from None
            yield
        finally:
            os.close(descriptor)
