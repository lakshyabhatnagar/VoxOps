from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Literal

from pydantic import Field, field_validator, model_validator

from voxops.domain.execution_models import (
    ExecutionAttestation,
    ExecutionPlan,
    ExecutionRun,
    IncompleteAcknowledgement,
)
from voxops.domain.models import AIUsage, DeployableUnit, Model

Strategy = Literal["container", "static", "native"]
ArtifactType = Literal["dockerfile", "dockerignore", "gitignore", "ci", "provider", "source_patch"]
TaskStatus = Literal["pending", "validated_existing", "passed", "failed", "blocked", "skipped"]


# Checks that a file path stays within the repository and uses an allowed name.
# Returns the path or raises an error for unsafe paths and sensitive file names.
def artifact_path(value: str) -> str:
    path = PurePosixPath(value)
    hidden = {".github", ".dockerignore", ".gitignore"}
    if (
        not value
        or path.is_absolute()
        or str(path) != value
        or "\\" in value
        or any(part in {".", "..", ".git", ".voxops"} for part in path.parts)
        or any(part.startswith(".") and part not in hidden for part in path.parts)
        or any(ord(char) < 32 for char in value)
        or path.name.lower().endswith((".pem", ".key", ".p12"))
    ):
        raise ValueError("A safe canonical repository-relative artifact path is required")
    return value


# Describes a requested change to one existing source file.
# Records the safe path, expected file checksum, reason, and requested correction.
class SourcePatchSpec(Model):
    target_path: str
    expected_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    reason: str = Field(min_length=1, max_length=500)
    correction: str = Field(min_length=1, max_length=1000)

    _path = field_validator("target_path")(artifact_path)


# Records the outcome of one check on a file.
# Includes messages, affected paths and lines, tool details, and elapsed time.
class ValidationResult(Model):
    validator_id: str
    status: Literal["passed", "failed", "unavailable", "needs_review"]
    severity: Literal["info", "warning", "error"] = "error"
    kind: Literal["static", "tool_check", "cross_artifact"] = "static"
    messages: list[str] = Field(default_factory=list, max_length=30)
    paths: list[str] = Field(default_factory=list, max_length=30)
    lines: list[int] = Field(default_factory=list, max_length=30)
    tool_version: str | None = None
    duration_ms: int = 0


# Describe one planned file job, such as creating a Dockerfile.
# List the destination, source files it needs, earlier jobs it depends on, and its three-content-
# attempt limit.
# Stage 3 repairs reuse this same job instead of creating a fresh allowance for more model calls.
class ArtifactTask(Model):
    task_id: str
    unit_id: str | None = None
    artifact_type: ArtifactType
    target_path: str
    operation: Literal["create", "modify", "validate", "skip"] = "create"
    depends_on: list[str] = Field(default_factory=list)
    strategy: Strategy | None = None
    build_context: str = "."
    required_paths: list[str] = Field(default_factory=list)
    expected_commands: list[str] = Field(default_factory=list)
    existing_hash: str | None = None
    max_attempts: Literal[3] = 3
    reason: str
    evidence: list[str] = Field(default_factory=list)
    approval_class: Literal["artifact", "source_patch"] = "artifact"

    _path = field_validator("target_path")(artifact_path)


# Holds the chosen app strategies and all planned file jobs.
# Records scan details, warnings, and open questions, and checks the job dependencies.
class ArtifactPlan(Model):
    schema_version: Literal["1"] = "1"
    plan_id: str
    revision: int = Field(default=1, ge=1)
    scan_id: str
    repo_fingerprint: str
    source_input_fingerprint: str
    strategy_by_unit: dict[str, Strategy]
    units: list[DeployableUnit]
    tasks: dict[str, ArtifactTask]
    warnings: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    provider: str | None = None

    @model_validator(mode="after")
    # Checks that every job has a matching ID and its own destination file.
    # Rejects unknown dependencies and cycles that would prevent jobs from starting.
    def valid_dag(self) -> "ArtifactPlan":
        paths = set()
        for key, task in self.tasks.items():
            if key != task.task_id or task.target_path in paths:
                raise ValueError("Task identity or target ownership conflict")
            paths.add(task.target_path)
            if set(task.depends_on) - self.tasks.keys():
                raise ValueError("Unknown task dependency")
        pending = {key: set(task.depends_on) for key, task in self.tasks.items()}
        while pending:
            ready = {key for key, dependencies in pending.items() if not dependencies}
            if not ready:
                raise ValueError("Task dependency cycle")
            pending = {
                key: dependencies - ready
                for key, dependencies in pending.items()
                if key not in ready
            }
        return self


# Holds the files and app details we give the AI when asking it to write one file.
# Includes dependency lists and startup commands, while leaving out secret values.
class ContextPacket(Model):
    schema_version: Literal["1"] = "1"
    task_id: str
    source_fingerprint: str
    files: dict[str, str]
    file_hashes: dict[str, str]
    requirements: dict
    context_hash: str
    size_chars: int
    warnings: list[str] = Field(default_factory=list)


# Holds the AI's response for one requested file.
# Includes its path and contents, plus any explanation, assumptions, and questions.
class ArtifactOutput(Model):
    target_path: str
    content: str = Field(max_length=1048576)
    explanation: str = Field(default="", max_length=2000)
    assumptions: list[str] = Field(default_factory=list, max_length=20)
    questions: list[str] = Field(default_factory=list, max_length=20)

    _path = field_validator("target_path")(artifact_path)


# Record one model request for a file job.
# Keep timing, token usage, validation feedback, and the outcome of that particular try.
# Generation and later execution-driven repairs append to this same bounded history.
class ArtifactAttempt(Model):
    schema_version: Literal["1"] = "1"
    run_id: str
    task_id: str
    attempt_number: int = Field(ge=1, le=3)
    started_at: datetime
    ended_at: datetime | None = None
    model: str
    context_hash: str
    reserved_tokens: int = Field(ge=1)
    output_hash: str | None = None
    feedback: list[ValidationResult] = Field(default_factory=list)
    usage: AIUsage = Field(default_factory=AIUsage)
    result: Literal["passed", "failed", "blocked"] = "blocked"
    error_category: str | None = None


# Point to checked file text saved in VoxOps's private storage.
# Keep its intended destination and hashes of the proposed, original, and expected parent
# contents.
# Applying a repair checks that parent so it cannot overwrite a file changed since the proposal
# was prepared.
class ArtifactProposal(Model):
    schema_version: Literal["1"] = "1"
    task_id: str
    target_path: str
    artifact_type: ArtifactType
    original_hash: str | None
    parent_hash: str | None = None
    content_hash: str
    staged_ref: str
    creator: Literal["existing", "llm", "deterministic_patch"]
    validations: list[ValidationResult] = Field(default_factory=list)


# Describe a request to correct an already applied generated file.
# Name the exact applied content hash and provide bounded failure feedback.
# The generator verifies that authority before spending another attempt on the existing task.
class RepairIntent(Model):
    expected_applied_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    feedback: str = Field(min_length=1, max_length=2000)


# Keep one file job's accepted proposal, validation results, attempt history, and current outcome.
# A failed new repair may have saved attempts without replacing the run's accepted file proposal.
# This distinction lets accounting include failed calls while file application still requires
# checked content.
class TaskResult(Model):
    schema_version: Literal["1"] = "1"
    task_id: str
    status: TaskStatus
    proposal: ArtifactProposal | None = None
    attempts: list[ArtifactAttempt] = Field(default_factory=list, max_length=3)
    validations: list[ValidationResult] = Field(default_factory=list)
    error_category: str | None = None


# Describes the repository chosen for publication.
# Stores its owner, name, privacy, URL, ID, and whether it can be pushed to.
class RepositoryTarget(Model):
    owner: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
    repository_id: str | None = None
    private: bool = True
    clone_url: str | None = None
    can_push: bool = False


# Describes one file included in a review of changes.
# Records its path, file mode, before and after checksums, and saved content reference.
class ReviewedFile(Model):
    path: str
    category: Literal["baseline", "snapshot", "artifact", "source_patch"]
    before_hash: str | None
    after_hash: str | None
    mode: Literal["100644", "100755"] = "100644"
    staged_ref: str | None = None

    _path = field_validator("path")(artifact_path)


# Describe the exact file changes and actions shown to the user for a decision.
# Include the destination, source versions, expiry, and comparison code.
# For final publication, also attach the Stage 3 verification report the approval must match.
class ChangeReview(Model):
    schema_version: Literal["1"] = "1"
    review_id: str
    run_id: str
    purpose: Literal["baseline_publication", "source_snapshot", "artifact_publication"]
    plan_revision: int
    plan_fingerprint: str = ""
    policy_fingerprint: str | None = None
    source_fingerprint: str
    source_head_sha: str | None
    source_branch: str | None = None
    parent_sha: str | None
    target: RepositoryTarget
    branch: str
    files: list[ReviewedFile]
    excluded_paths: list[str] = Field(default_factory=list)
    diff: str = Field(default="", max_length=2097152)
    validations: list[ValidationResult] = Field(default_factory=list)
    ci_may_run: bool = False
    expected_actions: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime
    digest: str = ""
    execution_attestation: ExecutionAttestation | None = None
    attestation_digest: str | None = None


# Save the user's approval or rejection of a particular review.
# Keep the review comparison code so a later changed diff is not covered by this decision.
# When verification is incomplete, retain the matching acknowledgement of the exact unverified
# checks.
class ApprovalRecord(Model):
    schema_version: Literal["1"] = "1"
    approval_id: str
    review_id: str
    run_id: str
    digest: str
    decision: Literal["approve", "reject"]
    incomplete_acknowledgement: IncompleteAcknowledgement | None = None
    decided_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# Tracks which steps of publishing a run have completed.
# Keeps repository and commit details so later work can resume from saved progress.
class PublicationProgress(Model):
    creation_intent: bool = False
    repository_verified: bool = False
    baseline_sha: str | None = None
    baseline_pushed: bool = False
    commit_parent: str | None = None
    commit_tree: str | None = None
    artifact_sha: str | None = None
    remote_sha: str | None = None


# Record the repository, branch, and commit that Git publication actually verified.
# Include the approved review and Stage 3 verification status and report when present.
# The record explicitly says no deployment was executed, even though the branch is ready for a
# later deployment.
class GitPublishResult(Model):
    schema_version: Literal["1"] = "1"
    run_id: str
    repository: RepositoryTarget
    branch: str
    baseline_sha: str
    commit_sha: str
    remote_sha: str
    review_id: str
    status: Literal["ready_for_deployment"] = "ready_for_deployment"
    deployment_executed: Literal[False] = False
    verification_status: Literal["passed", "verification_incomplete"] | None = None
    execution_attestation: ExecutionAttestation | None = None
    execution_plan: ExecutionPlan | None = None
    published_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# Record which process currently owns a run's execution or repair operation.
# Give that operation a random epoch as well as its process identity.
# Progress callbacks compare the epoch so an interrupted older operation cannot overwrite newer
# state.
class OperationClaim(Model):
    epoch: str
    owner_process: str
    kind: Literal["execute", "repair"]


# Hold the saved progress for the complete file-generation and verification journey.
# Connect the plan, accepted files, worktree, reviews, approvals, model usage, and publication
# result.
# Its execution field links Stage 3 evidence to the same run while actual environment values stay
# in memory.
class ArtifactRun(Model):
    schema_version: Literal["1"] = "1"
    run_id: str
    repo_fingerprint: str
    scan_id: str
    source_input_fingerprint: str
    source_fingerprint: str
    source_head_sha: str | None
    source_branch: str | None = None
    policy_fingerprint: str | None = None
    plan: ArtifactPlan
    target: RepositoryTarget
    worktree_id: str | None = None
    branch: str
    status: Literal[
        "planning",
        "awaiting_source_publication",
        "generating",
        "validating",
        "awaiting_review",
        "approved",
        "publishing",
        "ready_for_deployment",
        "rejected",
        "failed",
        "cancelled",
        "blocked",
    ] = "planning"
    execution: ExecutionRun | None = None
    operation: OperationClaim | None = None
    tasks: dict[str, TaskResult] = Field(default_factory=dict)
    applied_hashes: dict[str, str] = Field(default_factory=dict)
    applied_parents: dict[str, str | None] = Field(default_factory=dict)
    reviews: dict[str, ChangeReview] = Field(default_factory=dict)
    approvals: dict[str, ApprovalRecord] = Field(default_factory=dict)
    progress: PublicationProgress = Field(default_factory=PublicationProgress)
    result: GitPublishResult | None = None
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    model_usage: AIUsage = Field(default_factory=AIUsage)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
