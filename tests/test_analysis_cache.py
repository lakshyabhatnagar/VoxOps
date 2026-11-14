import asyncio
import os
import shutil

import pytest
from conftest import committed_repo, git_test, settings_for
from test_stage2_git import LocalGit

from voxops.analysis.cache import AnalysisCache, relevant_inputs
from voxops.core.errors import AppError
from voxops.repository.acquisition import RepositoryAcquirer, github_source
from voxops.repository.filesystem_scanner import scan_repository
from voxops.repository.path_policy import fingerprint
from voxops.storage.mongodb import database_for
from voxops.storage.scan_store import ScanStore
from voxops.workflows import stage1_graph
from voxops.workflows.stage1_graph import Stage1Workflow


# Commit test inputs using a fixed disposable identity and no hooks.
# Cache assertions compare actual Git revisions rather than fabricated SHA strings.
def commit(root, message):
    git_test(root, "add", ".")
    git_test(
        root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "-m",
        message,
    )
    return git_test(root, "rev-parse", "HEAD")


# Count real framework detection across exact hits, reusable commits and changed inputs.
# Every cache return must refresh the scan ID, Git revision and temporary source identity.
def test_two_level_cache_refresh_and_invalidation(tmp_path, monkeypatch):
    root = committed_repo(tmp_path)
    (root / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    (root / "requirements.txt").write_text("fastapi\nuvicorn\n")
    first_sha = commit(root, "app")
    settings = settings_for(tmp_path)
    store = ScanStore(database_for(settings))
    workflow = Stage1Workflow(settings, store)
    cache = AnalysisCache(workflow)
    calls = []
    original = stage1_graph.detect_units

    # Record each expensive detection pass while retaining the real adapters.
    # A cache hit must not reach this function.
    def counted(scan):
        calls.append(scan.id)
        return original(scan)

    monkeypatch.setattr(stage1_graph, "detect_units", counted)

    # Exercise one cache over a realistic sequence of immutable commits and requests.
    # Keep all calls on the same event loop, as the hosted workflow will.
    async def scenario():
        first = await cache.analyze(root, "example/project")
        same = await cache.analyze(root, "example/project")
        assert len(calls) == 1
        assert first.scan.id != same.scan.id and same.scan.git.head == first_sha
        assert first.scan.input_fingerprint == relevant_inputs(root, settings)[0]
        (root / "docs").mkdir()
        (root / "docs/notes.md").write_text("Documentation without startup evidence.")
        docs_sha = commit(root, "docs")
        reused = await cache.analyze(root, "example/project")
        assert len(calls) == 1 and reused.scan.git.head == docs_sha
        assert len({first.scan.id, same.scan.id, reused.scan.id}) == 3
        assert "docs/notes.md" in reused.scan.shallow_tree
        (root / "README.md").write_text("```sh\nuvicorn main:app --port 9999\n```\n")
        commit(root, "startup docs")
        changed = await cache.analyze(root, "example/project")
        assert len(calls) == 2
        assert "9999" in changed.detected_units[0].runtime.start_command
        (root / "requirements.txt").write_text("fastapi\nuvicorn\nhttpx\n")
        commit(root, "dependencies")
        await cache.analyze(root, "example/project")
        assert len(calls) == 3
        await cache.analyze(root, "example/project", use_llm=True)
        assert len(calls) == 4
        workflow.settings = settings.model_copy(update={"max_depth": 7})
        await cache.analyze(root, "example/project", use_llm=True)
        assert len(calls) == 5

    asyncio.run(scenario())


# Reuse an exact commit after the first temporary checkout has been deleted.
# Cached analysis must identify the new directory rather than preserve an obsolete path binding.
def test_cache_hit_after_checkout_replacement(tmp_path, monkeypatch):
    root = committed_repo(tmp_path)
    (root / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    (root / "requirements.txt").write_text("fastapi\nuvicorn\n")
    commit(root, "app")
    settings = settings_for(tmp_path)
    cache = AnalysisCache(Stage1Workflow(settings, ScanStore(database_for(settings))))

    # A second detection call means the saved analysis was not actually reused.
    # Raise immediately instead of merely comparing similar framework answers.
    def unexpected_detection(scan):
        raise AssertionError("Exact-commit cache did not reuse saved analysis")

    # Keep both requests in one loop and replace only their temporary source directory.
    # The persisted cache and canonical scan collection remain unchanged.
    async def scenario():
        original = await cache.analyze(root, "example/project")
        replacement = tmp_path / "replacement"
        git_test(tmp_path, "clone", str(root), str(replacement))
        shutil.rmtree(root)
        monkeypatch.setattr(stage1_graph, "detect_units", unexpected_detection)
        reused = await cache.analyze(replacement, "example/project")
        assert reused.scan.id != original.scan.id
        assert reused.scan.git.head == original.scan.git.head
        assert reused.scan.repo_fingerprint == fingerprint(replacement)
        assert reused.scan.repo_fingerprint != original.scan.repo_fingerprint
        assert reused.scan.display_name == "replacement"

    asyncio.run(scenario())


# File timestamps and local secret values must not change reusable analysis identity.
# Relevant file deletion and names still participate in the key.
def test_content_fingerprint_excludes_mtime_head_and_secret_values(tmp_path):
    (tmp_path / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    (tmp_path / ".env").write_text("TOKEN=first-private-value\n")
    settings = settings_for(tmp_path)
    first = scan_repository(tmp_path, settings).input_fingerprint
    os.utime(tmp_path / "main.py", (1, 1))
    (tmp_path / ".env").write_text("TOKEN=other-private-value\n")
    assert scan_repository(tmp_path, settings).input_fingerprint == first
    (tmp_path / "main.py").unlink()
    assert scan_repository(tmp_path, settings).input_fingerprint != first


@pytest.mark.parametrize(
    "url",
    [
        "/tmp/repo",
        "file:///tmp/repo",
        "https://evil.test/a/b",
        "https://token@github.com/a/b",
        "https://github.com/a/b?token=private",
        "git@github.com:a/b",
    ],
)
# Reject every unsupported transport before creating a temporary repository.
# Hosted requests cannot direct Git at local paths or credential-bearing URLs.
def test_github_source_rejects_unsafe_inputs(url):
    with pytest.raises(AppError, match="GitHub HTTPS"):
        github_source(url)


# Fetch through real Git from a disposable bare remote and preserve an exact branch commit.
# Only the transport is mapped locally; production URL validation and checkout code are real.
def test_acquire_exact_commit_and_owned_cleanup(tmp_path):
    root = committed_repo(tmp_path)
    sha = git_test(root, "rev-parse", "HEAD")
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git_test(remote, "init", "--bare")
    git_test(root, "push", str(remote), "main")
    settings = settings_for(tmp_path)
    acquirer = RepositoryAcquirer(settings, database_for(settings), LocalGit(settings, remote))
    source = acquirer.acquire("https://github.com/example/project", "main")
    try:
        assert source.commit_sha == sha
        assert acquirer.load(source.id) == source
        assert git_test(acquirer.root / source.id, "status", "--porcelain") == ""
        assert (acquirer.root / source.id / "tracked.txt").read_text() == "committed\n"
    finally:
        acquirer.remove(source.id)
    assert not (acquirer.root / source.id).exists()
    with pytest.raises(AppError, match="no longer available"):
        acquirer.load(source.id)
