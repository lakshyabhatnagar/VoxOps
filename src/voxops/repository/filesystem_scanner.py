import ast
import hashlib
import itertools
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from uuid import uuid4

from voxops.core.errors import AppError
from voxops.core.settings import Settings
from voxops.domain.models import Evidence, RepoScan
from voxops.repository.env_inspector import config_names
from voxops.repository.git_inspector import inspect_git
from voxops.repository.manifest_reader import read_manifest
from voxops.repository.path_policy import fingerprint
from voxops.repository.startup_reader import STARTUP_FILES, read_startup

DENIED = {
    ".git",
    "node_modules",
    ".next",
    "dist",
    "build",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".cache",
    ".pytest_cache",
    ".ruff_cache",
    "coverage",
    ".turbo",
    ".voxops",
    ".aws",
    ".ssh",
    ".codex",
    ".agents",
    ".idea",
    ".vscode",
}
CONFIG_NAMES = {
    "pnpm-workspace.yaml",
    "Dockerfile",
    ".dockerignore",
    ".gitignore",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lock",
    "bun.lockb",
    "uv.lock",
    "poetry.lock",
    "Procfile",
    "README.md",
}
SOURCE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts"}


# Removes JavaScript comments before checking configuration.
# Keeps quoted strings intact so their contents are not mistaken for comments.
def strip_js_comments(text: str) -> str:
    return re.sub(
        r"""("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)|//[^\n]*|/\*[\s\S]*?\*/""",
        keep_literal,
        text,
    )


# Handles one match while removing JavaScript comments.
# Returns quoted text unchanged and replaces a comment with whitespace.
def keep_literal(match: re.Match) -> str:
    return match.group(1) or " "


# Lists project files within the scanner's limits.
# Skips links and excluded directories while limiting depth and entries.
def bounded_files(root: Path, settings: Settings) -> tuple[list[Path], list[str]]:
    pending = [(root, 0)]
    files, warnings = [], []
    remaining = settings.max_files
    while pending and remaining > 0:
        directory, depth = pending.pop()
        try:
            with os.scandir(directory) as stream:
                entries = sorted(
                    itertools.islice(stream, remaining + 1), key=lambda item: item.name
                )
            if len(entries) > remaining:
                warnings.append("Scanner entry limit reached; findings may be incomplete.")
            for entry in entries[:remaining]:
                remaining -= 1
                if entry.is_symlink() or entry.name in DENIED:
                    continue
                path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    if depth < settings.max_depth:
                        pending.append((path, depth + 1))
                    else:
                        warnings.append("Scanner depth limit reached; findings may be incomplete.")
                elif entry.is_file(follow_symlinks=False):
                    files.append(path)
        except OSError:
            warnings.append("A directory could not be inspected.")
    if pending:
        warnings.append("Scanner entry limit reached; findings may be incomplete.")
    return sorted(files), sorted(set(warnings))


# Reads a regular project file within the size limit.
# Checks the file and avoids following a final symbolic link.
def read_bounded(path: Path, limit: int) -> str:
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
            raise ValueError("Not a bounded regular file")
        content = stream.read(limit + 1)
    if len(content) > limit or b"\x00" in content:
        raise ValueError("Unsupported file")
    return content.decode("utf-8")


# Finds FastAPI application names in Python source.
# Looks for where apps are created or returned by functions without running the code.
def fastapi_entries(text: str) -> list[tuple[str, str]]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return []
    aliases = {
        alias.asname or alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "fastapi"
        for alias in node.names
        if alias.name == "FastAPI"
    }
    modules = {
        alias.asname or "fastapi"
        for node in tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name == "fastapi"
    }
    entries: set[tuple[str, str]] = set()
    scopes = [(None, tree.body)] + [
        (node.name, node.body)
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and not node.decorator_list
        and len(node.args.posonlyargs + node.args.args) == len(node.args.defaults)
        and all(default is not None for default in node.args.kw_defaults)
    ]
    for owner, statements in scopes:
        instances = set()
        for node in statements:
            if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.Return)) or not isinstance(
                node.value, ast.Call
            ):
                continue
            target = node.value.func
            match = (isinstance(target, ast.Name) and target.id in aliases) or (
                isinstance(target, ast.Attribute)
                and target.attr == "FastAPI"
                and isinstance(target.value, ast.Name)
                and target.value.id in modules
            )
            if not match:
                continue
            if isinstance(node, ast.Return):
                if owner:
                    entries.add(("fastapi_factory", owner))
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            instances.update(item.id for item in targets if isinstance(item, ast.Name))
        if owner is None:
            entries.update(("fastapi_instance", name) for name in instances)
        elif any(
            isinstance(node, ast.Return)
            and isinstance(node.value, ast.Name)
            and node.value.id in instances
            for node in statements
        ):
            entries.add(("fastapi_factory", owner))
    factories = {name for kind, name in entries if kind == "fastapi_factory"}
    for node in tree.body:
        if (
            isinstance(node, (ast.Assign, ast.AnnAssign))
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id in factories
        ):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            entries.update(
                ("fastapi_instance", target.id)
                for target in targets
                if isinstance(target, ast.Name)
            )
    return sorted(entries)


# Extracts framework clues from source and configuration files.
# Keeps selected flags, output paths and application names instead of whole files.
def source_evidence(path: str, text: str) -> list[Evidence]:
    facts: list[tuple[str, str]] = []
    if not path.endswith(".py"):
        text = strip_js_comments(text)
    if path.endswith(".py"):
        facts.extend(fastapi_entries(text))
    if re.search(r"(?:^|/)next\.config\.", path):
        if re.search(r"output\s*:\s*['\"]export['\"]", text):
            facts.append(("next_static_export", "output: export"))
    if re.search(r"(?:^|/)vite\.config\.", path):
        if re.search(r"\blib\s*:", text):
            facts.append(("vite_library", "library build configuration"))
        match = re.search(r"outDir\s*:\s*['\"]([\w./-]+)['\"]", text)
        if match:
            facts.append(("vite_output", match[1]))
        elif "outDir" in text:
            facts.append(("vite_output_dynamic", "output directory needs review"))
        if re.search(r"\bssr\s*:", text):
            facts.append(("vite_server", "SSR configuration"))
    if re.search(r"(?:^|/)(?:src/)?app/(?:.*/)?route\.[jt]s$", path) or re.search(
        r"(?:^|/)(?:src/)?pages/api/.*\.[jt]sx?$", path
    ):
        facts.append(("next_api", "API route file"))
    if re.search(r"(?:^|/)(?:middleware|proxy)\.[jt]s$", path):
        facts.append(("next_server", "middleware file"))
    if re.search(r"(?:^|[;{\n])\s*['\"]use server['\"]", text):
        facts.append(("next_action", "server action"))
    if re.search(r"import\s*['\"]server-only['\"]|require\(['\"]server-only['\"]\)", text):
        facts.append(("next_server", "server-only capability"))
    if re.search(r"next\s*\(.*\)|require\(['\"]next['\"]\)", text) and "createServer" in text:
        facts.append(("next_server", "custom server"))
    return [Evidence(path=path, kind=kind, field=field, confidence=0.95) for kind, field in facts]


# Collects facts needed to analyze a project.
# Reads selected files within limits and records checksums for later comparison.
def scan_repository(root: Path, settings: Settings, scan_id: str | None = None) -> RepoScan:
    files, warnings = bounded_files(root, settings)
    manifests, evidence, relevant, config_keys = [], [], [], {}
    startup_commands = []
    digest = hashlib.sha256()
    for file in files:
        relative = file.relative_to(root).as_posix()
        name = file.name
        manifest = (
            name == "package.json"
            or name == "pyproject.toml"
            or bool(re.fullmatch(r"requirements.*\.txt", name))
        )
        env = name == ".env" or name.startswith(".env.")
        source = file.suffix in SOURCE_SUFFIXES
        targeted = manifest or env or source or name in CONFIG_NAMES
        if not targeted:
            continue
        relevant.append(relative)
        if name in CONFIG_NAMES and name not in STARTUP_FILES:
            metadata = file.stat()
            digest.update(f"{relative}:{metadata.st_size}:{metadata.st_mtime_ns}".encode())
            continue
        try:
            text = read_bounded(file, settings.max_file_bytes)
            digest.update(relative.encode() + text.encode())
            if manifest:
                summary = read_manifest(relative, text)
                if summary:
                    manifests.append(summary)
            if source or env:
                keys = config_names(
                    text,
                    env_file=env,
                    example=name in {".env.example", ".env.sample", ".env.template"},
                )
                if keys:
                    config_keys[relative] = keys
            if source:
                evidence.extend(source_evidence(relative, text))
            if name in STARTUP_FILES:
                startup_commands.extend(read_startup(relative, text))
        except (OSError, ValueError, TypeError, AttributeError, RecursionError):
            warnings.append(f"Skipped unreadable, oversized or malformed file: {relative}")
    metadata = inspect_git(root)
    if metadata.nested_path:
        warnings.append("Scan path is a subdirectory of a Git repository.")
    digest.update((metadata.head or "no-head").encode())
    try:
        return RepoScan(
            id=scan_id or uuid4().hex,
            scanned_at=datetime.now(UTC),
            repo_fingerprint=fingerprint(root),
            display_name=root.name,
            git=metadata,
            relevant_files=relevant,
            shallow_tree=[p.relative_to(root).as_posix() for p in files],
            manifests=manifests,
            workspace_roots=sorted(
                {item.root_path for item in manifests if item.workspace}
                | {
                    str(PurePosixPath(file).parent)
                    for file in relevant
                    if Path(file).name == "pnpm-workspace.yaml"
                }
            ),
            evidence=evidence,
            startup_commands=startup_commands,
            config_keys=config_keys,
            warnings=sorted(set(warnings)),
            input_fingerprint=digest.hexdigest(),
        )
    except ValueError:
        raise AppError("scan_invalid", "Repository metadata could not be validated.", 422) from None
