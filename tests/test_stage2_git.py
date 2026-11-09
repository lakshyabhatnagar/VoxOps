import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from voxops.artifacts.review import approve_review, require_approval
from voxops.core.errors import AppError
from voxops.core.settings import Settings
from voxops.domain.artifact_models import (
    ArtifactPlan,
    ArtifactProposal,
    ArtifactRun,
    ArtifactTask,
    RepositoryTarget,
    TaskResult,
)
from voxops.github.connection import GitHubConnection
from voxops.github.publisher import GitPublisher
from voxops.repository.git_executor import GitExecutor
from voxops.repository.path_policy import fingerprint
from voxops.repository.snapshot import capture_source
from voxops.repository.worktree_manager import WorktreeManager
from voxops.storage.artifact_store import ArtifactStore, digest
from voxops.storage.scan_store import ScanStore


# Run a Git command in the given test repository.
# Return its raw output so tests can compare exact bytes.
def git(path: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True).stdout


# Use a local Git repository in place of the fake GitHub destination.
# This lets publication tests exercise Git without sending data online.
class LocalGit(GitExecutor):
    # Set up this test helper with the values supplied by the test.
    # Keep those values available for its later operations.
    def __init__(self, settings, remote: Path):
        super().__init__(settings)
        self.remote = remote
        self.fail_push = False

    # Check that the requested address is the expected fake GitHub address.
    # Return the local repository path in its place.
    def transport(self, url: str) -> str:
        assert url == "https://github.com/example/project.git"
        return str(self.remote)

    # Add permission for local file access to the normal Git settings.
    # This allows the test to push to its temporary local repository.
    def config_overrides(self, path: Path, environment: dict[str, str]) -> list[str]:
        return super().config_overrides(path, environment) + ["-c", "protocol.file.allow=always"]

    # Fail the next push when the test requests a simulated error.
    # Otherwise pass the push through to Git.
    def push(self, path: Path, url: str, branch: str, sha: str) -> None:
        if self.fail_push:
            self.fail_push = False
            raise AppError("git_failed", "Simulated offline transport failure.", 409)
        super().push(path, url, branch, sha)


# Stand in for GitHub when tests create and look up a repository.
# Record creation attempts and return the test account and repository details.
class FakeGitHub:
    # Set up this test helper with the values supplied by the test.
    # Keep those values available for its later operations.
    def __init__(self, exists: bool):
        self.exists = exists
        self.creations = 0
        self.creation_id = None
        self.fail_after_create = False
        self.target = RepositoryTarget(
            owner="example",
            name="project",
            repository_id="123",
            clone_url="https://github.com/example/project.git",
            private=True,
            can_push=True,
        )

    # Return the test account name without contacting GitHub.
    # This keeps the test focused on the repository operation.
    async def probe(self, require_create: bool = False) -> str:
        return "example"

    # Look up the fake repository and check its creation marker when supplied.
    # Return the repository only when it belongs to this creation attempt.
    async def lookup(
        self, target: RepositoryTarget, creation_id: str | None = None
    ) -> RepositoryTarget | None:
        if creation_id and self.exists and creation_id != self.creation_id:
            raise AppError("git_collision", "Wrong creation owner.", 409)
        return self.target.model_copy(deep=True) if self.exists else None

    # Record a fake repository creation and save its identifying marker.
    # Optionally simulate a lost response so the test can retry.
    async def create(self, target: RepositoryTarget, creation_id: str | None = None) -> None:
        assert not self.exists
        self.creations += 1
        self.exists, self.creation_id = True, creation_id
        if self.fail_after_create:
            self.fail_after_create = False
            raise AppError("github_mcp_failed", "Simulated lost creation response.", 409)


# Create a temporary source repository, storage, worktree, and local remote.
# Return the objects needed to test publication without contacting GitHub.
def scenario(tmp_path: Path, committed: bool = True, remote: bool = True):
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_bytes(b"print('hello')\r\n")
    bare = tmp_path / "remote.git"
    bare.mkdir()
    git(bare, "init", "--bare", "--initial-branch=main")
    if committed:
        (source / "package.json").write_text(json.dumps({
            "name": "publication-fixture", "private": True,
            "dependencies": {"react": "19.1.0", "vite": "5.4.21"},
            "scripts": {"build": "vite build"},
        }))
        (source / "index.html").write_text("<html><body>Publication fixture</body></html>")
        git(source, "init", "--initial-branch=main")
        git(source, "config", "user.name", "Fixture")
        git(source, "config", "user.email", "fixture@example.test")
        git(source, "add", "app.py", "package.json", "index.html")
        git(source, "commit", "-m", "baseline")
        git(source, "push", str(bare), "main")
    if remote:
        git(source, "remote", "add", "origin", "https://github.com/example/project.git")
    settings = Settings(allowed_roots=[tmp_path], data_dir=tmp_path / "data")
    store = ArtifactStore(settings.data_dir)
    manager = WorktreeManager(settings, ScanStore(settings.data_dir))
    manager.root = tmp_path / "worktrees"
    executor = LocalGit(settings, bare)
    connection = FakeGitHub(remote)
    publisher = GitPublisher(settings, store, manager, connection, executor)
    run_id = uuid4().hex
    source_hash, _ = capture_source(source)
    from voxops.frameworks.registry import detect_units
    from voxops.repository.filesystem_scanner import scan_repository

    units = detect_units(scan_repository(source, settings, uuid4().hex)) if committed else []
    task = ArtifactTask(
        task_id="ignore",
        artifact_type="dockerignore",
        target_path=".dockerignore",
        reason="Keep source secrets outside build context",
    )
    plan = ArtifactPlan(
        plan_id=uuid4().hex,
        scan_id=uuid4().hex,
        repo_fingerprint=fingerprint(source),
        source_input_fingerprint="scan-input",
        strategy_by_unit={unit.id: "static" for unit in units},
        units=units,
        tasks={"ignore": task},
    )
    run = ArtifactRun(
        run_id=run_id,
        repo_fingerprint=fingerprint(source),
        scan_id=plan.scan_id,
        source_input_fingerprint="scan-input",
        source_fingerprint=source_hash,
        source_head_sha=executor.head(source),
        plan=plan,
        target=RepositoryTarget(owner="example", name="project"),
        branch=f"voxops/{run_id}",
    )
    store.bind_source(run_id, source)
    store.save_run(run)
    return SimpleNamespace(
        source=source,
        bare=bare,
        publisher=publisher,
        store=store,
        manager=manager,
        executor=executor,
        connection=connection,
        run=run,
    )


# Find the latest review for the requested purpose.
# Submit the test decision for the exact review shown.
def approve(run: ArtifactRun, purpose: str, decision: str = "approve"):
    review = [review for review in run.reviews.values() if review.purpose == purpose][-1]
    return approve_review(run, review.review_id, review.digest, decision)


# Place generated file content in the test worktree and save its result.
# Return a run that is ready for the publication tests.
def artifacts(context):
    run = context.run
    location = Path(context.manager.load(run.worktree_id)[0].location)
    content = b".env\n.git\nnode_modules\n"
    (location / ".dockerignore").write_bytes(content)
    key = context.store.stage(content)
    proposal = ArtifactProposal(
        task_id="ignore",
        target_path=".dockerignore",
        artifact_type="dockerignore",
        original_hash=None,
        content_hash=digest(content),
        staged_ref=key,
        creator="llm",
    )
    run.tasks["ignore"] = TaskResult(task_id="ignore", status="passed", proposal=proposal)
    context.publisher.artifact_review(run)
    return location


def verify_artifacts(context):
    from simulated_docker import DemoDocker

    from voxops.artifacts.generator import ArtifactGenerator
    from voxops.artifacts.service import ArtifactService

    settings = context.publisher.settings
    scans = ScanStore(settings.data_dir)
    assert context.run.plan.units
    context.run.status = "generating"
    for key in list(context.run.reviews):
        if context.run.reviews[key].purpose == "artifact_publication":
            context.run.reviews.pop(key)
            context.run.approvals.pop(key, None)
    context.store.save_run(context.run)
    service = ArtifactService(settings, scans, context.manager,
                              ArtifactGenerator(settings, context.store), context.publisher)
    service.driver_factory = (
        lambda unit_id, journal, cancel, session: DemoDocker(settings, journal, cancel, session)
    )
    context.run = asyncio.run(service.execute(context.run.run_id))
    assert context.run.execution.status == "passed", context.run.errors



# Approve an initial source review and then change the initial branch.
# Check that publication rejects the earlier approval.
def test_unborn_source_branch_change_invalidates_approval(tmp_path):
    ctx = scenario(tmp_path, committed=False, remote=False)
    git(ctx.source, "init", "--initial-branch=main")
    asyncio.run(ctx.publisher.preflight(ctx.run, ["app.py"]))
    review = next(iter(ctx.run.reviews.values()))
    assert review.source_branch == "main"
    approve(ctx.run, "baseline_publication")
    git(ctx.source, "symbolic-ref", "HEAD", "refs/heads/other")
    with pytest.raises(AppError, match="Source branch changed"):
        asyncio.run(ctx.publisher.prepare(ctx.run))
    assert ctx.connection.creations == 0 and ctx.executor.head(ctx.source) is None


# Add changed files in hidden and dependency folders.
# Check that the source review lists the files left out.
def test_excluded_dirty_paths_are_disclosed(tmp_path):
    ctx = scenario(tmp_path)
    for folder in (".hidden", "node_modules"):
        (ctx.source / folder).mkdir()
        (ctx.source / folder / "local.txt").write_text("omitted source\n")
    ctx.run.source_fingerprint = capture_source(ctx.source)[0]
    asyncio.run(ctx.publisher.preflight(ctx.run, []))
    assert ctx.run.status == "awaiting_source_publication"
    review = next(iter(ctx.run.reviews.values()))
    assert review.purpose == "source_snapshot"
    assert {".hidden/", "node_modules/"} <= set(review.excluded_paths)


# Approve selected binary and executable files in a new repository.
# Check their contents and permissions while preserving unrelated staged changes.
def test_a11_no_head_exact_baseline_preserves_unrelated_index(tmp_path):
    ctx = scenario(tmp_path, committed=False, remote=False)
    git(ctx.source, "init", "--initial-branch=main")
    (ctx.source / "unrelated.txt").write_bytes(b"not selected\n")
    git(ctx.source, "add", "unrelated.txt")
    (ctx.source / "asset.bin").write_bytes(b"\xff\x00\r\n")
    (ctx.source / "app.py").chmod(0o755)
    ctx.run.source_fingerprint = capture_source(ctx.source)[0]
    index = (ctx.source / ".git/index").read_bytes()
    asyncio.run(ctx.publisher.preflight(ctx.run, ["app.py", "asset.bin"]))
    assert ctx.connection.creations == 0
    assert ctx.executor.head(ctx.source) is None
    with pytest.raises(AppError, match="approval"):
        asyncio.run(ctx.publisher.prepare(ctx.run))
    approve(ctx.run, "baseline_publication")
    asyncio.run(ctx.publisher.prepare(ctx.run))
    assert ctx.run.status == "generating"
    assert (ctx.source / ".git/index").read_bytes() == index
    assert git(ctx.bare, "show", "main:app.py") == b"print('hello')\r\n"
    assert git(ctx.bare, "show", "main:asset.bin") == b"\xff\x00\r\n"
    assert b"100755" in git(ctx.bare, "ls-tree", "main", "app.py")
    assert b"unrelated.txt" not in git(ctx.bare, "ls-tree", "-r", "main")
    assert ctx.connection.creations == 1


# Publish generated files through a real local Git repository.
# Check that the original source stays unchanged and the reported commit matches.
def test_a12_a16_existing_source_preserved_and_receipt_exact(tmp_path):
    ctx = scenario(tmp_path)
    head, source_bytes = ctx.executor.head(ctx.source), (ctx.source / "app.py").read_bytes()
    asyncio.run(ctx.publisher.preflight(ctx.run))
    asyncio.run(ctx.publisher.prepare(ctx.run))
    location = artifacts(ctx)
    verify_artifacts(ctx)
    approve(ctx.run, "artifact_publication")
    receipt = asyncio.run(ctx.publisher.publish(ctx.run))
    assert receipt.commit_sha == git(ctx.bare, "rev-parse", ctx.run.branch).decode().strip()
    assert receipt.remote_sha == receipt.commit_sha
    assert receipt.status == "ready_for_deployment" and receipt.deployment_executed is False
    assert ctx.executor.head(ctx.source) == head
    assert (ctx.source / "app.py").read_bytes() == source_bytes
    assert not (ctx.source / ".dockerignore").exists()
    assert (
        git(ctx.bare, "show", f"{ctx.run.branch}:.dockerignore")
        == (location / ".dockerignore").read_bytes()
    )
    assert ctx.connection.creations == 0
    assert asyncio.run(ctx.publisher.publish(ctx.run)).commit_sha == receipt.commit_sha


# Select one locally changed file for publication and leave others out.
# Check that only the selected content reaches the separate branch.
def test_a13_dirty_snapshot_selected_and_omitted(tmp_path):
    ctx = scenario(tmp_path)
    head = ctx.executor.head(ctx.source)
    (ctx.source / "app.py").write_bytes(b"print('dirty')\n")
    (ctx.source / "notes.txt").write_bytes(b"local notes\n")
    ctx.run.source_fingerprint = capture_source(ctx.source)[0]
    asyncio.run(ctx.publisher.preflight(ctx.run, ["app.py"]))
    review = list(ctx.run.reviews.values())[-1]
    assert review.excluded_paths == [".git/", "notes.txt"]
    with pytest.raises(AppError):
        asyncio.run(ctx.publisher.prepare(ctx.run))
    approve(ctx.run, "source_snapshot")
    asyncio.run(ctx.publisher.prepare(ctx.run))
    artifacts(ctx)
    verify_artifacts(ctx)
    approve(ctx.run, "artifact_publication")
    asyncio.run(ctx.publisher.publish(ctx.run))
    assert git(ctx.bare, "show", f"{ctx.run.branch}:app.py") == b"print('dirty')\n"
    assert b"notes.txt" not in git(ctx.bare, "ls-tree", "-r", ctx.run.branch)
    assert ctx.executor.head(ctx.source) == head
    assert (ctx.source / "notes.txt").read_bytes() == b"local notes\n"


@pytest.mark.parametrize(
    "change", ["reject", "source", "worktree", "mode", "branch", "policy", "plan"]
)
# Reject a review or change reviewed information after approval.
# Check that neither a commit nor publication occurs.
def test_a14_rejected_or_stale_approval_never_publishes(tmp_path, change):
    ctx = scenario(tmp_path)
    asyncio.run(ctx.publisher.preflight(ctx.run))
    asyncio.run(ctx.publisher.prepare(ctx.run))
    location = artifacts(ctx)
    parent = ctx.executor.head(location)
    approve(ctx.run, "artifact_publication", "reject" if change == "reject" else "approve")
    if change == "source":
        (ctx.source / "app.py").write_text("changed\n")
    elif change == "worktree":
        (location / ".dockerignore").write_text("changed\n")
    elif change == "mode":
        (location / ".dockerignore").chmod(0o755)
    elif change == "branch":
        ctx.run.branch = "voxops/unapproved"
    elif change == "policy":
        ctx.publisher.settings.artifact_optional_tools = True
    elif change == "plan":
        ctx.run.plan.tasks["ignore"].reason = "Changed plan without incrementing its revision"
    with pytest.raises(AppError):
        asyncio.run(ctx.publisher.publish(ctx.run))
    assert ctx.executor.head(location) == parent
    assert git(ctx.bare, "for-each-ref", "--format=%(refname)", "refs/heads/voxops/") == b""


# Simulate losing the response after creating a remote repository.
# Check that retrying finds the same repository instead of creating another.
def test_a15_retry_after_remote_creation_does_not_create_twice(tmp_path):
    ctx = scenario(tmp_path, committed=False, remote=False)
    asyncio.run(ctx.publisher.preflight(ctx.run, ["app.py"]))
    approve(ctx.run, "baseline_publication")
    ctx.connection.fail_after_create = True
    with pytest.raises(AppError):
        asyncio.run(ctx.publisher.prepare(ctx.run))
    assert ctx.connection.creations == 1
    ctx.run = ctx.store.load_run(ctx.run.run_id)
    asyncio.run(ctx.publisher.prepare(ctx.run))
    assert ctx.connection.creations == 1
    assert git(ctx.bare, "rev-parse", "main").decode().strip() == ctx.run.progress.baseline_sha


# Interrupt publication after a local commit and retry it.
# Check that repeated attempts use the same commit.
def test_a15_retry_after_commit_and_push_is_idempotent(tmp_path):
    ctx = scenario(tmp_path)
    asyncio.run(ctx.publisher.preflight(ctx.run))
    asyncio.run(ctx.publisher.prepare(ctx.run))
    location = artifacts(ctx)
    verify_artifacts(ctx)
    approve(ctx.run, "artifact_publication")
    ctx.executor.fail_push = True
    with pytest.raises(AppError):
        asyncio.run(ctx.publisher.publish(ctx.run))
    committed = ctx.executor.head(location)
    ctx.run = ctx.store.load_run(ctx.run.run_id)
    first = asyncio.run(ctx.publisher.publish(ctx.run))
    second = asyncio.run(ctx.publisher.publish(ctx.run))
    assert first.commit_sha == second.commit_sha == committed
    assert git(location, "rev-list", "--count", f"{ctx.run.progress.baseline_sha}..HEAD") == b"1\n"


# Create a remote branch with a different existing commit.
# Check that publication refuses to overwrite it.
def test_remote_branch_collision_is_not_overwritten(tmp_path):
    ctx = scenario(tmp_path)
    asyncio.run(ctx.publisher.preflight(ctx.run))
    asyncio.run(ctx.publisher.prepare(ctx.run))
    artifacts(ctx)
    verify_artifacts(ctx)
    approve(ctx.run, "artifact_publication")
    baseline = ctx.run.progress.baseline_sha
    git(ctx.bare, "update-ref", f"refs/heads/{ctx.run.branch}", baseline)
    with pytest.raises(AppError) as error:
        asyncio.run(ctx.publisher.publish(ctx.run))
    assert error.value.category == "git_collision"
    assert git(ctx.bare, "rev-parse", ctx.run.branch).decode().strip() == baseline


@pytest.mark.parametrize("where", ["history", "index"])
# Put a credential in Git history or staged changes.
# Check that publication is blocked.
def test_secret_in_history_or_staged_index_blocks_publication(tmp_path, where):
    ctx = scenario(tmp_path)
    secret = b"API_TOKEN=ghp_" + b"A" * 36 + b"\n"
    if where == "history":
        (ctx.source / "config.txt").write_bytes(secret)
        git(ctx.source, "add", "config.txt")
        git(ctx.source, "commit", "-m", "unsafe history")
        git(ctx.source, "rm", "config.txt")
        git(ctx.source, "commit", "-m", "remove")
        ctx.run.source_head_sha = ctx.executor.head(ctx.source)
        with pytest.raises(AppError):
            asyncio.run(ctx.publisher.preflight(ctx.run))
    else:
        asyncio.run(ctx.publisher.preflight(ctx.run))
        asyncio.run(ctx.publisher.prepare(ctx.run))
        location = artifacts(ctx)
        approve(ctx.run, "artifact_publication")
        (location / ".env").write_bytes(secret)
        git(location, "add", ".env")
        with pytest.raises(AppError):
            asyncio.run(ctx.publisher.publish(ctx.run))
    assert git(ctx.bare, "for-each-ref", "--format=%(refname)", "refs/heads/voxops/") == b""


# Install Git hooks, filters, and a credential helper in a test repository.
# Check that unsafe configuration is rejected and no commands run.
def test_untrusted_git_execution_disabled(tmp_path):
    ctx = scenario(tmp_path)
    marker = tmp_path / "executed"
    hook = ctx.source / ".git/hooks/post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    hook.chmod(0o755)
    git(ctx.source, "config", "filter.evil.clean", f"touch '{marker}'")
    git(ctx.source, "config", "filter.evil.smudge", f"touch '{marker}'")
    asyncio.run(ctx.publisher.preflight(ctx.run))
    asyncio.run(ctx.publisher.prepare(ctx.run))
    artifacts(ctx)
    verify_artifacts(ctx)
    approve(ctx.run, "artifact_publication")
    asyncio.run(ctx.publisher.publish(ctx.run))
    assert not marker.exists()
    git(ctx.source, "config", "credential.helper", f"!touch '{marker}'")
    with pytest.raises(AppError) as error:
        ctx.executor.head(ctx.source)
    assert error.value.category == "unsafe_git_config"
    assert not marker.exists()


# Try to use the GitHub connection without configuring it.
# Check that it reports the connection as unavailable.
def test_mcp_unconfigured_is_unavailable():
    connection = GitHubConnection(SimpleNamespace())
    with pytest.raises(AppError) as error:
        asyncio.run(connection.probe())
    assert error.value.category == "github_mcp_unavailable"


@pytest.mark.parametrize("curated", [False, True])
# Connect to a fake GitHub service with different tool and metadata formats.
# Check that repository details and collaborator permissions are verified.
def test_mcp_discovers_tools_and_verifies_repository_metadata(curated):
    calls = []
    schemas = {
        "get_me": {},
        "search_repositories": {"query": {}, "minimal_output": {}},
        "create_repository": {"name": {}, "private": {}, "autoInit": {}, "description": {}},
        "list_repository_collaborators": {"owner": {}, "repo": {}, "page": {}, "perPage": {}},
    }

    # Read requests sent to the fake GitHub service and record them.
    # Return the requested tool descriptions and repository details without network access.
    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        method = body["method"]
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            result = {"protocolVersion": "2025-03-26", "serverInfo": {"name": "github"}}
        elif method == "tools/list":
            result = {
                "tools": [
                    {"name": name, "inputSchema": {"properties": schema}}
                    for name, schema in schemas.items()
                ]
            }
        else:
            name = body["params"]["name"]
            if name == "get_me":
                value = {"login": "example"}
            elif name == "create_repository":
                assert body["params"]["arguments"]["private"] is True
                value = {"id": "123", "url": "https://github.com/example/project"}
            elif name == "list_repository_collaborators":
                value = {"items": [{"login": "example", "role_name": "admin"}]}
            else:
                value = {
                    "items": [
                        {
                            "id": 123,
                            "full_name": "example/project",
                            "owner": {"login": "example"},
                            "private": True,
                            "permissions": {"push": True},
                        }
                    ]
                }
                if curated:
                    value["items"][0].pop("owner")
                    value["items"][0].pop("permissions")
            result = {"content": [{"type": "text", "text": json.dumps(value)}]}
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"], "result": result},
            headers={"Mcp-Session-Id": "session-1"},
        )

    # Use a test HTTP client to connect to the fake GitHub service.
    # Check repository discovery, creation, and metadata in one session.
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = GitHubConnection(
                SimpleNamespace(github_mcp_url="https://mcp.example.test/"), client
            )
            await adapter.probe(require_create=True)
            target = RepositoryTarget(owner="example", name="project")
            await adapter.create(target, "test-run")
            verified = await adapter.lookup(target)
            assert verified.private is True and verified.can_push is True
            assert verified.repository_id == "123"

    asyncio.run(exercise())
    assert not any(call.get("params", {}).get("name") == "get_repository" for call in calls)
    assert any(
        call.get("params", {}).get("arguments", {}).get("minimal_output") is False for call in calls
    )


@pytest.mark.parametrize("role", ["read", "triage", "custom-owner", ["admin"]])
# Return repository details with collaborator roles that cannot write.
# Check that publication permission is denied.
def test_mcp_curated_metadata_rejects_non_write_roles(role):
    adapter = GitHubConnection(SimpleNamespace())
    adapter.identity = "example"
    adapter.tools = {
        "search_repositories": {"inputSchema": {"properties": {"query": {}}}},
        "list_repository_collaborators": {
            "inputSchema": {
                "properties": {
                    "owner": {},
                    "repo": {},
                }
            }
        },
    }

    # Return the test account name without contacting GitHub.
    # This keeps the test focused on the repository operation.
    async def probe(require_create=False):
        return "example"

    # Return the repository or collaborator details requested by the test.
    # Use the supplied role or permissions to exercise access checks.
    async def call(name, arguments):
        if name == "list_repository_collaborators":
            return {"items": [{"login": "example", "role_name": role}]}
        return {"items": [{"id": 123, "full_name": "example/project", "private": True}]}

    adapter.probe, adapter.call = probe, call
    with pytest.raises(AppError) as error:
        asyncio.run(adapter.lookup(RepositoryTarget(owner="example", name="project")))
    assert error.value.category == "github_mcp_metadata"


# Change the list of reviewed files after approval.
# Check that publication rejects the altered review.
def test_review_manifest_tampering_invalidates_approval(tmp_path):
    ctx = scenario(tmp_path)
    asyncio.run(ctx.publisher.preflight(ctx.run))
    asyncio.run(ctx.publisher.prepare(ctx.run))
    artifacts(ctx)
    approve(ctx.run, "artifact_publication")
    review = list(ctx.run.reviews.values())[-1]
    review.files[0].mode = "100755"
    with pytest.raises(AppError) as error:
        require_approval(ctx.run, "artifact_publication")
    assert error.value.category == "stale_review"


# Interrupt initial publication after creating a commit but before updating the branch.
# Check that a retry recovers the same intended commit.
def test_retry_after_baseline_commit_before_head_update(tmp_path, monkeypatch):
    ctx = scenario(tmp_path, committed=False, remote=False)
    asyncio.run(ctx.publisher.preflight(ctx.run, ["app.py"]))
    approve(ctx.run, "baseline_publication")
    original = ctx.executor.run
    interrupted = False

    # Interrupt the first Git reference update to simulate a crash.
    # Allow all other Git operations to run normally.
    def fail_before_update(path, *args, **kwargs):
        nonlocal interrupted
        if args[0] == "update-ref" and not interrupted:
            interrupted = True
            raise AppError("git_failed", "Simulated crash before source HEAD update.", 409)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(ctx.executor, "run", fail_before_update)
    with pytest.raises(AppError):
        asyncio.run(ctx.publisher.prepare(ctx.run))
    ctx.run = ctx.store.load_run(ctx.run.run_id)
    expected = ctx.run.progress.baseline_sha
    assert expected is not None and ctx.executor.head(ctx.source) is None
    asyncio.run(ctx.publisher.prepare(ctx.run))
    assert ctx.executor.head(ctx.source) == expected
    assert ctx.connection.creations == 1


# Replace the repository’s Git configuration with a symbolic link.
# Check that publication stops before contacting the remote.
def test_git_metadata_symlink_is_rejected(tmp_path):
    ctx = scenario(tmp_path)
    config = ctx.source / ".git/config"
    outside = tmp_path / "config"
    config.rename(outside)
    config.symlink_to(outside)
    with pytest.raises(AppError) as error:
        asyncio.run(ctx.publisher.preflight(ctx.run))
    assert error.value.category == "unsafe_git_metadata"
    assert ctx.connection.creations == 0


# Write files where a directory is a symbolic link or a file has a hard link.
# Check that the writer rejects the link and avoids changing the linked source.
def test_owned_writer_rejects_symlinks_and_replaces_hardlinks(tmp_path):
    ctx = scenario(tmp_path)
    destination = tmp_path / "owned"
    destination.mkdir()
    (destination / "escape").symlink_to(ctx.source, target_is_directory=True)
    with pytest.raises(AppError):
        ctx.publisher.write_owned(destination, "escape/app.py", b"changed", "100644")
    original = (ctx.source / "app.py").read_bytes()
    import os

    os.link(ctx.source / "app.py", destination / "app.py")
    ctx.publisher.write_owned(destination, "app.py", b"isolated", "100644")
    assert (ctx.source / "app.py").read_bytes() == original
    assert (destination / "app.py").read_bytes() == b"isolated"


# Return repository details that omit permission to push.
# Check that publication is denied even when the account owns the repository.
def test_mcp_missing_push_permission_fails_closed():
    adapter = GitHubConnection(SimpleNamespace())
    adapter.tools = {"get_me": {}, "search_repositories": {"inputSchema": {"properties": {}}}}

    # Return the test account name without contacting GitHub.
    # This keeps the test focused on the repository operation.
    async def probe(require_create=False):
        return "example"

    # Return repository details without a push permission.
    # Let the test check that missing permission blocks publication.
    async def call(name, arguments):
        return {
            "items": [
                {
                    "id": 123,
                    "full_name": "example/project",
                    "private": True,
                    "owner": {"login": "example"},
                }
            ]
        }

    adapter.probe, adapter.call = probe, call
    with pytest.raises(AppError) as error:
        asyncio.run(adapter.lookup(RepositoryTarget(owner="example", name="project")))
    assert error.value.category == "github_mcp_capability"


# Compare file contents that do not end with a newline.
# Check that the review shows where the final newline is missing.
def test_review_diff_preserves_no_final_newline_evidence(tmp_path):
    from voxops.domain.artifact_models import ReviewedFile
    from voxops.repository.snapshot import file_diff

    store = ArtifactStore(tmp_path / "records")
    after = b"after"
    key = store.stage(after)
    file = ReviewedFile(
        path="config.txt",
        category="artifact",
        before_hash=digest(b"before"),
        after_hash=digest(after),
        staged_ref=key,
    )
    output = file_diff([file], store, {"config.txt": b"before"})
    assert "-before\n" in output and "+after\n" in output
    assert output.count("\\ No newline at end of file") == 2
