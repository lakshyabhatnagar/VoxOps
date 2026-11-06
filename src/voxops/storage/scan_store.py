import json
import os
import tempfile
from pathlib import Path
from threading import RLock

from voxops.core.errors import AppError
from voxops.domain.models import AnalysisResult


# Stores the results of local project scans.
# Writes private JSON files outside the scanned project.
class ScanStore:
    # Sets up the directory used for scan records.
    # Resolves the configured path and creates a lock for writes.
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.lock = RLock()

    # Saves a JSON record without leaving a partial result.
    # Writes and flushes a private temporary file before replacing the destination.
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

    # Saves the result of a project scan.
    # Uses the scan's generated identifier as its record name.
    def save(self, result: AnalysisResult) -> None:
        self.write(f"scan-{result.scan.id}.json", result.model_dump(mode="json"))

    # Checks whether local scan storage is usable.
    # Creates and removes a private test file in the storage directory.
    def ready(self) -> bool:
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.TemporaryFile(dir=self.directory):
                return True
        except OSError:
            return False
