import shutil
import subprocess
from pathlib import Path

import pytest

from voxops.core.settings import Settings

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
# Remove model service keys from the environment before each test.
# This keeps tests from using a developer’s real account.
# Tests that check a provider must explicitly supply their own fake key.
def no_credentials(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)


# Copy a sample repository into the test’s temporary folder.
# Return the copy so the test can change it safely.
def copy_fixture(tmp_path: Path, name: str) -> Path:
    return Path(shutil.copytree(FIXTURES / name, tmp_path / name))


# Run a Git command in a temporary test repository.
# Return its output and fail the test if the command fails.
def git_test(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, text=True, capture_output=True, timeout=15
    ).stdout.strip()


# Create a temporary repository and commit a sample file.
# Return it for tests that need real Git history.
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


# Create settings using folders inside the test’s temporary directory.
# Apply any extra settings supplied by the test.
def settings_for(tmp_path: Path, **overrides) -> Settings:
    return Settings(allowed_roots=[tmp_path], data_dir=tmp_path / "records", **overrides)
