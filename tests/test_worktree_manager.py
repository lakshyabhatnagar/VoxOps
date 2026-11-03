from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import committed_repo, git_test, settings_for

from voxops.core.errors import AppError
from voxops.repository.git_inspector import inspect_git
from voxops.repository.worktree_manager import WorktreeManager
from voxops.storage.scan_store import ScanStore


# Why: Prove worktree isolation preserves every part of the original working tree.
# How: Compare source branch, HEAD, status and file bytes before creation and after dirty cleanup.
def test_worktree_lifecycle_preserves_source(tmp_path):
    source = committed_repo(tmp_path)
    (source / "tracked.txt").write_text("uncommitted source\n")
    (source / "untracked.txt").write_text("private local\n")
    before = (inspect_git(source), git_test(source, "status", "--porcelain"))
    settings = settings_for(tmp_path)
    manager = WorktreeManager(settings, ScanStore(settings.data_dir))
    record = manager.create(str(source))
    location = Path(record.location)
    assert not location.is_relative_to(source)
    assert (location / "tracked.txt").read_text() == "committed\n"
    assert not (location / "untracked.txt").exists()
    assert record.warnings and manager.inspect(record.id).status == "active"
    assert len(manager.list()) == 1
    (location / "new.txt").write_text("isolated change")
    with pytest.raises(AppError, match="explicitly") as failure:
        manager.remove(record.id)
    assert failure.value.category == "dirty_worktree"
    # A restarted manager must recognize existing ownership records.
    manager = WorktreeManager(settings, ScanStore(settings.data_dir))
    assert manager.remove(record.id, discard_changes=True).status == "removed"
    assert not location.exists()
    assert not git_test(source, "branch", "--list", record.branch)
    assert (inspect_git(source), git_test(source, "status", "--porcelain")) == before
    assert (source / "tracked.txt").read_text() == "uncommitted source\n"
    assert (source / "untracked.txt").read_text() == "private local\n"


# Why: Make Git prerequisite and ownership failures actionable without implicit initialization.
# How: Exercise uninitialized, unborn, nested, detached and unknown-worktree paths.
def test_worktree_prerequisites(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    settings = settings_for(tmp_path)
    manager = WorktreeManager(settings, ScanStore(settings.data_dir))
    with pytest.raises(AppError) as failure:
        manager.create(str(empty))
    assert failure.value.category == "uninitialized"
    assert not (empty / ".git").exists()
    git_test(empty, "init")
    with pytest.raises(AppError) as failure:
        manager.create(str(empty))
    assert failure.value.category == "missing_initial_commit"
    source = committed_repo(tmp_path)
    child = source / "child"
    child.mkdir()
    with pytest.raises(AppError) as failure:
        manager.create(str(child))
    assert failure.value.category == "nested_repository_path"
    git_test(source, "checkout", "--detach")
    with pytest.raises(AppError) as failure:
        manager.create(str(source))
    assert failure.value.category == "detached_head"
    with pytest.raises(AppError) as failure:
        manager.remove("f" * 32, discard_changes=True)
    assert failure.value.category == "worktree_not_found"


# Why: Prove local Git metadata cannot turn scan/checkout into project-defined code execution.
# How: Install sentinel hooks/content filters and verify manager operations never invoke them.
def test_git_hooks_and_filters_are_disabled(tmp_path):
    source = committed_repo(tmp_path)
    marker = tmp_path / "executed"
    (source / ".gitattributes").write_text("*.txt filter=poison\n")
    git_test(source, "add", ".gitattributes")
    git_test(
        source,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "attributes",
    )
    git_test(source, "config", "filter.poison.smudge", f"touch {marker}; cat")
    git_test(source, "config", "filter.poison.clean", f"touch {marker}; cat")
    hook = source / ".git/hooks/post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o700)
    settings = settings_for(tmp_path)
    manager = WorktreeManager(settings, ScanStore(settings.data_dir))
    record = manager.create(str(source))
    manager.remove(record.id)
    assert not marker.exists()


# Why: Avoid deleting committed user work merely because a temporary worktree has clean status.
# How: Commit inside a disposable worktree and require discard consent for cleanup.
def test_worktree_new_commits_need_discard(tmp_path):
    source = committed_repo(tmp_path)
    settings = settings_for(tmp_path)
    manager = WorktreeManager(settings, ScanStore(settings.data_dir))
    record = manager.create(str(source))
    location = Path(record.location)
    (location / "new.txt").write_text("new committed work")
    git_test(location, "add", "new.txt")
    git_test(
        location,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "isolated work",
    )
    with pytest.raises(AppError) as failure:
        manager.remove(record.id)
    assert failure.value.category == "dirty_worktree"
    manager.remove(record.id, discard_changes=True)
    assert not (source / "new.txt").exists()


# Why: Keep interrupted cleanup recoverable and refuse generated branch collisions.
# How: Retry a failed branch deletion from persisted state and simulate an occupied branch.
def test_worktree_cleanup_retry_and_collision(tmp_path, monkeypatch):
    import voxops.repository.worktree_manager as module

    source = committed_repo(tmp_path)
    settings = settings_for(tmp_path)
    manager = WorktreeManager(settings, ScanStore(settings.data_dir))
    record = manager.create(str(source))
    original = module.git

    # Why: Exercise the failure window after a worktree directory has already been removed.
    # How: Fail only generated branch deletion while forwarding all other Git operations.
    def fail_branch_delete(path, *args, **kwargs):
        if args[0] == "branch":
            raise AppError("git_failed", "Simulated local deletion failure.", 409)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(module, "git", fail_branch_delete)
    with pytest.raises(AppError) as failure:
        manager.remove(record.id)
    assert failure.value.category == "worktree_removal_failed"
    monkeypatch.setattr(module, "git", original)
    assert manager.inspect(record.id).status == "cleanup_failed"
    assert manager.remove(record.id).status == "removed"
    occupied = "a" * 32
    git_test(source, "branch", f"voxops/{occupied}")
    monkeypatch.setattr(module, "uuid4", lambda: SimpleNamespace(hex=occupied))
    with pytest.raises(AppError) as failure:
        manager.create(str(source))
    assert failure.value.category == "worktree_collision"
