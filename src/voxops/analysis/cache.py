import asyncio
import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from voxops.domain.models import AnalysisResult
from voxops.repository.env_inspector import config_names
from voxops.repository.filesystem_scanner import (
    CONFIG_NAMES,
    SOURCE_SUFFIXES,
    bounded_files,
    read_bounded,
)
from voxops.repository.git_inspector import inspect_git
from voxops.repository.path_policy import fingerprint
from voxops.storage.artifact_store import digest


# Hash bounded analysis inputs without parsing frameworks or calling a model.
# Environment files contribute names only; checkout timestamps and HEAD are excluded.
def relevant_inputs(root, settings):
    files, warnings = bounded_files(root, settings)
    content = hashlib.sha256()
    for path in files:
        name = path.name
        env = name == ".env" or name.startswith(".env.")
        if not (
            env
            or name in CONFIG_NAMES
            or path.suffix in SOURCE_SUFFIXES
            or name in {"package.json", "pyproject.toml"}
            or re.fullmatch(r"requirements.*\.txt", name)
        ):
            continue
        try:
            text = read_bounded(path, settings.max_file_bytes)
        except (OSError, ValueError, UnicodeError):
            return None, files
        if env:
            text = "\n".join(key.name for key in config_names(text, env_file=True))
        content.update(path.relative_to(root).as_posix().encode() + b"\0" + text.encode() + b"\0")
    return (None if warnings else content.hexdigest()), files


# Bind cache records to the analyzer code, bounded settings and model mode.
# Credential availability affects fallback behavior, but credential values never enter the key.
def analyzer_identity(settings, use_llm, available):
    package = Path(__file__).resolve().parents[1]
    paths = [
        *package.joinpath("analysis").glob("*.py"),
        *package.joinpath("frameworks").glob("*.py"),
        *package.joinpath("repository").glob("*.py"),
        package / "workflows/stage1_graph.py",
        package / "agents/topology_agent.py",
        package / "domain/models.py",
    ]
    return digest(
        {
            "code": digest(
                b"".join(
                    p.relative_to(package).as_posix().encode() + p.read_bytes()
                    for p in sorted(paths)
                )
            ),
            "settings": {
                key: getattr(settings, key)
                for key in (
                    "max_depth",
                    "max_files",
                    "max_file_bytes",
                    "max_context_chars",
                    "model_timeout",
                )
            },
            "use_llm": use_llm,
            "model": settings.model,
            "model_available": available,
        }
    )


# Reuse complete analysis while refreshing its scan, workspace and commit identity.
# Cache position is independent of Stage 2 source and publication authority checks.
class AnalysisCache:
    # Reuse the current Stage 1 algorithm and the same MongoDB scan collection.
    # Unique cache keys point to complete saved analyses rather than duplicate result payloads.
    def __init__(self, workflow):
        self.workflow = workflow
        self.database = workflow.store.database

    # Resolve exact revision first, then relevant content, before running Stage 1.
    # Every returned result receives a fresh scan identity and current Git/path metadata.
    async def analyze(self, root: Path, repository: str, use_llm=False) -> AnalysisResult:
        settings = self.workflow.settings
        policy = await asyncio.to_thread(
            analyzer_identity, settings, use_llm, self.workflow.resolver.available()
        )
        metadata = await asyncio.to_thread(inspect_git, root)
        exact = digest({"repository": repository.lower(), "sha": metadata.head, "policy": policy})
        record = await asyncio.to_thread(self.database.analysis_cache.find_one, {"_id": exact})
        content, files = None, None
        if record is None:
            content, files = await asyncio.to_thread(relevant_inputs, root, settings)
            if content is not None:
                record = await asyncio.to_thread(
                    self.database.analysis_cache.find_one,
                    {
                        "repository": repository.lower(),
                        "content": content,
                        "policy": policy,
                    },
                )
        if record:
            result = await asyncio.to_thread(self.workflow.store.load, record["scan_id"])
            result = result.model_copy(deep=True)
            result.scan.id, result.scan.scanned_at = uuid4().hex, datetime.now(UTC)
            result.scan.git = metadata
            result.scan.repo_fingerprint = fingerprint(root)
            result.scan.display_name = root.name
            if files is not None:
                result.scan.shallow_tree = [p.relative_to(root).as_posix() for p in files]
            await asyncio.to_thread(self.workflow.store.save, result)
            content = record["content"]
        else:
            result = await self.workflow.analyze(str(root), use_llm)
            if content is None:
                content, _ = await asyncio.to_thread(relevant_inputs, root, settings)
        if content is not None and not result.scan.warnings and result.ai_usage.status != "failed":
            await asyncio.to_thread(
                self.database.analysis_cache.replace_one,
                {"_id": exact},
                {
                    "_id": exact,
                    "repository": repository.lower(),
                    "sha": metadata.head,
                    "content": content,
                    "policy": policy,
                    "scan_id": result.scan.id,
                },
                upsert=True,
            )
        return result
