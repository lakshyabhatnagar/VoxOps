import asyncio
import json
import os
from datetime import UTC, datetime
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
    OperationClaim,
    RepositoryTarget,
    SourcePatchSpec,
    Strategy,
)
from voxops.domain.execution_models import EnvFormSchema, ExecutionAttestation, ExecutionRun
from voxops.domain.models import AIUsage
from voxops.execution.candidate import attestation_digest, candidate_identity
from voxops.execution.contexts import remove_context
from voxops.execution.docker_driver import DockerDriver
from voxops.execution.environment import EnvironmentManager
from voxops.execution.planner import build_execution_plan, freeze_plan_contexts
from voxops.execution.repair import propose_repair
from voxops.execution.runner import ExecutionOutcome, ExecutionRunner
from voxops.github.publisher import GitPublisher
from voxops.repository.git_inspector import inspect_git
from voxops.repository.path_policy import fingerprint, workspace_path
from voxops.repository.snapshot import capture_source
from voxops.repository.worktree_manager import WorktreeManager
from voxops.storage.artifact_store import ArtifactStore, digest
from voxops.storage.scan_store import ScanStore
from voxops.workflows.stage1_graph import Stage1Workflow
from voxops.workflows.stage2_graph import Stage2Graph


# Write accepted generated files into the run's separate working copy.
# Check the planned file owner, stored content hash, and currently applied parent before replacing
# anything.
# Use temporary files and safe directory handles so unexpected file changes cause an error instead
# of silent overwriting.
def apply_proposals(root: Path, run: ArtifactRun, store: ArtifactStore) -> None:
    for key, result in sorted(run.tasks.items()):
        proposal = result.proposal
        if proposal is None:
            continue
        task = run.plan.tasks[key]
        if (
            result.status not in {"passed", "validated_existing"}
            or (result.status == "validated_existing" and proposal.creator != "existing")
            or proposal.target_path != task.target_path
            or proposal.task_id != key
            or proposal.original_hash != task.existing_hash
            or proposal.artifact_type != task.artifact_type
        ):
            raise AppError(
                "proposal_conflict", "Proposal does not match its planned ownership.", 409
            )
        content = store.read_blob(proposal.staged_ref)
        if digest(content) != proposal.content_hash:
            raise AppError("proposal_conflict", "Proposal content hash changed.", 409)
        check_secret(task.target_path, content)
        current = safe_read(root, task.target_path, store.max_file_bytes)
        current_hash = digest(current) if current is not None else None
        parent = proposal.parent_hash
        if "parent_hash" not in proposal.model_fields_set:
            parent = task.existing_hash
        recorded = run.applied_hashes.get(key, task.existing_hash)
        replay = (
            key in run.applied_hashes
            and recorded == proposal.content_hash
            and current_hash == proposal.content_hash
            and key in run.applied_parents
            and run.applied_parents[key] == parent
        )
        if parent != recorded and not replay:
            raise AppError("proposal_conflict", "Proposal parent is not authorized.", 409)
        if current_hash == proposal.content_hash:
            run.applied_hashes[key] = proposal.content_hash
            run.applied_parents[key] = parent
            continue
        if current_hash != recorded:
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
            os.fsync(directory)
            run.applied_hashes[key] = proposal.content_hash
            run.applied_parents[key] = parent
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


# Coordinate planning, file generation, local verification, review, and approved branch
# publication.
# Reuse the existing generator, worktree manager, Docker runner, and Git publisher.
# Save progress under the project's lock while keeping actual environment values in memory.
class ArtifactService:
    _controls: dict[str, tuple[str, asyncio.Event]] = {}

    # Connect the service to settings, scan storage, worktree management, generation, and
    # publishing.
    # Reuse supplied generator and publisher objects when tests or demos provide them.
    # Create the environment manager and leave the Docker factory unset for normal production
    # execution.
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
        self.environment = EnvironmentManager(max_file_bytes=settings.max_file_bytes)
        self.driver_factory = None

    # Build an identifier for this process using its PID and Linux start information when
    # available.
    # The start information helps distinguish a new process that reused an old PID.
    # Saved operation claims use this identifier when deciding whether an old owner may still be
    # working.
    @staticmethod
    def _process_identity():
        pid = os.getpid()
        try:
            start = Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].split()[19]
        except (OSError, IndexError):
            start = "unknown"
        return f"{pid}:{start}"

    # Check whether a saved process identifier still belongs to a live matching process.
    # Compare the available start information as well as the PID.
    # Recovery must not take ownership away from a process that is still executing this run.
    @staticmethod
    def _owner_alive(owner):
        try:
            pid, start = owner.split(":", 1)
            if start == "unknown":
                os.kill(int(pid), 0)
                return True
            return Path(f"/proc/{int(pid)}/stat").read_text().split(") ", 1)[1].split()[19] == start
        except (OSError, ValueError, IndexError):
            return False

    # Reject a run that already has an active operation claim.
    # Distinguish a live owner from an interrupted operation that needs recovery.
    # This keeps two service requests from starting conflicting work on the same run.
    def _idle(self, run):
        if run.operation and self._owner_alive(run.operation.owner_process):
            raise AppError("verification_busy", "Another operation owns this run.", 409)
        if run.operation:
            raise AppError(
                "verification_interrupted", "Interrupted execution requires recovery.", 409
            )

    # Reload the run and check that this operation still owns its claim.
    # Compare the saved random epoch with the caller's epoch.
    # If ownership changed, stop instead of saving updates from an older operation.
    def _claimed(self, run_id, epoch):
        run = self.store.load_run(run_id)
        if not run.operation or run.operation.epoch != epoch:
            raise AppError("verification_stale", "Execution ownership changed.", 409)
        return run

    # Load and validate the run before reading or editing its environment configuration.
    # Find both the original repository and candidate worktree and compare the current candidate
    # identity.
    # Return those checked paths and records so form operations do not proceed on stale or closed
    # runs.
    def _environment_run(self, run_id: str, *, allow_active: bool = False):
        run = self.store.load_run(run_id)
        if not allow_active:
            self._idle(run)
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

    # Create or restore the environment form while holding the project lock.
    # If this is the first form, invalidate old verification and attach the new form to the
    # candidate identity.
    # Save only the form and state changes; actual configuration values remain in the manager's
    # memory.
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

    # Return the refreshed form for the current candidate under the project lock.
    # Let the environment manager detect changed local files or a lost session.
    # Save any resulting invalidation even if refreshing the form raises an error.
    def environment_form(self, run_id: str):
        previous = self.store.load_run(run_id)
        with self.store.project_lock(previous.repo_fingerprint):
            run, source, destination, _ = self._environment_run(run_id)
            try:
                return self.environment.form(run, source, destination)
            finally:
                self.store.save_run(run)

    # Accept values only for the current form and candidate.
    # Let the environment manager check the submitted form comparison code and values.
    # Save any changed field states or verification invalidation without storing the submitted
    # values themselves.
    def submit_environment(self, run_id: str, expected_digest: str, values: dict[str, str]):
        previous = self.store.load_run(run_id)
        with self.store.project_lock(previous.repo_fingerprint):
            run, source, destination, _ = self._environment_run(run_id)
            try:
                return self.environment.submit(run, source, destination, expected_digest, values)
            finally:
                self.store.save_run(run)

    # Check that current configuration still supports this run's verification or publication.
    # Optionally require the same generation marker that the completed checks used.
    # Save any invalidation discovered while making that comparison.
    def environment_authority(self, run_id: str, expected_generation: str | None = None):
        previous = self.store.load_run(run_id)
        with self.store.project_lock(previous.repo_fingerprint):
            run, source, destination, _ = self._environment_run(run_id, allow_active=True)
            try:
                return self.environment.authority(run, source, destination, expected_generation)
            finally:
                self.store.save_run(run)

    # Forget one run's in-memory configuration after confirming no operation owns it.
    # Invalidate the final report, review, and approval that depended on those values.
    # Mark previously resolved fields missing so a later refresh must establish their values
    # again.
    def clear_environment(self, run_id: str):
        previous = self.store.load_run(run_id)
        with self.store.project_lock(previous.repo_fingerprint):
            run = self.store.load_run(run_id)
            self._idle(run)
            self.environment.clear(run_id)
            if run.execution is not None:
                self.environment._invalidate(run)
                for item in run.execution.env_schema.requirements:
                    if item.resolution in {"existing_env", "supplied", "default"}:
                        item.resolution = "missing"
                self.store.save_run(run)

    # Analyze the selected repository and create the requested file-generation plan.
    # Check that the source stayed unchanged while analysis ran.
    # Save the resulting plan so starting a run uses these reviewed choices rather than new
    # guesses.
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
            plan = build_plan(
                source, analysis, strategies, provider, patches,
                max_file_bytes=self.settings.max_file_bytes,
            )
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

    # Create a saved run from an existing file-generation plan and GitHub target.
    # Check source identity and bind the run to its original repository.
    # Prepare the existing baseline/source-review path before generation proceeds in a separate
    # worktree.
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

    # Record which original source paths the user wants included in the run.
    # Reject selection changes after a worktree or later execution state has been prepared.
    # Create the appropriate source review using the existing publisher rather than committing
    # unreviewed edits.
    async def select_source(self, run_id: str, paths: list[str]) -> ArtifactRun:
        previous = self.store.load_run(run_id)
        with self.store.project_lock(previous.repo_fingerprint):
            run = self.store.load_run(run_id)
            self._idle(run)
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

    # Count requests and tokens across every task's saved model attempts.
    # Include failed repair calls even though their proposed contents were not accepted.
    # Update the run summary without replacing its accepted file proposals.
    def _record_model_usage(self, run: ArtifactRun) -> None:
        """Count model requests and tokens from the saved attempts for every task.

        Failed repairs also consume requests, even when their content is rejected.
        Read their saved attempts without replacing the run's accepted file proposals.
        """
        attempts = [
            attempt
            for task in run.plan.tasks.values()
            for attempt in self.generator.previous(run.run_id, task).attempts
        ]
        run.model_usage = AIUsage(
            status="used" if attempts else "skipped",
            model=self.settings.model,
            requests=sum(attempt.usage.requests for attempt in attempts),
            input_tokens=sum(attempt.usage.input_tokens for attempt in attempts),
            output_tokens=sum(attempt.usage.output_tokens for attempt in attempts),
        )

    # Continue a run through generation, environment setup, and local verification.
    # Use a separate worktree, claim ownership, and reuse existing generated files and attempt
    # records.
    # Return when input or approval is needed, or when verification has produced the final review.
    async def execute(self, run_id: str) -> ArtifactRun:
        previous = self.store.load_run(run_id)
        if (previous.operation and not self._owner_alive(previous.operation.owner_process)) or (
            not previous.operation
            and previous.execution
            and any(item.state != "removed" for item in previous.execution.resources)
        ):
            await self._recover(previous)
        with self.store.project_lock(previous.repo_fingerprint):
            run = self.store.load_run(run_id)
            self._idle(run)
            if (
                run.status in {"awaiting_review", "approved"}
                and run.execution
                and run.execution.status == "verification_incomplete"
            ):
                self.environment._invalidate(run)
            if run.status in {"awaiting_review", "approved", "ready_for_deployment", "rejected"}:
                return run
            if run.execution is None:
                await self.publisher.prepare(run)
                if run.status != "generating":
                    return run
            epoch = uuid4().hex
            run.operation = OperationClaim(
                epoch=epoch, owner_process=self._process_identity(), kind="execute"
            )
            cancel = asyncio.Event()
            self._controls[run_id] = (epoch, cancel)
            self.store.save_run(run)
        try:
            destination = Path(self.worktrees.inspect(run.worktree_id).location)
            if run.execution is None:
                future = {task.target_path for task in run.plan.tasks.values()}
                for task in run.plan.tasks.values():
                    for relative in task.required_paths:
                        if relative in future:
                            continue
                        path = destination / relative
                        if path.is_dir() and not path.is_symlink():
                            continue
                        if safe_read(destination, relative, self.settings.max_file_bytes) is None:
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
                generated = await graph.execute(run.run_id, destination, run.plan, run.tasks)
                with self.store.project_lock(run.repo_fingerprint):
                    run = self._claimed(run_id, epoch)
                    if cancel.is_set():
                        raise AppError("CANCELLED", "Execution was cancelled.", 409)
                    run.tasks = generated
                    self.store.save_run(run)
                self._record_model_usage(run)
                if any(
                    task.status not in {"passed", "validated_existing", "skipped"}
                    for task in run.tasks.values()
                ):
                    raise AppError("artifacts_incomplete", "Artifacts are incomplete.", 409)
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
                    raise AppError("cross_artifact_validation_failed", "Artifacts conflict.", 409)
                with self.store.project_lock(run.repo_fingerprint):
                    current = self._claimed(run_id, epoch)
                    current.tasks, current.model_usage = run.tasks, run.model_usage
                    current.status = "validating"
                    self.publisher.source(current)
                    apply_proposals(destination, current, self.store)
                    plan = build_execution_plan(destination, current.plan, self.settings)
                    _, inventory = capture_source(destination, self.settings.max_file_bytes)
                    execution_id = uuid4().hex
                    schema = EnvFormSchema(source_digest=digest({}))
                    identity = candidate_identity(
                        current, inventory, plan, schema, self.settings, execution_id
                    )
                    current.execution = ExecutionRun(
                        execution_id=execution_id,
                        artifact_run_id=run_id,
                        epoch=epoch,
                        plan=plan,
                        env_schema=schema,
                        identity=identity,
                    )
                    self.environment.prepare(current, destination, self.publisher.source(current))
                    current.execution.identity = candidate_identity(
                        current,
                        inventory,
                        plan,
                        current.execution.env_schema,
                        self.settings,
                        execution_id,
                    )
                    self.store.save_run(current)
            return await self._verify(run_id, epoch, destination, cancel)
        except AppError as error:
            with self.store.project_lock(previous.repo_fingerprint):
                run = self._claimed(run_id, epoch)
                run.status, run.errors = "blocked", [error.category]
                if run.execution:
                    run.execution.status = (
                        "cancelled" if error.category == "CANCELLED" else "blocked"
                    )
                self.store.save_run(run)
                run.operation = None
                return run
        except asyncio.CancelledError:
            cancel.set()
            with self.store.project_lock(previous.repo_fingerprint):
                run = self._claimed(run_id, epoch)
                if run.execution:
                    run.execution.status = "cancelled"
                self.store.save_run(run)
            raise
        finally:
            with self.store.project_lock(previous.repo_fingerprint):
                run = self._claimed(run_id, epoch)
                run.operation = None
                self.store.save_run(run)
            self._controls.pop(run_id, None)

    # Take over a run only after its former owner is no longer active.
    # Use saved ownership records to remove leftover Docker resources and mark running attempts
    # interrupted.
    # Release the recovery claim afterward, and report cleanup that could not be completed.
    async def _recover(self, previous):
        with self.store.project_lock(previous.repo_fingerprint):
            run = self.store.load_run(previous.run_id)
            if run.operation and self._owner_alive(run.operation.owner_process):
                raise AppError("verification_busy", "Another operation owns this run.", 409)
            epoch = uuid4().hex
            run.operation = OperationClaim(
                epoch=epoch, owner_process=self._process_identity(), kind="execute"
            )
            resources = list(run.execution.resources) if run.execution else []
            self.store.save_run(run)

        # Save a resource's latest cleanup state while recovery owns the run.
        # Replace the record with the same resource name instead of appending duplicate versions.
        # This lets another recovery attempt see which resources still need removal.
        def journal(item):
            with self.store.project_lock(run.repo_fingerprint):
                current = self._claimed(run.run_id, epoch)
                current.execution.resources = [
                    r for r in current.execution.resources if r.name != item.name
                ]
                current.execution.resources.append(item.model_copy(deep=True))
                self.store.save_run(current)

        try:
            for owner in {item.ownership_token for item in resources if item.state != "removed"}:
                driver = DockerDriver(
                    self.settings, run.run_id, owner, journal, asyncio.Event(), lambda text: text
                )
                driver.resources = [item for item in resources if item.ownership_token == owner]
                await driver.cleanup()
            with self.store.project_lock(run.repo_fingerprint):
                current = self._claimed(run.run_id, epoch)
                if current.execution:
                    for entries in current.execution.attempts.values():
                        for attempt in entries:
                            if attempt.status == "running":
                                attempt.status, attempt.ended_at = "interrupted", datetime.now(UTC)
                    current.execution.status = "interrupted"
                    self.store.save_run(current)
                    if any(item.state != "removed" for item in current.execution.resources):
                        raise AppError(
                            "RESOURCE_LIMIT", "Owned resource cleanup remains pending.", 409
                        )
        finally:
            with self.store.project_lock(run.repo_fingerprint):
                current = self._claimed(run.run_id, epoch)
                current.operation = None
                self.store.save_run(current)

    # Run the existing execution plan and save progress through callbacks.
    # If a supported failure permits repair, reuse the same Stage 2 task and bounded attempt
    # history.
    # Stop with missing configuration, failure, or a new final review whose report matches the
    # checked candidate.
    async def _verify(self, run_id, epoch, destination, cancel):
        initial = self.store.load_run(run_id)
        project = initial.repo_fingerprint
        retry_failed = True
        while True:
            with self.store.project_lock(project):
                run = self._claimed(run_id, epoch)
                source = self.publisher.source(run)
                self.environment.form(run, source, destination)
                try:
                    session = self.environment.authority(run, source, destination)
                except AppError as error:
                    if error.category == "env_unresolved":
                        run.execution.status = "waiting_for_env"
                        self.store.save_run(run)
                        run.operation = None
                        return run
                    raise
                execution = run.execution
                execution.status = "verifying"
                execution.epoch = epoch
                execution.owner_process = self._process_identity()
                execution.config_generations = dict(session.unit_generations)
                _, inventory = capture_source(destination, self.settings.max_file_bytes)
                expected_tree = digest(inventory)
                previous = None
                if execution.units:
                    previous = ExecutionOutcome(
                        execution.previous_plan or execution.plan,
                        dict(execution.units),
                        tuple(a for entries in execution.attempts.values() for a in entries),
                    )
                plan = execution.plan.model_copy(deep=True)
                self.store.save_run(run)

            # Reload the run under the project lock before saving an execution callback.
            # Require the same source tree, configuration markers, and operation owner.
            # Reject a stale callback rather than letting old Docker work certify changed inputs.
            def update(mutator, expected_tree=expected_tree, session=session):
                with self.store.project_lock(project):
                    current = self._claimed(run_id, epoch)
                    _, actual = capture_source(destination, self.settings.max_file_bytes)
                    if (
                        digest(actual) != expected_tree
                        or session.unit_generations != current.execution.config_generations
                    ):
                        raise AppError("verification_stale", "Execution inputs changed.", 409)
                    mutator(current.execution)
                    self.store.save_run(current)

            # Forward a Docker resource update through the checked run-update path.
            # Use the resource's generated name to find its existing record.
            # The saved run then tracks cleanup progress without the driver writing storage
            # directly.
            def journal(item):
                # Replace any older resource record with the same name.
                # Store a copy of the new state in the execution's resource list.
                # This preserves one current record for recovery and final cleanup checks.
                def record(state):
                    state.resources = [r for r in state.resources if r.name != item.name]
                    state.resources.append(item.model_copy(deep=True))

                update(record)

            # Save concrete execution settings discovered while preparing an app.
            # For example, the runner may now know the exact builder image ID and tool versions.
            # Update the app's job input codes so attempts refer to those finalized inputs.
            def prepared(item):
                # Replace this app's saved execution specification with the prepared version.
                # Apply its final input comparison code to all of its planned jobs.
                # The saved plan and subsequent attempt records now describe the same tools and
                # configuration.
                def record(state):
                    state.plan.units[item.spec.unit_id] = item.spec
                    for task in state.plan.tasks.values():
                        if task.unit_id == item.spec.unit_id:
                            task.input_digest = item.task_input_digest

                update(record)

            # Send a running or completed attempt to the checked update path.
            # Locate the history list for the attempt's task.
            # Replacing by attempt ID lets progress updates complete an existing attempt without
            # counting it twice.
            def attempt(item):
                # Remove the older saved version of this attempt, if present.
                # Append the newest version to its task's history.
                # This records status changes while keeping distinct retries as separate attempts.
                def record(state):
                    entries = state.attempts.setdefault(item.task_id, [])
                    entries[:] = [a for a in entries if a.attempt_id != item.attempt_id]
                    entries.append(item)

                update(record)

            # Save one app's completed verification result through the checked callback path.
            # Use the app ID as its key in the execution record.
            # Later review and repair decisions can inspect its checks and diagnostics.
            def unit(item):
                update(lambda state: state.units.__setitem__(item.unit_id, item))

            # Create the Docker driver for one app with this run's journal, cancellation, and
            # redaction hooks.
            # Use the supplied driver factory when a test or demo replaces the Docker boundary.
            # Otherwise create the real driver that operates the local Docker daemon.
            def driver(unit_id, session=session, execution_id=execution.execution_id):
                if self.driver_factory:
                    return self.driver_factory(unit_id, journal, cancel, session)
                return DockerDriver(
                    self.settings, run_id, execution_id, journal, cancel, session.redact
                )

            contexts = freeze_plan_contexts(destination, plan, self.settings)
            try:
                outcome = await ExecutionRunner(self.settings, driver).run(
                    plan,
                    contexts,
                    session,
                    config_generations=execution.config_generations,
                    previous=previous,
                    retry_failed=retry_failed,
                    on_prepared=prepared,
                    on_attempt=attempt,
                    on_unit=unit,
                )
            finally:
                for context in contexts.values():
                    remove_context(context)
            retry_failed = False
            if cancel.is_set():
                raise AppError("CANCELLED", "Execution was cancelled.", 409)
            with self.store.project_lock(project):
                run = self._claimed(run_id, epoch)
                execution = run.execution
                execution.plan = outcome.plan
                execution.previous_plan = outcome.plan.model_copy(deep=True)
                execution.units = dict(outcome.units)
                execution.attempts = {}
                for item in outcome.attempts:
                    execution.attempts.setdefault(item.task_id, []).append(item)
                _, inventory = capture_source(destination, self.settings.max_file_bytes)
                if digest(inventory) != expected_tree:
                    raise AppError("verification_stale", "Execution candidate changed.", 409)
                execution.identity = candidate_identity(
                    run,
                    inventory,
                    execution.plan,
                    execution.env_schema,
                    self.settings,
                    execution.execution_id,
                )
                statuses = {item.status for item in outcome.units.values()}
                if statuses <= {"passed", "verification_incomplete"}:
                    status = (
                        "verification_incomplete"
                        if "verification_incomplete" in statuses
                        else "passed"
                    )
                    execution.status = status
                    run.warnings.extend(f"Resolved earlier error: {error}" for error in run.errors)
                    run.errors = []
                    attestation = ExecutionAttestation(
                        identity=execution.identity, status=status, units=outcome.units
                    )
                    attestation.digest = attestation_digest(attestation)
                    execution.attestation = attestation
                    review = self.publisher.artifact_review(run)
                    review.digest = review_digest(review)
                    self.store.save_run(run)
                    run.operation = None
                    return run
                execution.status = "blocked" if "blocked" in statuses else "failed"
                self.store.save_run(run)
            diagnostics = [
                diagnostic for result in outcome.units.values() for diagnostic in result.diagnostics
            ]
            prior = [
                attempt
                for task in run.plan.tasks.values()
                for attempt in self.generator.previous(run_id, task).attempts
            ]
            budget = ModelBudget(self.settings, prior)
            proposal = None
            for diagnostic in diagnostics:
                proposal = await propose_repair(
                    run, destination, outcome.plan, diagnostic, self.generator, budget
                )
                if proposal is not None:
                    break
            if proposal is None:
                run.operation = None
                return run
            with self.store.project_lock(project):
                run = self._claimed(run_id, epoch)
                self._record_model_usage(run)
                if proposal.status != "passed" or proposal.proposal is None:
                    self.store.save_run(run)
                    run.operation = None
                    return run
                old_hash = run.applied_hashes[proposal.task_id]
                if proposal.proposal.content_hash == old_hash:
                    run.tasks[proposal.task_id] = proposal
                    self.store.save_run(run)
                    run.operation = None
                    return run
                run.tasks[proposal.task_id] = proposal
                contents = {
                    r.proposal.target_path: self.store.read_blob(r.proposal.staged_ref).decode()
                    for r in run.tasks.values()
                    if r.proposal
                }
                if not self.generator.valid(
                    validate_cross_artifacts(run.plan, contents, root=destination)
                ):
                    raise AppError(
                        "cross_artifact_validation_failed", "Repaired artifacts conflict.", 409
                    )
                apply_proposals(destination, run, self.store)
                execution = run.execution
                execution.attestation = None
                execution.previous_plan = outcome.plan.model_copy(deep=True)
                execution.plan = build_execution_plan(destination, run.plan, self.settings)
                _, inventory = capture_source(destination, self.settings.max_file_bytes)
                if (
                    self.environment.source_digest(run, destination)
                    != execution.env_schema.source_digest
                ):
                    self.environment.clear(run_id)
                    execution.env_schema = EnvFormSchema(source_digest=digest({}))
                    execution.identity = candidate_identity(
                        run,
                        inventory,
                        execution.plan,
                        execution.env_schema,
                        self.settings,
                        execution.execution_id,
                    )
                    self.environment.prepare(run, destination, self.publisher.source(run))
                execution.identity = candidate_identity(
                    run,
                    inventory,
                    execution.plan,
                    execution.env_schema,
                    self.settings,
                    execution.execution_id,
                )
                self.store.save_run(run)

    # Signal cancellation to the currently controlled execution, if one exists.
    # For an inactive run, check ownership and use recovery when leftover work needs cleanup.
    # Return the saved state rather than starting unrelated generation or publication work.
    async def cancel_execution(self, run_id: str):
        control = self._controls.get(run_id)
        if control:
            control[1].set()
        run = self.store.load_run(run_id)
        if control:
            return run
        with self.store.project_lock(run.repo_fingerprint):
            run = self.store.load_run(run_id)
            self._idle(run)
            if run.execution:
                run.execution.status = "cancelled"
            self.store.save_run(run)
            return run

    # Record approval or rejection for the exact displayed review and comparison code.
    # Pass any incomplete-verification acknowledgement to the existing review validation.
    # Save the decision so publication can require it later.
    def approve(
        self,
        run_id: str,
        review_id: str,
        review_digest: str,
        decision: str,
        incomplete_acknowledgement=None,
    ):
        previous = self.store.load_run(run_id)
        with self.store.project_lock(previous.repo_fingerprint):
            run = self.store.load_run(run_id)
            self._idle(run)
            self.publisher.source(run)
            approve_review(run, review_id, review_digest, decision, incomplete_acknowledgement)
            self.store.save_run(run)
            return run

    # Publish through the existing Git publisher only after checking the current run state.
    # When execution exists, supply a final configuration check that runs at publication
    # boundaries.
    # Save errors or the resulting branch information without deploying an application.
    async def publish(self, run_id: str) -> ArtifactRun:
        previous = self.store.load_run(run_id)
        with self.store.project_lock(previous.repo_fingerprint):
            run = self.store.load_run(run_id)
            self._idle(run)
            try:
                if run.execution:
                    source = self.publisher.source(run)
                    destination = Path(self.worktrees.inspect(run.worktree_id).location)

                    # Recheck that the configuration session still matches the markers used during
                    # verification.
                    # Use both the original repository and current candidate paths for that check.
                    # The publisher calls this before committing and pushing so changed or lost
                    # values cannot reuse old approval.
                    def authority():
                        self.environment.authority(
                            run, source, destination, run.execution.config_generations
                        )

                    await self.publisher.publish(run, authority=authority)
                else:
                    await self.publisher.publish(run)
            except AppError as error:
                run.errors = [error.category]
                self.store.save_run(run)
                raise
            run.warnings.extend(f"Resolved earlier error: {error}" for error in run.errors)
            run.errors = []
            self.store.save_run(run)
            return run
