import json
import os
from pathlib import Path
from uuid import uuid4

from voxops.artifacts.generator import ArtifactGenerator, ModelBudget
from voxops.artifacts.planner import build_plan
from voxops.artifacts.review import approve_review, review_digest
from voxops.artifacts.security import check_secret, safe_read
from voxops.artifacts.validators import validate_cross_artifacts
from voxops.core.errors import AppError
from voxops.core.settings import Settings
from voxops.domain.artifact_models import (
    ArtifactPlan,
    ArtifactRun,
    RepositoryTarget,
    SourcePatchSpec,
    Strategy,
)
from voxops.domain.models import AIUsage
from voxops.execution.candidate import candidate_identity
from voxops.execution.environment import EnvironmentManager
from voxops.github.publisher import GitPublisher
from voxops.repository.git_inspector import inspect_git
from voxops.repository.path_policy import fingerprint, workspace_path
from voxops.repository.snapshot import capture_source
from voxops.repository.worktree_manager import WorktreeManager
from voxops.storage.artifact_store import ArtifactStore, digest
from voxops.storage.scan_store import ScanStore
from voxops.workflows.stage1_graph import Stage1Workflow
from voxops.workflows.stage2_graph import Stage2Graph


# Copies accepted files from private storage into the project working copy.
# Checks ownership, checksums, and secrets before safely replacing each file.
def apply_proposals(root: Path, run: ArtifactRun, store: ArtifactStore) -> None:
    for key, result in sorted(run.tasks.items()):
        proposal = result.proposal
        if proposal is None or proposal.creator == "existing":
            continue
        task = run.plan.tasks[key]
        if (
            result.status != "passed"
            or proposal.target_path != task.target_path
            or proposal.task_id != key
            or proposal.original_hash != task.existing_hash
        ):
            raise AppError(
                "proposal_conflict", "Proposal does not match its planned ownership.", 409
            )
        content = store.read_blob(proposal.staged_ref)
        if digest(content) != proposal.content_hash:
            raise AppError("proposal_conflict", "Proposal content hash changed.", 409)
        check_secret(task.target_path, content)
        current = safe_read(root, task.target_path)
        current_hash = digest(current) if current is not None else None
        if current_hash == proposal.content_hash:
            continue
        if current_hash != task.existing_hash:
            raise AppError("worktree_changed", "Artifact destination changed before apply.", 409)
        descriptors = []
        temporary = f".voxops-write-{uuid4().hex}"
        try:
            directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptors.append(directory)
            parts = Path(task.target_path).parts
            for part in parts[:-1]:
                try:
                    os.mkdir(part, mode=0o755, dir_fd=directory)
                except FileExistsError:
                    pass
                directory = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                )
                descriptors.append(directory)
            mode = 0o644
            if current is not None:
                mode = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False).st_mode & 0o777
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                mode,
                dir_fd=directory,
            )
            with os.fdopen(descriptor, "wb") as file:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            os.rename(temporary, parts[-1], src_dir_fd=directory, dst_dir_fd=directory)
        except OSError:
            raise AppError(
                "unsafe_destination", "Artifact destination could not be safely written.", 409
            ) from None
        finally:
            if descriptors:
                try:
                    os.unlink(temporary, dir_fd=descriptors[-1])
                except FileNotFoundError:
                    pass
            for descriptor in reversed(descriptors):
                os.close(descriptor)


# Coordinates planning, creating, reviewing, and publishing project files.
# Uses saved run state and a project lock to keep each operation consistent.
class ArtifactService:
    # Connects settings, scan storage, and working copy management.
    # Uses supplied file generator and publisher objects or creates the defaults.
    def __init__(
        self,
        settings: Settings,
        scans: ScanStore,
        worktrees: WorktreeManager,
        generator: ArtifactGenerator | None = None,
        publisher: GitPublisher | None = None,
    ):
        self.settings, self.scans, self.worktrees = settings, scans, worktrees
        self.store = (
            generator.store
            if generator
            else ArtifactStore(
                settings.data_dir, settings.max_file_bytes, settings.artifact_staged_bytes
            )
        )
        self.generator = generator or ArtifactGenerator(settings, self.store)
        self.publisher = publisher or GitPublisher(settings, self.store, worktrees)
        self.analysis = Stage1Workflow(settings, scans)
        self.environment = EnvironmentManager()

    def _environment_run(self, run_id: str, *, allow_active: bool = False):
        run = self.store.load_run(run_id)
        if run.execution is None or not run.worktree_id:
            raise AppError(
                "verification_not_prepared", "Execution verification is not prepared.", 409
            )
        if run.status in {"rejected", "cancelled", "ready_for_deployment", "publishing"} or (
            run.execution.status == "verifying" and not allow_active
        ):
            raise AppError(
                "verification_busy", "Environment configuration cannot be edited now.", 409
            )
        source = self.publisher.source(run)
        destination = Path(self.worktrees.inspect(run.worktree_id).location)
        _, inventory = capture_source(destination, self.settings.max_file_bytes)
        expected = candidate_identity(
            run,
            inventory,
            run.execution.plan,
            run.execution.env_schema,
            self.settings,
            run.execution.execution_id,
        )
        if run.execution.identity != expected:
            raise AppError(
                "verification_stale",
                "Execution candidate changed; prepare verification again.",
                409,
            )
        return run, source, destination, inventory

    def prepare_environment(self, run_id: str):
        previous = self.store.load_run(run_id)
        with self.store.project_lock(previous.repo_fingerprint):
            run, source, destination, inventory = self._environment_run(run_id)
            before = run.execution.env_schema.scan_id
            form = self.environment.prepare(run, destination, source)
            if not before:
                self.environment._invalidate(run)
                run.execution.identity = candidate_identity(
                    run,
                    inventory,
                    run.execution.plan,
                    run.execution.env_schema,
                    self.settings,
                    run.execution.execution_id,
                )
            self.store.save_run(run)
            return form

    def environment_form(self, run_id: str):
        previous = self.store.load_run(run_id)
        with self.store.project_lock(previous.repo_fingerprint):
            run, source, destination, _ = self._environment_run(run_id)
            try:
                return self.environment.form(run, source, destination)
            finally:
                self.store.save_run(run)

    def submit_environment(self, run_id: str, expected_digest: str, values: dict[str, str]):
        previous = self.store.load_run(run_id)
        with self.store.project_lock(previous.repo_fingerprint):
            run, source, destination, _ = self._environment_run(run_id)
            try:
                return self.environment.submit(run, source, destination, expected_digest, values)
            finally:
                self.store.save_run(run)

    def environment_authority(self, run_id: str, expected_generation: str | None = None):
        previous = self.store.load_run(run_id)
        with self.store.project_lock(previous.repo_fingerprint):
            run, source, destination, _ = self._environment_run(run_id, allow_active=True)
            try:
                return self.environment.authority(run, source, destination, expected_generation)
            finally:
                self.store.save_run(run)

    def clear_environment(self, run_id: str):
        previous = self.store.load_run(run_id)
        with self.store.project_lock(previous.repo_fingerprint):
            run = self.store.load_run(run_id)
            self.environment.clear(run_id)
            if run.execution is not None:
                self.environment._invalidate(run)
                for item in run.execution.env_schema.requirements:
                    if item.resolution in {"existing_env", "supplied", "default"}:
                        item.resolution = "missing"
                self.store.save_run(run)

    # Scans the project and makes a plan from the chosen strategies and source edits.
    # Checks that the source stayed unchanged during the scan, then saves the plan.
    async def plan(
        self,
        value: str,
        strategies: dict[str, Strategy],
        provider: str | None = None,
        patches: list[SourcePatchSpec] | None = None,
    ) -> ArtifactPlan:
        source = workspace_path(value, self.settings.allowed_roots)
        if self.store.directory.is_relative_to(source):
            raise AppError(
                "unsafe_storage", "Set VOXOPS_DATA_DIR outside the target repository.", 409
            )
        with self.store.project_lock(fingerprint(source)):
            before, _ = capture_source(source, self.settings.max_file_bytes)
            analysis = await self.analysis.analyze(str(source))
            plan = build_plan(source, analysis, strategies, provider, patches)
            if capture_source(source, self.settings.max_file_bytes)[0] != before:
                raise AppError("source_changed", "Source changed during analysis; plan again.", 409)
            self.store.records.write(
                f"artifact-plan-{plan.plan_id}.json",
                {
                    "schema_version": "1",
                    "plan": plan.model_dump(mode="json"),
                    "source": str(source),
                    "source_fingerprint": before,
                    "head": analysis.scan.git.head,
                },
            )
            return plan

    # Starts a saved plan for the chosen repository owner and name.
    # Checks the source and open questions, saves the run, and begins publication checks.
    async def start(
        self, plan_id: str, owner: str, name: str, selected_paths: list[str] | None = None
    ) -> ArtifactRun:
        path = self.store.directory / f"artifact-plan-{self.store.identifier(plan_id)}.json"
        try:
            saved = json.loads(path.read_text())
            plan = ArtifactPlan.model_validate(saved["plan"])
        except (OSError, ValueError, KeyError):
            raise AppError("plan_not_found", "Stored artifact plan is unavailable.", 404) from None
        source = workspace_path(saved["source"], self.settings.allowed_roots)
        with self.store.project_lock(plan.repo_fingerprint):
            if plan.unresolved_questions:
                raise AppError(
                    "ambiguous_plan", "Resolve the plan's questions before generation.", 409
                )
            if capture_source(source, self.settings.max_file_bytes)[0] != saved[
                "source_fingerprint"
            ] or (inspect_git(source).head != saved["head"]):
                raise AppError("source_changed", "Source changed after planning; plan again.", 409)
            identifier = uuid4().hex
            run = ArtifactRun(
                run_id=identifier,
                repo_fingerprint=plan.repo_fingerprint,
                scan_id=plan.scan_id,
                source_input_fingerprint=plan.source_input_fingerprint,
                source_fingerprint=saved["source_fingerprint"],
                source_head_sha=saved["head"],
                plan=plan,
                target=RepositoryTarget(owner=owner, name=name),
                branch=f"voxops/{identifier}",
            )
            self.store.bind_source(identifier, source)
            self.store.save_run(run)
            try:
                await self.publisher.preflight(run, selected_paths)
            except AppError as error:
                run.status, run.errors = "blocked", [error.category]
                self.store.save_run(run)
            return run

    # Updates which source files are selected for a run.
    # Repeats the appropriate publication checks before the working copy is prepared.
    async def select_source(self, run_id: str, paths: list[str]) -> ArtifactRun:
        previous = self.store.load_run(run_id)
        with self.store.project_lock(previous.repo_fingerprint):
            run = self.store.load_run(run_id)
            if run.status in {"rejected", "cancelled", "ready_for_deployment"}:
                raise AppError("run_closed", "This run is closed; create a new plan and run.", 409)
            if run.worktree_id:
                raise AppError(
                    "selection_closed", "Source selection has already been prepared.", 409
                )
            if run.progress.baseline_pushed:
                self.publisher.snapshot_review(run, self.publisher.source(run), paths)
            else:
                await self.publisher.preflight(run, paths)
            self.store.save_run(run)
            return run

    # Runs ready file jobs in a separate working copy and checks their combined results.
    # Applies accepted files and prepares a review, or records why the run is blocked.
    async def execute(self, run_id: str) -> ArtifactRun:
        previous = self.store.load_run(run_id)
        with self.store.project_lock(previous.repo_fingerprint):
            run = self.store.load_run(run_id)
            if run.status in {"awaiting_review", "approved", "ready_for_deployment", "rejected"}:
                return run
            try:
                await self.publisher.prepare(run)
                if run.status != "generating":
                    return run
                record = self.worktrees.inspect(run.worktree_id)
                destination = Path(record.location)
                future = {task.target_path for task in run.plan.tasks.values()}
                for task in run.plan.tasks.values():
                    for relative in task.required_paths:
                        if relative in future:
                            continue
                        path = destination / relative
                        if path.is_dir() and not path.is_symlink():
                            continue
                        if safe_read(destination, relative) is None:
                            raise AppError(
                                "required_source_missing",
                                "Selected baseline/snapshot omits a required build input.",
                                409,
                            )
                prior = [
                    attempt
                    for task in run.plan.tasks.values()
                    for attempt in self.generator.previous(run.run_id, task).attempts
                ]
                graph = Stage2Graph(
                    self.settings, self.generator, ModelBudget(self.settings, prior)
                )
                run.tasks = await graph.execute(run.run_id, destination, run.plan, run.tasks)
                attempts = [attempt for task in run.tasks.values() for attempt in task.attempts]
                run.model_usage = AIUsage(
                    status="used" if attempts else "skipped",
                    model=self.settings.model,
                    requests=sum(attempt.usage.requests for attempt in attempts),
                    input_tokens=sum(attempt.usage.input_tokens for attempt in attempts),
                    output_tokens=sum(attempt.usage.output_tokens for attempt in attempts),
                )
                if any(
                    task.status not in {"passed", "validated_existing", "skipped"}
                    for task in run.tasks.values()
                ):
                    run.status, run.errors = "blocked", ["artifacts_incomplete"]
                    return run
                run.status = "validating"
                contents = {
                    result.proposal.target_path: self.store.read_blob(
                        result.proposal.staged_ref
                    ).decode()
                    for result in run.tasks.values()
                    if result.proposal
                }
                findings = validate_cross_artifacts(run.plan, contents, root=destination)
                if not self.generator.valid(findings):
                    run.status, run.errors = "blocked", ["cross_artifact_validation_failed"]
                    return run
                self.publisher.source(run)
                apply_proposals(destination, run, self.store)
                self.publisher.artifact_review(run)
                latest = max(run.reviews.values(), key=lambda review: review.created_at)
                latest.validations.extend(findings)
                latest.digest = review_digest(latest)
                return run
            except AppError as error:
                run.status, run.errors = "blocked", [error.category]
                return run
            finally:
                self.store.save_run(run)

    # Records a person's decision on a specific review of the run.
    # Checks the source under the project lock and saves the decision.
    def approve(self, run_id: str, review_id: str, review_digest: str, decision: str):
        previous = self.store.load_run(run_id)
        with self.store.project_lock(previous.repo_fingerprint):
            run = self.store.load_run(run_id)
            self.publisher.source(run)
            approve_review(run, review_id, review_digest, decision)
            self.store.save_run(run)
            return run

    # Publishes the approved run using the Git publisher under the project lock.
    # Saves the resulting state or records an error if publication fails.
    async def publish(self, run_id: str) -> ArtifactRun:
        previous = self.store.load_run(run_id)
        with self.store.project_lock(previous.repo_fingerprint):
            run = self.store.load_run(run_id)
            try:
                await self.publisher.publish(run)
            except AppError as error:
                run.errors = [error.category]
                self.store.save_run(run)
                raise
            self.store.save_run(run)
            return run
