import os
import re
import shutil
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from uuid import uuid4

from pydantic import Field

from voxops.core.errors import AppError
from voxops.domain.models import Model
from voxops.repository.git_executor import GitExecutor


# Describe one exact fetched source and the temporary checkout that belongs to it.
# The durable record preserves identity and cleanup authority, never repository contents.
class RepositorySource(Model):
    id: str = Field(default_factory=lambda: uuid4().hex)
    repository: str
    url: str
    ref: str
    commit_sha: str
    path: str
    created_at: datetime
    expires_at: datetime
    status: str = "active"


# Accept only a plain GitHub HTTPS repository address and normalize its spelling.
# Credentials, query parameters, alternate hosts and local filesystem paths are rejected.
def github_source(value: str) -> tuple[str, str]:
    match = re.fullmatch(
        r"https://github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38})/"
        r"([A-Za-z0-9][A-Za-z0-9_.-]{0,99}?)(?:\.git)?/?",
        value,
    )
    if not match or match[2] in {".", ".."}:
        raise AppError("invalid_repository", "Supply a GitHub HTTPS repository URL.", 422)
    identity = f"{match[1]}/{match[2]}"
    return identity.lower(), f"https://github.com/{identity}.git"


# Fetch immutable source into an owned disposable directory using the existing trusted Git runner.
# MongoDB records ownership; source bytes exist only in temporary Git objects and working files.
class RepositoryAcquirer:
    # Share the application settings and database with the existing Git safety checks.
    # A database-specific private root keeps test and application workspaces separate.
    def __init__(self, settings, database, executor=None):
        self.settings, self.database = settings, database
        self.executor = executor or GitExecutor(settings)
        self.root = Path(tempfile.gettempdir()).resolve() / f"voxops-sources-{database.name}"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = self.root.lstat()
        if self.root.is_symlink() or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise AppError("unsafe_workspace", "Temporary source root must be owner-private.", 409)

    # Resolve a validated ref, read checked Git blobs, and record the exact fetched commit.
    # Full branch history preserves the publisher's existing refusal of shallow source history.
    def acquire(self, url: str, ref: str | None = None, *, cancel=None) -> RepositorySource:
        repository, transport = github_source(url)
        cancel = cancel or Event()
        ref = ref or "HEAD"
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,199}", ref) or ".." in ref:
            raise AppError("invalid_ref", "Supply a branch, tag, or commit SHA.", 422)
        source_id = uuid4().hex
        destination = self.root / source_id
        destination.mkdir(mode=0o700)
        created = datetime.now(UTC)
        self.database.sources.insert_one(
            {
                "_id": source_id,
                "path": str(destination),
                "repository": repository,
                "created_at": created.isoformat(),
                "status": "acquiring",
            }
        )
        try:
            self.executor.run(destination, "init", "-b", "source")
            self.executor.run(
                destination, "fetch", "--no-tags", self.executor.transport(transport), ref
            )
            sha = (
                self.executor.run(destination, "rev-parse", "--verify", "FETCH_HEAD^{commit}")
                .decode()
                .strip()
            )
            if not re.fullmatch(r"[a-f0-9]{40}", sha):
                raise AppError("invalid_revision", "Git did not resolve an immutable commit.", 409)
            files = self.executor.tree(destination, sha, cancel=cancel)
            for relative, item in files.items():
                if cancel.is_set():
                    raise AppError("CANCELLED", "Source acquisition was cancelled.", 409)
                path = destination / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(self.executor.blob(destination, item["object"]))
                path.chmod(0o755 if item["mode"] == "100755" else 0o644)
            self.executor.run(destination, "update-ref", "refs/heads/source", sha)
            self.executor.run(destination, "read-tree", sha)
            self.executor.run(destination, "remote", "add", "origin", transport)
            source = RepositorySource(
                id=source_id,
                repository=repository,
                url=transport,
                ref=ref,
                commit_sha=sha,
                path=str(destination),
                created_at=created,
                expires_at=created
                + timedelta(minutes=self.settings.workspace_max_lifetime_minutes),
            )
            self.database.sources.replace_one(
                {"_id": source_id}, {"_id": source_id, **source.model_dump(mode="json")}
            )
            return source
        except BaseException:
            shutil.rmtree(destination)
            self.database.sources.update_one({"_id": source_id}, {"$set": {"status": "failed"}})
            raise

    # Load a saved source only while its owned directory and immutable commit remain available.
    # A checkpoint cannot reconstruct a deleted or expired checkout.
    def load(self, source_id: str) -> RepositorySource:
        record = self.database.sources.find_one({"_id": source_id})
        if record is None or record.get("status") != "active":
            raise AppError("workspace_expired", "Source workspace is no longer available.", 409)
        record.pop("_id")
        source = RepositorySource.model_validate(record)
        path = self.root / source.id
        if (
            Path(source.path) != path
            or path.is_symlink()
            or not path.is_dir()
            or source.expires_at <= datetime.now(UTC)
        ):
            raise AppError("workspace_expired", "Source workspace expired or was removed.", 409)
        if self.executor.head(path) != source.commit_sha:
            raise AppError("source_changed", "Fetched source revision changed.", 409)
        return source

    # Remove only the source directory named by its own durable ownership record.
    # Repeated cleanup leaves the same removed record and never touches other checkouts.
    def remove(self, source_id: str) -> None:
        record = self.database.sources.find_one({"_id": source_id})
        if record is None:
            return
        path = self.root / source_id
        if record["path"] != str(path) or path.is_symlink():
            raise AppError("ownership_invalid", "Temporary source ownership changed.", 409)
        if path.exists():
            shutil.rmtree(path)
        self.database.sources.update_one({"_id": source_id}, {"$set": {"status": "removed"}})
