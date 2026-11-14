import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from voxops.artifacts.security import check_secret
from voxops.core.errors import AppError
from voxops.domain.artifact_models import ReviewedFile, artifact_path
from voxops.storage.artifact_store import ArtifactStore, digest

COMMANDS = {
    "init",
    "rev-parse",
    "symbolic-ref",
    "show-ref",
    "remote",
    "config",
    "status",
    "ls-tree",
    "cat-file",
    "rev-list",
    "read-tree",
    "write-tree",
    "hash-object",
    "update-index",
    "commit-tree",
    "update-ref",
    "worktree",
    "ls-remote",
    "push",
    "fetch",
}


# Runs the Git operations needed for publication.
# Restricts commands and settings so project configuration cannot run arbitrary helpers.
class GitExecutor:
    # Sets up Git execution using trusted application settings.
    # Keeps credential and command limits under host control.
    def __init__(self, settings):
        self.settings = settings

    # Checks a repository address before Git uses it.
    # Accepts only a GitHub HTTPS address without credentials or query parameters.
    def transport(self, url: str) -> str:
        if not re.fullmatch(r"https://github\.com/[A-Za-z0-9-]+/[A-Za-z0-9_.-]+\.git", url):
            raise AppError(
                "unsafe_transport", "Only canonical GitHub HTTPS transport is supported.", 409
            )
        return url

    # Builds the environment passed to Git commands.
    # Keeps necessary host values and disables implicit credential sources.
    def environment(self) -> dict[str, str]:
        environment = {
            key: os.environ[key]
            for key in ("PATH", "HOME", "TMPDIR", "SYSTEMROOT")
            if key in os.environ
        }
        environment.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_ATTR_NOSYSTEM="1",
            GIT_TERMINAL_PROMPT="0",
            GIT_NO_REPLACE_OBJECTS="1",
            GIT_OPTIONAL_LOCKS="0",
            GIT_ASKPASS="/bin/false",
            SSH_ASKPASS="/bin/false",
            GIT_SSH_COMMAND="/bin/false",
            GIT_PROTOCOL_FROM_USER="0",
        )
        return environment

    # Checks repository settings that could redirect Git operations.
    # Rejects unsafe includes and transport changes before running a command.
    def config_overrides(self, path: Path, environment: dict[str, str]) -> list[str]:
        try:
            result = subprocess.run(
                ["git", "-C", str(path), "config", "--local", "--no-includes", "--null", "--list"],
                env=environment,
                capture_output=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise AppError(
                "git_config_invalid", "Git configuration could not be inspected.", 409
            ) from None
        if result.returncode not in {0, 128}:
            raise AppError("git_config_invalid", "Git configuration could not be inspected.", 409)
        overrides = []
        for entry in result.stdout.split(b"\0"):
            key = entry.split(b"\n", 1)[0].decode("utf-8", "replace").lower()
            if (
                key.startswith(
                    (
                        "include.",
                        "includeif.",
                        "url.",
                        "credential.",
                        "http.",
                        "https.",
                        "protocol.",
                        "core.sshcommand",
                        "core.gitproxy",
                    )
                )
                or key in {"core.worktree", "extensions.worktreeconfig"}
                or (key.startswith("remote.") and not key.endswith((".url", ".fetch")))
            ):
                raise AppError(
                    "unsafe_git_config", "Remove unsafe repository Git configuration.", 409
                )
            if key.startswith("filter.") and key.endswith(
                (".clean", ".smudge", ".process", ".required")
            ):
                overrides += ["-c", f"{key}={'false' if key.endswith('.required') else ''}"]
        return overrides

    # Runs an allowed Git command and returns its output bytes.
    # Disables hooks and other integrations and limits execution time.
    def run(
        self,
        path: Path,
        *args: str,
        data: bytes | None = None,
        check: bool = True,
        index: Path | None = None,
        timestamp: str | None = None,
    ) -> bytes:
        if not args or args[0] not in COMMANDS or not shutil.which("git"):
            raise AppError("git_unavailable", "Required trusted Git operation is unavailable.", 503)
        environment = self.environment()
        overrides = self.config_overrides(path, environment)
        environment.update(
            GIT_AUTHOR_NAME=getattr(self.settings, "git_author_name", "VoxOps"),
            GIT_AUTHOR_EMAIL=getattr(self.settings, "git_author_email", "voxops@localhost"),
            GIT_COMMITTER_NAME=getattr(self.settings, "git_author_name", "VoxOps"),
            GIT_COMMITTER_EMAIL=getattr(self.settings, "git_author_email", "voxops@localhost"),
        )
        if index:
            environment["GIT_INDEX_FILE"] = str(index)
        if timestamp:
            environment.update(GIT_AUTHOR_DATE=timestamp, GIT_COMMITTER_DATE=timestamp)
        options = {
            "core.fsmonitor": "false",
            "core.hooksPath": os.devnull,
            "core.attributesFile": os.devnull,
            "core.autocrlf": "false",
            "core.safecrlf": "false",
            "core.untrackedCache": "false",
            "core.sshCommand": "/bin/false",
            "submodule.recurse": "false",
            "commit.gpgSign": "false",
            "tag.gpgSign": "false",
            "push.gpgSign": "false",
            "credential.helper": "",
            "credential.interactive": "false",
            "diff.external": "",
            "protocol.allow": "never",
            "protocol.https.allow": "always",
            "http.followRedirects": "false",
            "http.sslVerify": "true",
            "push.recurseSubmodules": "no",
            "push.followTags": "false",
        }
        command = ["git", "--no-optional-locks"]
        for key, value in options.items():
            command += ["-c", f"{key}={value}"]
        helper = getattr(self.settings, "git_credential_helper", None)
        if helper:
            command += ["-c", f"credential.helper={helper}"]
        command += overrides + ["-C", str(path), *args]
        try:
            result = subprocess.run(
                command,
                env=environment,
                input=data,
                capture_output=True,
                timeout=getattr(self.settings, "git_timeout", 30),
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise AppError(
                "git_timeout", "Git operation timed out; retry reconciliation.", 504
            ) from None
        except OSError:
            raise AppError("git_unavailable", "Git could not start.", 503) from None
        if result.returncode and check:
            raise AppError(
                "git_failed", "Git operation failed; verify credentials and repository state.", 409
            )
        return result.stdout if result.returncode == 0 else b""

    # Finds the current commit of a repository.
    # Uses a fixed read-only Git command to resolve it.
    def head(self, path: Path) -> str | None:
        return self.run(path, "rev-parse", "--verify", "HEAD", check=False).decode().strip() or None

    # Lists tracked files and their contents at a commit.
    # Reads Git's file records and rejects links, submodules and unsafe paths.
    def tree(self, path: Path, revision: str, *, cancel=None) -> dict[str, dict]:
        result = {}
        raw = self.run(path, "ls-tree", "-rz", "--full-tree", revision)
        for entry in raw.split(b"\0"):
            if cancel is not None and cancel.is_set():
                raise AppError("CANCELLED", "Source acquisition was cancelled.", 409)
            if not entry:
                continue
            metadata, encoded_path = entry.split(b"\t", 1)
            mode, kind, sha = metadata.decode().split()
            try:
                relative = encoded_path.decode("utf-8")
                artifact_path(relative)
            except (UnicodeError, ValueError):
                raise AppError(
                    "unsafe_source", "Tracked history contains unsupported or sensitive paths.", 409
                ) from None
            if mode not in {"100644", "100755"} or kind != "blob":
                raise AppError("unsafe_source", "Symlinks and submodules cannot be published.", 409)
            content = self.blob(path, sha)
            check_secret(relative, content, allow_binary=True)
            result[relative] = {"hash": digest(content), "mode": mode, "object": sha}
            if len(result) > 20000:
                raise AppError(
                    "source_too_large", "Tracked source exceeds the publication limit.", 409
                )
        return result

    # Reads the saved contents of a Git file object.
    # Checks its size before loading the bytes.
    def blob(self, path: Path, sha: str) -> bytes:
        size = int(self.run(path, "cat-file", "-s", sha))
        if size > getattr(self.settings, "max_file_bytes", 1048576):
            raise AppError("source_too_large", "Tracked file exceeds the publication limit.", 409)
        return self.run(path, "cat-file", "blob", sha)

    # Checks commits that would be published for secrets.
    # Scans the files in reachable commits within a fixed history limit.
    def scan_history(self, path: Path, sha: str) -> None:
        commits = self.run(path, "rev-list", "--max-count=1001", sha).decode().splitlines()
        if len(commits) > 1000:
            raise AppError(
                "history_too_large", "History exceeds the safe publication scan limit.", 409
            )
        for commit in commits:
            self.tree(path, commit)
            message = self.run(path, "cat-file", "commit", commit)
            check_secret("commit-message.txt", message)

    # Builds the Git file set for an approved commit.
    # Uses a temporary index containing only the reviewed files and permissions.
    def reviewed_tree(
        self, path: Path, parent: str | None, files: list[ReviewedFile], store: ArtifactStore
    ) -> str:
        with tempfile.TemporaryDirectory(prefix="voxops-index-") as directory:
            index = Path(directory) / "index"
            self.run(path, "read-tree", parent or "--empty", index=index)
            records = []
            for file in files:
                if file.staged_ref is None:
                    records.append(f"0 {'0' * 40}\t{file.path}".encode() + b"\0")
                    continue
                content = store.read_blob(file.staged_ref)
                check_secret(file.path, content, allow_binary=True)
                if digest(content) != file.after_hash:
                    raise AppError("stale_review", "Reviewed bytes no longer match.", 409)
                sha = self.run(path, "hash-object", "-w", "--stdin", data=content).decode().strip()
                records.append(f"{file.mode} {sha}\t{file.path}".encode() + b"\0")
            self.run(
                path, "update-index", "-z", "--index-info", data=b"".join(records), index=index
            )
            tree = self.run(path, "write-tree", index=index).decode().strip()
            self.tree(path, tree)
            return tree

    # Creates a commit for the approved file set.
    # Uses the run timestamp and parent so a retry can recreate the same commit.
    def commit(
        self, path: Path, tree: str, parent: str | None, message: str, timestamp: str
    ) -> str:
        args = ["commit-tree", tree]
        if parent:
            args += ["-p", parent]
        return (
            self.run(path, *args, data=(message + "\n").encode(), timestamp=timestamp)
            .decode()
            .strip()
        )

    # Finds the commit currently at a remote branch.
    # Queries that exact branch through the approved connection.
    def remote_sha(self, path: Path, url: str, branch: str) -> str | None:
        raw = self.run(path, "ls-remote", "--refs", self.transport(url), f"refs/heads/{branch}")
        lines = raw.decode().splitlines()
        if not lines:
            return None
        if len(lines) != 1 or lines[0].split("\t")[1] != f"refs/heads/{branch}":
            raise AppError("remote_mismatch", "Remote returned an unexpected branch.", 409)
        return lines[0].split("\t")[0]

    # Pushes an approved commit to its destination branch.
    # Checks the existing remote commit and never forces the update.
    def push(self, path: Path, url: str, branch: str, sha: str) -> None:
        actual = self.remote_sha(path, url, branch)
        if actual == sha:
            return
        if actual is not None:
            raise AppError("git_collision", "Remote branch exists with an unapproved commit.", 409)
        self.run(
            path,
            "push",
            "--porcelain",
            "--no-verify",
            self.transport(url),
            f"{sha}:refs/heads/{branch}",
        )
        if self.remote_sha(path, url, branch) != sha:
            raise AppError(
                "remote_mismatch", "Remote commit did not match the published commit.", 409
            )
