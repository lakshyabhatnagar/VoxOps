import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from voxops.artifacts.review import build_review, policy_fingerprint, require_approval
from voxops.artifacts.security import check_secret, safe_read
from voxops.core.errors import AppError
from voxops.domain.artifact_models import ArtifactRun, ChangeReview, GitPublishResult, ReviewedFile
from voxops.domain.models import WorktreeRecord
from voxops.execution.candidate import require_publishable
from voxops.github.connection import GitHubConnection
from voxops.repository.git_executor import GitExecutor
from voxops.repository.path_policy import fingerprint, workspace_path
from voxops.repository.snapshot import capture_source, file_diff, snapshot_files, source_exclusions
from voxops.repository.worktree_manager import WorktreeManager
from voxops.storage.artifact_store import ArtifactStore, digest


# Manages the steps that publish approved project changes.
# Keeps records of source reviews, working copies and remote results.
class GitPublisher:
    # Sets up the services needed to publish a project.
    # Receives configuration, storage, worktree management and optional test services.
    def __init__(
        self,
        settings,
        store: ArtifactStore,
        worktrees: WorktreeManager,
        connection: GitHubConnection | None = None,
        executor: GitExecutor | None = None,
    ):
        self.settings, self.store, self.worktrees = settings, store, worktrees
        self.connection = connection or GitHubConnection(settings)
        self.executor = executor or GitExecutor(settings)

    # Checks the source directory associated with a saved run.
    # Verifies its allowed location, project identity and current commit.
    def source(self, run: ArtifactRun) -> Path:
        policy = policy_fingerprint(self.settings)
        if not run.reviews and not run.worktree_id and not run.progress.creation_intent:
            run.policy_fingerprint = policy
        if run.policy_fingerprint != policy:
            raise AppError(
                "stale_review", "Executor policy changed; create a new plan and review.", 409
            )
        source = workspace_path(str(self.store.source(run.run_id)), self.settings.allowed_roots)
        if fingerprint(source) != run.repo_fingerprint or (source / ".git").is_symlink():
            raise AppError("source_changed", "The registered source identity changed.", 409)
        metadata = source / ".git"
        if metadata.exists():
            if not metadata.is_dir():
                raise AppError(
                    "unsafe_git_metadata", "Use a standalone source repository root.", 409
                )
            for folder, dirs, files in os.walk(metadata, followlinks=False):
                if any((Path(folder) / name).is_symlink() for name in dirs + files):
                    raise AppError(
                        "unsafe_git_metadata", "Git metadata cannot contain symlinks.", 409
                    )
            if any(
                (metadata / name).exists()
                for name in ("objects/info/alternates", "info/grafts", "shallow")
            ):
                raise AppError(
                    "unsafe_git_metadata", "Shared or rewritten history is unsupported.", 409
                )
        current = self.executor.head(source)
        source_branch = (
            self.executor.run(source, "symbolic-ref", "--short", "HEAD", check=False)
            .decode()
            .strip()
            or None
        )
        if (
            run.source_branch is not None
            and source_branch != run.source_branch
            and ((source / ".git").exists() or source_branch is not None)
        ):
            raise AppError("stale_review", "Source branch changed after review.", 409)
        expected = run.source_head_sha or run.progress.baseline_sha
        head_changed = current != expected and not (
            run.source_head_sha is None and current is None and not run.progress.baseline_pushed
        )
        if (
            head_changed
            or capture_source(source, self.settings.max_file_bytes)[0] != run.source_fingerprint
        ):
            raise AppError(
                "stale_review", "Source bytes, modes or revision changed; start a new review.", 409
            )
        top = (
            self.executor.run(source, "rev-parse", "--show-toplevel", check=False).decode().strip()
        )
        if top and Path(top).resolve() != source:
            raise AppError(
                "nested_repository_path", "Use the Git repository root for publication.", 409
            )
        return source

    # Checks whether configured Git remotes match the approved repository.
    # Rejects a remote that points to a different destination.
    def has_remote(self, source: Path, run: ArtifactRun) -> bool:
        raw = self.executor.run(
            source, "config", "--local", "--get-regexp", r"^remote\..*\.url$", check=False
        ).decode()
        urls = [line.split(" ", 1)[1] for line in raw.splitlines() if " " in line]
        expected = f"https://github.com/{run.target.owner}/{run.target.name}.git"
        for url in urls:
            if url != expected:
                raise AppError(
                    "remote_mismatch",
                    "Configured remote does not match the selected GitHub repository.",
                    409,
                )
        return bool(urls)

    # Prepares the source review needed before publication.
    # Checks the repository and selected files without creating commits or remote repositories.
    async def preflight(
        self, run: ArtifactRun, selected_paths: list[str] | None = None
    ) -> ArtifactRun:
        if run.status in {"rejected", "cancelled", "ready_for_deployment"}:
            raise AppError("run_closed", "This run is closed; create a new plan and run.", 409)
        if run.source_branch is None:
            binding = workspace_path(
                str(self.store.source(run.run_id)), self.settings.allowed_roots
            )
            run.source_branch = (
                self.executor.run(binding, "symbolic-ref", "--short", "HEAD", check=False)
                .decode()
                .strip()
                or "main"
            )
        source = self.source(run)
        existing = self.has_remote(source, run)
        await self.connection.probe(require_create=not existing)
        actual = await self.connection.lookup(run.target)
        if existing:
            if actual is None:
                raise AppError(
                    "remote_mismatch", "Configured GitHub repository could not be verified.", 409
                )
            run.target = actual
            run.progress.repository_verified = True
            if not actual.private:
                run.warnings.append(
                    "Selected GitHub repository is public; approved source will be public."
                )
        else:
            if actual is not None and not run.progress.creation_intent:
                raise AppError(
                    "git_collision",
                    "Selected repository already exists; configure its verified remote explicitly.",
                    409,
                )
            if not run.target.private:
                raise AppError(
                    "private_repository_required", "New repositories must be private.", 409
                )
            run.target.clone_url = f"https://github.com/{run.target.owner}/{run.target.name}.git"
        head = self.executor.head(source)
        if head:
            self.executor.scan_history(source, head)
        if not existing:
            if head:
                before = self.executor.tree(source, head)
                if selected_paths is not None and set(selected_paths) != set(before):
                    raise AppError(
                        "baseline_selection",
                        "Existing history publishes all tracked files; select all baseline files.",
                        409,
                    )
                files = []
                for path, facts in before.items():
                    content = self.executor.blob(source, facts["object"])
                    files.append(
                        ReviewedFile(
                            path=path,
                            category="baseline",
                            before_hash=None,
                            after_hash=facts["hash"],
                            mode=facts["mode"],
                            staged_ref=self.store.stage(content),
                        )
                    )
            else:
                files = snapshot_files(source, selected_paths or [], self.store)
            _, inventory = capture_source(source, self.settings.max_file_bytes)
            build_review(
                run,
                "baseline_publication",
                files,
                file_diff(files, self.store),
                head,
                excluded_paths=sorted(
                    set(inventory) - {file.path for file in files} | set(source_exclusions(source))
                ),
            )
            run.status = "awaiting_source_publication"
        elif head:
            run.progress.baseline_sha = head
            run.progress.baseline_pushed = True
            self.snapshot_review(run, source, selected_paths)
        else:
            raise AppError(
                "missing_initial_commit",
                "An existing remote requires a selected local baseline commit.",
                409,
            )
        self.store.save_run(run)
        return run

    # Builds a review of uncommitted source changes.
    # Shows which files were selected and which were left out.
    def snapshot_review(
        self, run: ArtifactRun, source: Path, selected_paths: list[str] | None = None
    ) -> None:
        baseline = self.executor.tree(source, run.progress.baseline_sha)
        _, current = capture_source(source, self.settings.max_file_bytes)
        exclusions = source_exclusions(source)
        changed = {
            path
            for path in current.keys() | baseline.keys()
            if current.get(path)
            != (
                {"hash": baseline[path]["hash"], "mode": baseline[path]["mode"]}
                if path in baseline
                else None
            )
        }
        dirty = bool(self.executor.run(source, "status", "--porcelain", "--untracked-files=all"))
        if not changed and not dirty:
            if exclusions:
                run.warnings.append(
                    "Source contains excluded local paths; they will not be published."
                )
            run.status = "generating"
            return
        if selected_paths is None:
            run.status = "awaiting_source_publication"
            warning = (
                "Source has uncommitted changes; select snapshot paths "
                "or an empty list to omit them."
            )
            if warning not in run.warnings:
                run.warnings.append(warning)
            return
        if set(selected_paths) - changed:
            raise AppError(
                "snapshot_selection",
                "Snapshot selection must contain only changed source paths.",
                409,
            )
        files = (
            snapshot_files(source, selected_paths, self.store, baseline, "snapshot")
            if selected_paths
            else []
        )
        old = {
            file.path: self.executor.blob(source, baseline[file.path]["object"])
            for file in files
            if file.path in baseline
        }
        build_review(
            run,
            "source_snapshot",
            files,
            file_diff(files, self.store, old),
            run.progress.baseline_sha,
            excluded_paths=sorted((changed - set(selected_paths)) | set(exclusions)),
        )
        run.status = "awaiting_source_publication"

    # Finds or creates the private destination repository.
    # Records the creation request and checks the resulting owner and access.
    async def ensure_repository(self, run: ArtifactRun) -> None:
        require_approval(run, "baseline_publication")
        actual = await self.connection.lookup(
            run.target, creation_id=run.run_id if run.progress.creation_intent else None
        )
        if actual is None:
            if run.progress.creation_intent:
                raise AppError(
                    "repository_creation_pending",
                    "Repository creation was attempted; reconcile its visibility before retrying.",
                    409,
                )
            if run.progress.repository_verified:
                raise AppError(
                    "remote_mismatch", "Previously verified GitHub repository is unavailable.", 409
                )
            run.progress.creation_intent = True
            self.store.save_run(run)
            await self.connection.create(run.target, creation_id=run.run_id)
            actual = await self.connection.lookup(run.target, creation_id=run.run_id)
        elif not run.progress.creation_intent:
            raise AppError(
                "git_collision", "Repository appeared after preflight; review the collision.", 409
            )
        if actual is None or not actual.private or not actual.can_push:
            raise AppError(
                "github_mcp_metadata",
                "Created repository must be verified private and writable before any push.",
                409,
            )
        run.target = actual
        run.progress.repository_verified = True
        self.store.save_run(run)

    # Creates the approved starting commit when one is needed.
    # Uses a separate Git index so unrelated staged files stay untouched.
    def ensure_baseline(self, run: ArtifactRun, source: Path, review: ChangeReview) -> None:
        if run.source_head_sha:
            run.progress.baseline_sha = run.source_head_sha
            self.executor.scan_history(source, run.source_head_sha)
            self.store.save_run(run)
            return
        if not (source / ".git").exists():
            self.executor.run(source, "init", "--initial-branch=main", "--template=", ".")
        tree = self.executor.reviewed_tree(source, None, review.files, self.store)
        sha = self.executor.commit(
            source, tree, None, f"VoxOps approved baseline {run.run_id}", run.created_at.isoformat()
        )
        if run.progress.baseline_sha and run.progress.baseline_sha != sha:
            raise AppError("stale_review", "Recovered baseline commit changed.", 409)
        run.progress.baseline_sha = sha
        self.store.save_run(run)
        branch = self.executor.run(source, "symbolic-ref", "HEAD").decode().strip()
        head = self.executor.head(source)
        if head is None:
            self.executor.run(source, "update-ref", branch, sha, "0" * 40)
        elif head != sha:
            raise AppError("git_collision", "Source HEAD changed during initialization.", 409)
        self.executor.scan_history(source, sha)

    # Writes a file inside a managed working copy.
    # Checks each directory and replaces the destination without following links.
    def write_owned(
        self, destination: Path, relative: str, content: bytes | None, mode: str
    ) -> None:
        descriptors = []
        temporary = None
        try:
            current = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptors.append(current)
            parts = Path(relative).parts
            for component in parts[:-1]:
                try:
                    os.mkdir(component, 0o755, dir_fd=current)
                except FileExistsError:
                    pass
                current = os.open(
                    component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current
                )
                descriptors.append(current)
            if content is None:
                try:
                    os.unlink(parts[-1], dir_fd=current)
                except FileNotFoundError:
                    pass
                return
            temporary = f".voxops-write-{uuid4().hex}"
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=current,
            )
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fchmod(stream.fileno(), 0o755 if mode == "100755" else 0o644)
                os.fsync(stream.fileno())
            os.replace(temporary, parts[-1], src_dir_fd=current, dst_dir_fd=current)
            temporary = None
        except OSError:
            raise AppError(
                "unsafe_destination", "Owned worktree write was not safe.", 409
            ) from None
        finally:
            if temporary:
                os.unlink(temporary, dir_fd=descriptors[-1])
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    # Finds or creates the working copy for a run.
    # Records ownership and fills it with the approved project files.
    def ensure_worktree(self, run: ArtifactRun, source: Path) -> Path:
        identifier = run.worktree_id or run.run_id
        if run.branch != f"voxops/{run.run_id}" or identifier != run.run_id:
            raise AppError(
                "worktree_collision", "Run branch does not match its owned namespace.", 409
            )
        destination = self.worktrees.root / identifier
        if (
            self.worktrees.root.is_symlink()
            or destination.is_symlink()
            or destination.resolve().is_relative_to(source)
        ):
            raise AppError("unsafe_destination", "Managed worktree destination is unsafe.", 409)
        self.worktrees.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = self.worktrees.root.stat()
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
            raise AppError(
                "unsafe_destination", "Managed worktree root must be owned by the executor.", 409
            )
        if run.worktree_id:
            record, bound_source = self.worktrees.load(identifier)
            if bound_source != source or record.branch != run.branch:
                raise AppError("ownership_invalid", "Worktree source binding changed.", 409)
            if destination.exists():
                branch = (
                    self.executor.run(destination, "symbolic-ref", "--short", "HEAD")
                    .decode()
                    .strip()
                )
                common = (
                    self.executor.run(destination, "rev-parse", "--git-common-dir").decode().strip()
                )
                common_path = (destination / common).resolve()
                if common_path != source / ".git":
                    raise AppError(
                        "ownership_invalid", "Worktree Git metadata binding changed.", 409
                    )
                if branch != run.branch:
                    raise AppError("worktree_collision", "Owned worktree branch changed.", 409)
                if record.status == "active":
                    return destination
        else:
            if destination.exists() or self.executor.run(
                source, "show-ref", "--verify", f"refs/heads/{run.branch}", check=False
            ):
                raise AppError(
                    "worktree_collision", "Run branch or worktree destination already exists.", 409
                )
            record = WorktreeRecord(
                id=identifier,
                source_repo_fingerprint=run.repo_fingerprint,
                source_commit_sha=run.progress.baseline_sha,
                branch=run.branch,
                location=str(destination),
                created_at=datetime.now(UTC),
                status="creating",
            )
            self.worktrees.save(record, source)
            run.worktree_id = identifier
            self.store.save_run(run)
        if not destination.exists():
            existing = (
                self.executor.run(
                    source, "rev-parse", "--verify", f"refs/heads/{run.branch}", check=False
                )
                .decode()
                .strip()
            )
            if existing:
                if existing != run.progress.baseline_sha:
                    raise AppError("worktree_collision", "Partial worktree branch changed.", 409)
                self.executor.run(
                    source, "worktree", "add", "--no-checkout", str(destination), run.branch
                )
            else:
                self.executor.run(
                    source,
                    "worktree",
                    "add",
                    "--no-checkout",
                    "-b",
                    run.branch,
                    str(destination),
                    run.progress.baseline_sha,
                )
        entries = self.executor.tree(source, run.progress.baseline_sha)
        for relative, facts in entries.items():
            data = safe_read(destination, relative, self.settings.max_file_bytes)
            if data is not None and digest(data) != facts["hash"]:
                raise AppError(
                    "worktree_changed", "Partial worktree contains unexpected content.", 409
                )
            self.write_owned(
                destination, relative, self.executor.blob(source, facts["object"]), facts["mode"]
            )
        self.executor.run(destination, "read-tree", run.progress.baseline_sha)
        record.status = "active"
        self.worktrees.save(record, source)
        return destination

    # Copies approved source changes into the working copy.
    # Checks original file checksums before writing the saved contents.
    def apply_snapshot(self, run: ArtifactRun, destination: Path, review: ChangeReview) -> None:
        for file in review.files:
            current = safe_read(destination, file.path, self.settings.max_file_bytes)
            actual_hash = digest(current) if current is not None else None
            if actual_hash not in {file.before_hash, file.after_hash}:
                raise AppError(
                    "worktree_changed", "Snapshot conflicts with changed worktree content.", 409
                )
            content = self.store.read_blob(file.staged_ref) if file.staged_ref else None
            if content is not None:
                check_secret(file.path, content, allow_binary=True)
            self.write_owned(destination, file.path, content, file.mode)

    # Prepares the working copy for file generation.
    # Requires approved source choices and applies the selected changes.
    async def prepare(self, run: ArtifactRun) -> ArtifactRun:
        if run.status in {"rejected", "cancelled"}:
            raise AppError("approval_rejected", "Run was rejected or cancelled.", 409)
        source = self.source(run)
        if run.tasks and run.worktree_id:
            self.ensure_worktree(run, source)
            run.status = "generating"
            return run
        baseline_reviews = [
            item for item in run.reviews.values() if item.purpose == "baseline_publication"
        ]
        if baseline_reviews and not run.progress.baseline_pushed:
            review = require_approval(run, "baseline_publication")
            await self.ensure_repository(run)
            source = self.source(run)
            self.ensure_baseline(run, source, review)
            if not self.has_remote(source, run):
                self.executor.run(source, "remote", "add", "origin", run.target.clone_url)
            self.executor.push(source, run.target.clone_url, "main", run.progress.baseline_sha)
            run.progress.baseline_pushed = True
            self.store.save_run(run)
        if not run.progress.baseline_sha:
            raise AppError(
                "baseline_missing", "Run needs a verified committed source baseline.", 409
            )
        actual = await self.connection.lookup(run.target)
        if actual is None or actual != run.target:
            raise AppError("remote_mismatch", "Verified repository metadata changed.", 409)
        snapshots = [item for item in run.reviews.values() if item.purpose == "source_snapshot"]
        if not snapshots and run.source_head_sha:
            self.snapshot_review(run, source)
            if run.status == "awaiting_source_publication":
                self.store.save_run(run)
                return run
        snapshot = require_approval(run, "source_snapshot") if snapshots else None
        destination = self.ensure_worktree(run, source)
        if snapshot:
            self.apply_snapshot(run, destination, snapshot)
        run.status = "generating"
        self.store.save_run(run)
        return run

    # Builds a review of generated files and source changes.
    # Compares the working copy with its approved parent and includes validation results.
    def artifact_review(self, run: ArtifactRun) -> ChangeReview:
        source = self.source(run)
        if not run.worktree_id:
            raise AppError("worktree_missing", "Run has no owned worktree.", 409)
        destination = self.ensure_worktree(run, source)
        if self.executor.head(destination) != run.progress.baseline_sha:
            raise AppError(
                "worktree_changed", "Worktree HEAD differs from the approved baseline.", 409
            )
        baseline = self.executor.tree(destination, run.progress.baseline_sha)
        _, inventory = capture_source(destination, self.settings.max_file_bytes)
        proposals = {}
        validations = []
        for result in run.tasks.values():
            if result.status not in {"passed", "validated_existing", "skipped"}:
                raise AppError(
                    "artifacts_incomplete",
                    "All required artifacts must validate before review.",
                    409,
                )
            validations.extend(result.validations)
            if result.proposal:
                proposals[result.proposal.target_path] = result.proposal
                validations.extend(result.proposal.validations)
        for proposal in proposals.values():
            actual_file = inventory.get(proposal.target_path)
            staged = self.store.read_blob(proposal.staged_ref)
            if (
                actual_file is None
                or actual_file["hash"] != proposal.content_hash
                or digest(staged) != proposal.content_hash
            ):
                raise AppError(
                    "worktree_changed", "A validated proposal was not applied exactly.", 409
                )
        if set(run.tasks) != set(run.plan.tasks):
            raise AppError("artifacts_incomplete", "Artifact task results are incomplete.", 409)
        if any(item.status in {"failed", "needs_review"} for item in validations):
            raise AppError(
                "artifacts_incomplete", "Failed validation prevents publication review.", 409
            )
        snapshots = [item for item in run.reviews.values() if item.purpose == "source_snapshot"]
        snapshot_files_by_path = (
            {file.path: file for file in require_approval(run, "source_snapshot").files}
            if snapshots
            else {}
        )
        files = []
        old_bytes = {}
        for relative in sorted(inventory.keys() | baseline.keys()):
            original = baseline.get(relative)
            current = inventory.get(relative)
            if current == (
                {"hash": original["hash"], "mode": original["mode"]} if original else None
            ):
                continue
            proposal, snapshot = proposals.get(relative), snapshot_files_by_path.get(relative)
            if proposal:
                if current is None or current["hash"] != proposal.content_hash:
                    raise AppError(
                        "worktree_changed",
                        "Artifact bytes differ from their validated proposal.",
                        409,
                    )
                category = (
                    "source_patch" if proposal.artifact_type == "source_patch" else "artifact"
                )
            elif snapshot:
                if (current["hash"] if current else None) != snapshot.after_hash or (
                    current and current["mode"] != snapshot.mode
                ):
                    raise AppError(
                        "worktree_changed", "Source snapshot changed after approval.", 409
                    )
                category = "source_patch"
            else:
                raise AppError(
                    "worktree_changed", "Worktree contains changes outside approved proposals.", 409
                )
            content = safe_read(destination, relative, self.settings.max_file_bytes)
            if content is not None:
                check_secret(relative, content, allow_binary=proposal is None)
            files.append(
                ReviewedFile(
                    path=relative,
                    category=category,
                    before_hash=original["hash"] if original else None,
                    after_hash=current["hash"] if current else None,
                    mode=current["mode"] if current else original["mode"],
                    staged_ref=self.store.stage(content) if content is not None else None,
                )
            )
            if original:
                old_bytes[relative] = self.executor.blob(destination, original["object"])
        review = build_review(
            run,
            "artifact_publication",
            files,
            file_diff(files, self.store, old_bytes),
            run.progress.baseline_sha,
            validations=validations,
        )
        run.status = "awaiting_review"
        self.store.save_run(run)
        return review

    # Checks that a working copy still matches its approved review.
    # Compares file contents, permissions and Git index before committing.
    def verify_worktree(
        self, run: ArtifactRun, destination: Path, review: ChangeReview, tree: str
    ) -> None:
        expected = self.executor.tree(destination, tree)
        _, inventory = capture_source(destination, self.settings.max_file_bytes)
        expected_inventory = {
            path: {"hash": facts["hash"], "mode": facts["mode"]} for path, facts in expected.items()
        }
        if inventory != expected_inventory:
            raise AppError(
                "stale_review", "Worktree files or executable modes changed after review.", 409
            )
        index_tree = self.executor.run(destination, "write-tree").decode().strip()
        head = self.executor.head(destination)
        parent_tree = (
            self.executor.run(destination, "rev-parse", f"{review.parent_sha}^{{tree}}")
            .decode()
            .strip()
        )
        if index_tree not in {parent_tree, tree} or head not in {
            review.parent_sha,
            run.progress.artifact_sha,
        }:
            raise AppError(
                "worktree_changed", "Worktree index or HEAD contains unapproved changes.", 409
            )
        self.executor.tree(destination, index_tree)

    # Publishes the reviewed changes to the approved branch.
    # Creates the commit, checks its history and verifies the remote after pushing.
    async def publish(self, run: ArtifactRun) -> GitPublishResult:
        source = self.source(run)
        review = require_approval(run, "artifact_publication")
        if set(run.tasks) != set(run.plan.tasks) or any(
            result.status not in {"passed", "validated_existing", "skipped"}
            for result in run.tasks.values()
        ):
            raise AppError("artifacts_incomplete", "Required artifacts are not complete.", 409)
        actual = await self.connection.lookup(run.target)
        if actual is None or actual != run.target:
            raise AppError(
                "remote_mismatch", "Approved GitHub repository identity or permission changed.", 409
            )
        source = self.source(run)
        destination = self.ensure_worktree(run, source)
        tree = self.executor.reviewed_tree(destination, review.parent_sha, review.files, self.store)
        self.verify_worktree(run, destination, review, tree)
        require_publishable(run, review, self.executor.tree(destination, tree), self.settings)
        sha = self.executor.commit(
            destination,
            tree,
            review.parent_sha,
            f"VoxOps approved artifacts {run.run_id}",
            run.created_at.isoformat(),
        )
        if run.progress.artifact_sha and run.progress.artifact_sha != sha:
            raise AppError(
                "stale_review", "Recovered artifact commit differs from the approved commit.", 409
            )
        run.progress.commit_parent = review.parent_sha
        run.progress.commit_tree = tree
        run.progress.artifact_sha = sha
        run.status = "publishing"
        self.store.save_run(run)
        current = self.executor.head(destination)
        if current == review.parent_sha:
            self.executor.run(destination, "update-ref", f"refs/heads/{run.branch}", sha, current)
        elif current != sha:
            raise AppError("git_collision", "Owned branch changed during commit.", 409)
        self.executor.run(destination, "read-tree", sha)
        self.executor.scan_history(destination, sha)
        self.source(run)
        self.verify_worktree(run, destination, review, tree)
        attestation = require_publishable(
            run, review, self.executor.tree(destination, tree), self.settings
        )
        self.executor.push(destination, run.target.clone_url, run.branch, sha)
        remote_sha = self.executor.remote_sha(destination, run.target.clone_url, run.branch)
        if remote_sha != sha:
            raise AppError(
                "remote_mismatch", "Remote receipt differs from the approved commit.", 409
            )
        run.progress.remote_sha = remote_sha
        result = GitPublishResult(
            run_id=run.run_id,
            repository=run.target,
            branch=run.branch,
            baseline_sha=run.progress.baseline_sha,
            commit_sha=sha,
            remote_sha=remote_sha,
            review_id=review.review_id,
            verification_status=attestation.status,
            execution_attestation=attestation.model_copy(deep=True),
            execution_plan=run.execution.plan.model_copy(deep=True),
        )
        run.result, run.status = result, "ready_for_deployment"
        self.store.save_run(run)
        return result
