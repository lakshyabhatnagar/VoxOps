import os
import shutil
import subprocess
from pathlib import Path

from voxops.core.errors import AppError
from voxops.domain.models import GitMetadata

GIT_COMMANDS = {"rev-parse", "symbolic-ref", "status", "remote", "show-ref", "worktree", "branch"}


# Why: Keep Git invocation narrow, bounded and free from shell evaluation or project hooks.
# How: Allowlist commands and disable fsmonitor, hooks, prompting and optional locks.
def git(path: Path, *args: str, check: bool = True) -> str:
    if not args or args[0] not in GIT_COMMANDS:
        raise AppError("git_command_forbidden", "Git operation is not permitted.")
    if not shutil.which("git"):
        raise AppError("git_unavailable", "Install Git to use worktree operations.", 503)
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_ATTR_NOSYSTEM="1")
    try:
        filters = subprocess.run(
            [
                "git",
                "-C",
                str(path),
                "config",
                "--null",
                "--name-only",
                "--get-regexp",
                r"^filter\..*\.(smudge|clean|process|required)$",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            env=environment,
            check=False,
        )
        if filters.returncode not in (0, 1):
            raise AppError("git_config_invalid", "Git configuration could not be inspected.", 409)
        overrides = []
        for key in filters.stdout.split("\x00"):
            if key:
                overrides.extend(["-c", f"{key}={'false' if key.endswith('.required') else ''}"])
        result = subprocess.run(
            [
                "git",
                "--no-optional-locks",
                "-c",
                "core.fsmonitor=false",
                "-c",
                f"core.hooksPath={os.devnull}",
                "-c",
                "submodule.recurse=false",
                *overrides,
                "-C",
                str(path),
                *args,
            ],
            capture_output=True,
            text=True,
            timeout=15,
            env=environment,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise AppError("git_timeout", "Local Git operation timed out.", 504) from None
    except OSError:
        raise AppError("git_unavailable", "Local Git operation could not start.", 503) from None
    if result.returncode and check:
        raise AppError("git_failed", "Git operation failed; inspect local repository state.", 409)
    return result.stdout.strip() if not result.returncode else ""


# Why: Distinguish scan-safe repositories from worktree-ready committed roots.
# How: Inspect Git metadata through read-only commands without initializing or committing.
def inspect_git(path: Path) -> GitMetadata:
    if not shutil.which("git"):
        return GitMetadata(status="git_unavailable")
    top = git(path, "rev-parse", "--show-toplevel", check=False)
    if not top:
        return GitMetadata(status="uninitialized")
    head = git(path, "rev-parse", "--verify", "HEAD", check=False) or None
    branch = git(path, "symbolic-ref", "--short", "HEAD", check=False) or None
    return GitMetadata(
        status="committed" if head else "missing_initial_commit",
        head=head,
        branch=branch,
        detached=bool(head and not branch),
        nested_path=Path(top).resolve() != path.resolve(),
        dirty=bool(git(path, "status", "--porcelain", "--untracked-files=normal")),
        has_remotes=bool(git(path, "remote")),
    )
