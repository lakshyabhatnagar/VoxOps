import hashlib
import shutil
import subprocess
from pathlib import Path

import mongomock
import pytest

from voxops.core.settings import Settings
from voxops.storage import mongodb

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
    return Settings(
        allowed_roots=[tmp_path],
        mongo_database="test_" + hashlib.sha256(str(tmp_path).encode()).hexdigest()[:24],
        **overrides,
    )


# Choose whether the pinned-repository cases start from detection or a saved checkpoint.
# Other tests ignore these options and keep their existing behavior.
# Real Docker is opt-in because it builds upstream code and can download dependencies.
def pytest_addoption(parser):
    parser.addoption("--repo-stage", choices=["all", "1", "2", "3"], default="all")
    parser.addoption("--repo-real-docker", action="store_true", default=False)


@pytest.fixture(autouse=True)
# Inject one isolated Mongo-compatible client into each fast test.
# Production always uses PyMongo; no mock or fallback is selected by application code.
def mongo_client(monkeypatch):
    client = mongomock.MongoClient()
    monkeypatch.setattr(mongodb, "MongoClient", lambda *args, **kwargs: client)
    yield client
    client.close()
