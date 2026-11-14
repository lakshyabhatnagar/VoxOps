from pymongo.database import Database
from pymongo.errors import PyMongoError

from voxops.core.errors import AppError
from voxops.domain.models import AnalysisResult
from voxops.storage.mongodb import bounded_document


# Save completed repository analyses in MongoDB without storing repository files.
# The same records supply exact-revision and relevant-content cache lookups.
class ScanStore:
    # Share one application database with artifacts and workspace ownership.
    # Collection identity comes from MongoDB rather than a local directory.
    def __init__(self, database: Database):
        self.database = database

    # Save a validated analysis under its fresh scan ID.
    # Replace the complete document atomically and return a safe error on failure.
    def save(self, result: AnalysisResult) -> None:
        try:
            self.database.repository_scans.replace_one(
                {"_id": result.scan.id},
                bounded_document({"_id": result.scan.id, "result": result.model_dump(mode="json")}),
                upsert=True,
            )
        except PyMongoError:
            raise AppError(
                "storage_unavailable", "MongoDB result storage is unavailable.", 503
            ) from None

    # Load one complete analysis and validate its saved data before use.
    # Unknown scan IDs receive a normal missing-record response.
    def load(self, scan_id: str) -> AnalysisResult:
        record = self.database.repository_scans.find_one({"_id": scan_id})
        if record is None:
            raise AppError("scan_not_found", "Unknown repository scan.", 404)
        return AnalysisResult.model_validate(record["result"])

    # Check the database connection for the readiness endpoint.
    # Credentials and driver exception details never enter the response.
    def ready(self) -> bool:
        try:
            return bool(self.database.command("ping").get("ok"))
        except PyMongoError:
            return False
