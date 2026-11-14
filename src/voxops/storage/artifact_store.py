import hashlib
import json
import re
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

from pymongo.database import Database

from voxops.core.errors import AppError
from voxops.domain.artifact_models import ArtifactRun, TaskResult
from voxops.storage.mongodb import bounded_document


# Calculate a content checksum for bytes or JSON-safe structured values.
# Sorted keys make equivalent records produce the same checksum.
def digest(value: bytes | dict) -> str:
    data = (
        value
        if isinstance(value, bytes)
        else json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    )
    return hashlib.sha256(data).hexdigest()


# Keep runs, task ledgers and content-addressed artifacts in MongoDB.
# Temporary project files remain on disk, but no operational JSON or blobs do.
class ArtifactStore:
    _locks: dict[tuple[str, str], Lock] = {}
    _lock_guard = Lock()

    # Share the application database and retain existing file and staged-byte caps.
    # A separate task collection owns model attempts and their current results.
    def __init__(
        self,
        database: Database,
        max_file_bytes: int = 1048576,
        max_staged_bytes: int = 8388608,
    ):
        self.database = database
        self.max_file_bytes, self.max_staged_bytes = max_file_bytes, max_staged_bytes

    # Check generated identifiers before addressing saved records.
    # Submitted Mongo operators cannot become identifier queries.
    def identifier(self, value: str) -> str:
        if not re.fullmatch(r"[a-f0-9]{32,64}", value):
            raise AppError("invalid_identifier", "Unknown artifact identifier.", 404)
        return value

    # Save run metadata while keeping task history in its canonical collection.
    # Seed missing tasks only, so a stale run cannot overwrite a newer worker result.
    def save_run(self, run: ArtifactRun) -> None:
        run.updated_at = datetime.now(UTC)
        for task in run.tasks.values():
            self.database.artifact_tasks.update_one(
                {"_id": f"{run.run_id}:{task.task_id}"},
                {"$setOnInsert": {"run_id": run.run_id, "result": task.model_dump(mode="json")}},
                upsert=True,
            )
        self.database.artifact_runs.replace_one(
            {"_id": self.identifier(run.run_id)},
            bounded_document({"_id": run.run_id, **run.model_dump(mode="json", exclude={"tasks"})}),
            upsert=True,
        )

    # Load validated run metadata and assemble its current canonical task results.
    # Task histories are not duplicated inside the saved run document.
    def load_run(self, run_id: str) -> ArtifactRun:
        record = self.database.artifact_runs.find_one({"_id": self.identifier(run_id)})
        if record is None:
            raise AppError("run_not_found", "Unknown artifact run.", 404)
        record.pop("_id")
        record["tasks"] = {
            item["result"]["task_id"]: item["result"]
            for item in self.database.artifact_tasks.find({"run_id": run_id})
        }
        try:
            return ArtifactRun.model_validate(record)
        except ValueError:
            raise AppError("run_invalid", "Artifact run record is unavailable.", 409) from None

    # Read one task's latest result and its bounded-attempt history.
    # An unsaved task starts pending without allocating a disk file.
    def load_task(self, run_id: str, task_id: str) -> TaskResult:
        self.identifier(run_id)
        record = self.database.artifact_tasks.find_one({"_id": f"{run_id}:{task_id}"})
        try:
            return (
                TaskResult.model_validate(record["result"])
                if record
                else TaskResult(task_id=task_id, status="pending")
            )
        except ValueError:
            raise AppError("task_record_invalid", "Stored artifact task is invalid.", 409) from None

    # Find the active user-initiated attempt without loading the whole artifact run.
    # Internal retries and human-input resumes keep this same workflow identity.
    def workflow_attempt(self, run_id: str) -> str | None:
        record = self.database.artifact_runs.find_one(
            {"_id": self.identifier(run_id)}, {"workflow_attempt_id": 1}
        )
        return record.get("workflow_attempt_id") if record else None

    # Replace only this task's record so independent workers keep each other's data.
    # The generator still owns attempt accounting and accepted content checks.
    def save_task(self, run_id: str, result: TaskResult) -> TaskResult:
        self.identifier(run_id)
        self.database.artifact_tasks.replace_one(
            {"_id": f"{run_id}:{result.task_id}"},
            bounded_document(
                {
                    "_id": f"{run_id}:{result.task_id}",
                    "run_id": run_id,
                    "result": result.model_dump(mode="json"),
                }
            ),
            upsert=True,
        )
        return result

    # Save source authority separately from the public run's fields.
    # This records a temporary workspace path rather than preserving its contents.
    def bind_source(self, run_id: str, source: Path) -> None:
        self.database.artifact_sources.replace_one(
            {"_id": self.identifier(run_id)}, {"_id": run_id, "source": str(source)}, upsert=True
        )

    # Retrieve the binding for the publisher's existing path and ownership checks.
    # A missing source binding cannot authorize execution or publication.
    def source(self, run_id: str) -> Path:
        record = self.database.artifact_sources.find_one({"_id": self.identifier(run_id)})
        if record is None:
            raise AppError("source_unavailable", "Run source binding is unavailable.", 409)
        return Path(record["source"])

    # Save a plan together with its exact source authority for starting a run.
    # MongoDB retains the reviewed choices across application restarts.
    def save_plan(self, plan_id: str, record: dict) -> None:
        self.database.artifact_plans.replace_one(
            {"_id": self.identifier(plan_id)},
            bounded_document({"_id": plan_id, **record}),
            upsert=True,
        )

    # Load the saved plan rather than trusting replacement contents from a client.
    # An unknown plan must be created again through planning.
    def load_plan(self, plan_id: str) -> dict:
        record = self.database.artifact_plans.find_one({"_id": self.identifier(plan_id)})
        if record is None:
            raise AppError("plan_not_found", "Stored artifact plan is unavailable.", 404)
        record.pop("_id")
        return record

    # Store bounded bytes by checksum and check existing content before reuse.
    # A process lock keeps staged-byte accounting consistent on the single-worker VM.
    def stage(self, content: bytes) -> str:
        if len(content) > self.max_file_bytes:
            raise AppError("artifact_too_large", "Artifact exceeds the staged file limit.", 409)
        key = digest(content)
        with self._lock_guard:
            if self.database.artifact_blobs.find_one({"_id": key}):
                if self.read_blob(key) != content:
                    raise AppError("staged_content_changed", "Stored proposal is corrupt.", 409)
                return key
            size = sum(item["size"] for item in self.database.artifact_blobs.find({}, {"size": 1}))
            if size + len(content) > self.max_staged_bytes:
                raise AppError(
                    "staging_budget_exhausted", "Staged content budget is exhausted.", 409
                )
            self.database.artifact_blobs.insert_one(
                {"_id": key, "size": len(content), "content": content}
            )
        return key

    # Verify saved bytes and their checksum whenever an artifact is read.
    # Missing or corrupt blobs cannot supply reviewed project contents.
    def read_blob(self, key: str) -> bytes:
        record = self.database.artifact_blobs.find_one({"_id": self.identifier(key)})
        content = bytes(record["content"]) if record else b""
        if record is None or len(content) > self.max_file_bytes or digest(content) != key:
            raise AppError("staged_content_changed", "Staged proposal is unavailable.", 409)
        return content

    @contextmanager
    # Protect mutation of one temporary project inside the supported single process.
    # The workflow slot separately prevents concurrent active workflows.
    def project_lock(self, project: str):
        if not re.fullmatch(r"[a-f0-9]{16,64}", project):
            raise AppError("invalid_project", "Invalid repository fingerprint.")
        with self._lock_guard:
            lock = self._locks.setdefault((self.database.name, project), Lock())
        if not lock.acquire(blocking=False):
            raise AppError("project_busy", "Another operation owns this repository.", 409)
        try:
            yield
        finally:
            lock.release()
