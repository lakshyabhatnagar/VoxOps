import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from langsmith import tracing_context
from pydantic_ai import Agent, UsageLimits
from pydantic_ai.exceptions import ModelHTTPError, UsageLimitExceeded
from pydantic_ai.usage import RunUsage

from voxops.agents.artifact_agent import create_artifact_agent
from voxops.artifacts.context_builder import build_context
from voxops.artifacts.patcher import repair_gitignore
from voxops.artifacts.security import check_secret, safe_read
from voxops.artifacts.validators import validate_artifact
from voxops.core.errors import AppError
from voxops.core.settings import Settings
from voxops.domain.artifact_models import (
    ArtifactAttempt,
    ArtifactPlan,
    ArtifactProposal,
    ArtifactTask,
    RepairIntent,
    TaskResult,
    ValidationResult,
)
from voxops.domain.models import AIUsage
from voxops.storage.artifact_store import ArtifactStore, digest


# Calculates how many tokens an earlier AI request counts against the budget.
# Uses the reserved amount when the request did not finish or report token usage.
def budget_charge(attempt: ArtifactAttempt) -> int:
    known = attempt.usage.input_tokens + attempt.usage.output_tokens
    return known if attempt.ended_at is not None and known else max(known, attempt.reserved_tokens)


# Tracks the shared AI request and token limits for a run.
# Reserves tokens before calls and records usage when calls finish.
class ModelBudget:
    # Sets the request and token limits from the settings and earlier attempts.
    # Creates a lock so concurrent jobs share the same remaining budget safely.
    def __init__(self, settings: Settings, prior: list[ArtifactAttempt] | None = None):
        self.settings = settings
        self.requests = sum(attempt.usage.requests for attempt in prior or [])
        self.tokens = sum(budget_charge(attempt) for attempt in prior or [])
        self.reserved = 0
        self.lock = asyncio.Lock()

    # Reserve at most one third of a file job's allowance for each model request.
    # An interrupted request still counts as spent, but cannot consume all three shares.
    # Keep the shared run budget and request limit enforced across concurrent jobs.
    async def reserve(self, task_remaining: int) -> int:
        async with self.lock:
            available = self.settings.artifact_max_tokens - self.tokens - self.reserved
            allocation = min(task_remaining, available, self.settings.artifact_task_tokens // 3)
            if self.requests >= self.settings.artifact_max_requests or allocation < 1000:
                raise AppError("model_budget_exhausted", "Artifact model budget is exhausted.", 409)
            self.requests += 1
            self.reserved += allocation
            return allocation

    # Releases a request's token reservation after the AI call finishes.
    # Adds the reported usage, or the reserved amount when usage is missing.
    async def finish(self, allocation: int, usage: RunUsage) -> None:
        async with self.lock:
            self.reserved -= allocation
            self.tokens += usage.input_tokens + usage.output_tokens or allocation


# Creates or checks one planned project file at a time.
# Keeps attempts and check results, and saves accepted file contents privately.
class ArtifactGenerator:
    # Connects the settings and storage used for file jobs.
    # Uses the supplied AI assistant or creates one from the configured model.
    def __init__(self, settings: Settings, store: ArtifactStore, agent: Agent | None = None):
        self.settings, self.store = settings, store
        self.agent = agent if agent is not None else create_artifact_agent(settings)

    # Builds the storage filename for one run and file job.
    # Uses the run ID and a checksum of the job ID to keep records separate.
    def record_name(self, run_id: str, task_id: str) -> str:
        return f"artifact-task-{self.store.identifier(run_id)}-{digest(task_id.encode())}.json"

    # Load one file job's saved result and model-attempt history.
    # Return a pending result if that job has never been saved.
    # Repairs and budgets read this same record so resuming cannot reset the number of content
    # attempts.
    def previous(self, run_id: str, task: ArtifactTask) -> TaskResult:
        path = self.store.directory / self.record_name(run_id, task.task_id)
        try:
            result = TaskResult.model_validate_json(path.read_text())
            if result.task_id != task.task_id:
                raise ValueError("Task mismatch")
            return result
        except FileNotFoundError:
            return TaskResult(task_id=task.task_id, status="pending")
        except (OSError, ValueError):
            raise AppError("task_record_invalid", "Stored artifact task is invalid.", 409) from None

    # Saves a file job's current result and attempts.
    # Returns the same result after writing its record.
    def persist(self, run_id: str, result: TaskResult) -> TaskResult:
        self.store.records.write(
            self.record_name(run_id, result.task_id), result.model_dump(mode="json")
        )
        return result

    # Creates a failed check result for a file job.
    # Includes the error category, message, and planned destination path.
    def failure(self, task: ArtifactTask, category: str, message: str) -> ValidationResult:
        return ValidationResult(
            validator_id=category, status="failed", messages=[message], paths=[task.target_path]
        )

    # Checks whether the file findings include a passing check.
    # Rejects findings with a failure or a request for human review.
    def valid(self, findings: list[ValidationResult]) -> bool:
        return (
            bool(findings)
            and any(item.status == "passed" for item in findings)
            and not any(item.status in {"failed", "needs_review"} for item in findings)
        )

    # Save checked file text privately and return the record describing where it belongs.
    # Check for secrets and record content, original, and expected parent hashes.
    # The service later verifies those hashes before applying the text to its separate worktree.
    def proposal(
        self,
        task: ArtifactTask,
        content: str,
        creator: str,
        findings: list[ValidationResult],
        parent_hash: str | None = None,
    ) -> ArtifactProposal:
        data = content.encode()
        check_secret(task.target_path, data)
        key = self.store.stage(data)
        return ArtifactProposal(
            task_id=task.task_id,
            target_path=task.target_path,
            artifact_type=task.artifact_type,
            original_hash=task.existing_hash,
            parent_hash=parent_hash if parent_hash is not None else task.existing_hash,
            content_hash=key,
            staged_ref=key,
            creator=creator,
            validations=findings,
        )

    # Validate existing content or ask the model to produce a checked replacement for one file
    # job.
    # For an execution-driven repair, verify the already applied proposal and pass bounded
    # diagnostic feedback.
    # Reuse the task's saved attempts and budget; successful or failed repair calls count toward
    # the same three-attempt limit.
    async def generate(
        self,
        run_id: str,
        root: Path,
        task: ArtifactTask,
        plan: ArtifactPlan,
        budget: ModelBudget,
        *,
        repair: RepairIntent | None = None,
    ) -> TaskResult:
        result = self.previous(run_id, task)
        if repair:
            run = self.store.load_run(run_id)
            recorded = run.tasks.get(task.task_id)
            proposal = recorded.proposal if recorded else None
            if (
                run.plan != plan
                or plan.tasks.get(task.task_id) != task
                or run.applied_hashes.get(task.task_id) != repair.expected_applied_hash
                or proposal is None
                or proposal.task_id != task.task_id
                or proposal.target_path != task.target_path
                or proposal.artifact_type != task.artifact_type
                or proposal.original_hash != task.existing_hash
                or (
                    proposal.content_hash != repair.expected_applied_hash
                    and proposal.parent_hash != repair.expected_applied_hash
                )
            ):
                raise AppError("proposal_conflict", "Repair lacks recorded applied authority.", 409)
        if repair is None and result.status in {"passed", "validated_existing", "skipped"}:
            return result
        if task.operation == "skip":
            result.status = "skipped"
            return self.persist(run_id, result)
        try:
            original_bytes = safe_read(root, task.target_path, self.settings.max_file_bytes)
            current_hash = digest(original_bytes) if original_bytes is not None else None
            if current_hash != (repair.expected_applied_hash if repair else task.existing_hash):
                raise AppError("source_changed", "Artifact source changed after planning.", 409)
            if repair:
                prior = result.proposal
                if (
                    prior is None
                    or prior.task_id != task.task_id
                    or prior.target_path != task.target_path
                    or prior.artifact_type != task.artifact_type
                    or prior.original_hash != task.existing_hash
                ):
                    raise AppError("proposal_conflict", "Repair task ownership changed.", 409)
                if (
                    result.status == "passed"
                    and prior.parent_hash == current_hash
                    and prior.content_hash != current_hash
                ):
                    return result
                if prior.content_hash != current_hash:
                    raise AppError(
                        "proposal_conflict", "Repair parent is not the applied proposal.", 409
                    )
                check_secret(task.target_path, repair.feedback.encode())
            original = original_bytes.decode() if original_bytes is not None else ""
            if original_bytes is not None:
                check_secret(task.target_path, original_bytes)
            if (
                repair is None
                and original_bytes is not None
                and task.artifact_type != "source_patch"
            ):
                findings = validate_artifact(
                    task,
                    original,
                    plan,
                    allow_tools=self.settings.artifact_optional_tools,
                    root=root,
                )
                if self.valid(findings):
                    result.status = "validated_existing"
                    result.proposal = self.proposal(task, original, "existing", findings)
                    result.validations = findings
                    return self.persist(run_id, result)
                result.validations = findings
                if task.artifact_type == "gitignore":
                    repaired = repair_gitignore(original)
                    findings = validate_artifact(task, repaired, plan, root=root)
                    if self.valid(findings):
                        result.proposal = self.proposal(
                            task, repaired, "deterministic_patch", findings
                        )
                        result.status, result.validations = "passed", findings
                        return self.persist(run_id, result)
            if self.agent is None:
                result.status, result.error_category = "blocked", "model_unavailable"
                return self.persist(run_id, result)
            packet = build_context(root, task, plan, self.settings)
        except (UnicodeError, AppError) as error:
            result.status = "blocked"
            result.error_category = (
                error.category if isinstance(error, AppError) else "invalid_text"
            )
            return self.persist(run_id, result)
        previous = None
        while len(result.attempts) < task.max_attempts:
            usage = RunUsage()
            spent = sum(budget_charge(item) for item in result.attempts)
            try:
                allocation = await budget.reserve(self.settings.artifact_task_tokens - spent)
            except AppError as error:
                result.status, result.error_category = "blocked", error.category
                return self.persist(run_id, result)
            attempt = ArtifactAttempt(
                run_id=run_id,
                task_id=task.task_id,
                attempt_number=len(result.attempts) + 1,
                started_at=datetime.now(UTC),
                model=self.settings.model,
                context_hash=packet.context_hash,
                reserved_tokens=allocation,
                usage=AIUsage(status="used", requests=1),
            )
            result.attempts.append(attempt)
            self.persist(run_id, result)
            try:
                prompt = json.dumps(
                    {
                        "context": packet.model_dump(mode="json"),
                        "task": task.model_dump(mode="json"),
                        "previous_candidate": previous,
                        "repair_feedback": [
                            item.model_dump(mode="json") for item in result.validations
                        ],
                        "execution_feedback": repair.feedback if repair else None,
                    }
                )
                with tracing_context(enabled=False):
                    response = await asyncio.wait_for(
                        self.agent.run(
                            prompt,
                            usage=usage,
                            retries=0,
                            usage_limits=UsageLimits(
                                request_limit=1, total_tokens_limit=allocation
                            ),
                            model_settings={
                                "max_tokens": min(8192, allocation // 2),
                                "timeout": self.settings.artifact_model_timeout,
                            },
                        ),
                        self.settings.artifact_model_timeout,
                    )
                output = response.output
                if output.target_path != task.target_path:
                    raise AppError(
                        "output_path_mismatch", "Output must match the planned path.", 409
                    )
                check_secret(task.target_path, output.content.encode())
                if output.questions:
                    raise AppError("ambiguous_artifact", "Artifact needs additional evidence.", 409)
                previous = output.content
                attempt.output_hash = digest(output.content.encode())
                findings = validate_artifact(
                    task,
                    output.content,
                    plan,
                    allow_tools=self.settings.artifact_optional_tools,
                    root=root,
                )
                result.validations = findings
                attempt.feedback = findings
                if self.valid(findings):
                    result.proposal = self.proposal(
                        task,
                        output.content,
                        "llm",
                        findings,
                        repair.expected_applied_hash if repair else None,
                    )
                    result.status, attempt.result = "passed", "passed"
                else:
                    result.status, attempt.result = "failed", "failed"
            except TimeoutError:
                attempt.error_category, result.status = "model_timeout", "blocked"
            except ModelHTTPError as error:
                attempt.error_category = (
                    "model_auth" if error.status_code in {401, 403} else "model_transport"
                )
                result.status = "blocked"
            except UsageLimitExceeded:
                attempt.error_category, result.status = "model_budget_exhausted", "blocked"
            except AppError as error:
                attempt.error_category = error.category
                result.validations = [self.failure(task, error.category, error.message)]
                attempt.feedback = result.validations
                result.status = (
                    "blocked"
                    if error.category in {"unsafe_content", "secret_detected", "ambiguous_artifact"}
                    else "failed"
                )
            except Exception:
                attempt.error_category = "invalid_model_output"
                result.validations = [
                    self.failure(
                        task,
                        "invalid_model_output",
                        "Model output failed the typed response schema.",
                    )
                ]
                attempt.feedback, result.status = result.validations, "failed"
            finally:
                await budget.finish(allocation, usage)
                attempt.ended_at = datetime.now(UTC)
                attempt.usage = AIUsage(
                    status="used",
                    model=self.settings.model,
                    requests=max(1, usage.requests),
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                )
                if attempt.result != "passed":
                    attempt.result = "blocked" if result.status == "blocked" else "failed"
                result.error_category = attempt.error_category
                self.persist(run_id, result)
            if result.status in {"passed", "blocked"}:
                return result
        result.status, result.error_category = "failed", "validation_exhausted"
        return self.persist(run_id, result)
