import json
import os
import tempfile
from pathlib import Path
from threading import RLock

from voxops.core.errors import AppError
from voxops.domain.models import AnalysisResult


# Why: Persist local scan results independently of future hosted databases or checkpoints.
# How: Write validated JSON through owner-private temporary files and atomic replacement.
class ScanStore:
    # Why: Keep storage ownership and synchronization scoped to one application instance.
    # How: Canonicalize the configured local data directory and use a reentrant write lock.
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.lock = RLock()

    # Why: Provide durable, complete JSON records despite interrupted writes.
    # How: Flush a private temporary file before atomically replacing the destination.
    def write(self, name: str, value: dict) -> None:
        with self.lock:
            try:
                self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                with tempfile.NamedTemporaryFile(
                    mode="w", dir=self.directory, delete=False
                ) as file:
                    temporary = Path(file.name)
                    json.dump(value, file, ensure_ascii=True)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary, self.directory / name)
            except OSError:
                raise AppError(
                    "storage_unavailable", "Local result storage is unavailable.", 503
                ) from None
            finally:
                if "temporary" in locals():
                    temporary.unlink(missing_ok=True)

    # Why: Persist only the validated, value-sanitized aggregate contract.
    # How: Serialize the typed result under its generated scan identifier.
    def save(self, result: AnalysisResult) -> None:
        self.write(f"scan-{result.scan.id}.json", result.model_dump(mode="json"))

    # Why: Report readiness of the only required local service accurately.
    # How: Exercise private file creation and deletion without needing model/STT credentials.
    def ready(self) -> bool:
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.TemporaryFile(dir=self.directory):
                return True
        except OSError:
            return False
