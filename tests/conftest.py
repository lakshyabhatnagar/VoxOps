import shutil
import subprocess
from pathlib import Path

import pytest

from voxops.core.settings import Settings

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
# Why: Ensure tests never inherit a developer's live model credentials.
# How: Remove optional provider environment variables before each test.
def no_credentials(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


# Why: Provide isolated fixture copies that tests may mutate safely.
# How: Copy checked-in tiny repositories into pytest's temporary workspace.
def copy_fixture(tmp_path: Path, name: str) -> Path:
    return Path(shutil.copytree(FIXTURES / name, tmp_path / name))


# Why: Keep repository setup commands separate from production's narrow Git allowlist.
# How: Run argv-based Git exclusively against disposable test repositories.
def git_test(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, text=True, capture_output=True, timeout=15
    ).stdout.strip()


# Why: Exercise worktree behavior using actual committed content without touching user history.
# How: Initialize and commit a one-file repository in a temporary test directory.
def committed_repo(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    root.mkdir()
    (root / "tracked.txt").write_text("committed\n")
    git_test(root, "init", "-b", "main")
    git_test(root, "add", "tracked.txt")
    git_test(
        root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "-m",
        "fixture",
    )
    return root


# Why: Reuse safe per-test local policy without adding application-only test abstractions.
# How: Configure workspace and result directories under the test's owned temporary root.
def settings_for(tmp_path: Path, **overrides) -> Settings:
    return Settings(allowed_roots=[tmp_path], data_dir=tmp_path / "records", **overrides)
