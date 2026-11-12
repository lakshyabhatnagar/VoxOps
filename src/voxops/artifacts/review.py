from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from voxops.core.errors import AppError
from voxops.domain.artifact_models import (
    ApprovalRecord,
    ArtifactRun,
    ChangeReview,
    ReviewedFile,
    ValidationResult,
)
from voxops.domain.execution_models import IncompleteAcknowledgement
from voxops.execution.candidate import (
    attestation_digest,
    execution_policy_fingerprint,
    validate_acknowledgement,
)
from voxops.storage.artifact_store import digest

POLICY_FILES = (
    "artifacts/review.py",
    "artifacts/security.py",
    "artifacts/validators.py",
    "artifacts/ci_actions.py",
    "artifacts/planner.py",
    "artifacts/service.py",
    "domain/artifact_models.py",
    "repository/git_executor.py",
    "repository/snapshot.py",
    "github/publisher.py",
    "github/connection.py",
)
POLICY_CODE = digest(
    {
        path: digest((Path(__file__).resolve().parents[1] / path).read_bytes())
        for path in POLICY_FILES
    }
)


# Calculate a comparison code for the checking and publication rules and relevant settings.
# Include Stage 3 execution rules alongside the existing file and Git checks.
# An approval created under different rules must not silently authorize a later run.
def policy_fingerprint(settings) -> str:
    return digest(
        {
            "code": POLICY_CODE,
            "execution": execution_policy_fingerprint(settings),
            "max_file_bytes": settings.max_file_bytes,
            "optional_tools": settings.artifact_optional_tools,
            "github_mcp_url": settings.github_mcp_url,
        }
    )


# Calculate a comparison code for the exact review contents.
# Leave out the digest field itself.
# The approval request uses this value to prove which displayed changes the user approved.
def review_digest(review: ChangeReview) -> str:
    return digest(review.model_dump(mode="json", exclude={"digest"}))


# Create a time-limited review of the selected files, diff, target repository, and expected
# actions.
# For final publication, attach a copy of the candidate's current verification report.
# Save the review on the run and calculate its comparison code before asking for approval.
def build_review(
    run: ArtifactRun,
    purpose: str,
    files: list[ReviewedFile],
    diff: str,
    parent_sha: str | None,
    validations: list[ValidationResult] | None = None,
    excluded_paths: list[str] | None = None,
) -> ChangeReview:
    if len({file.path for file in files}) != len(files):
        raise AppError("review_conflict", "Review contains duplicate file destinations.", 409)
    review = ChangeReview(
        review_id=uuid4().hex,
        run_id=run.run_id,
        purpose=purpose,
        plan_revision=run.plan.revision,
        plan_fingerprint=digest(run.plan.model_dump(mode="json")),
        policy_fingerprint=run.policy_fingerprint,
        source_fingerprint=run.source_fingerprint,
        source_head_sha=run.source_head_sha,
        source_branch=run.source_branch,
        parent_sha=parent_sha,
        target=run.target.model_copy(deep=True),
        branch=run.branch,
        files=sorted(files, key=lambda item: item.path),
        diff=diff,
        validations=validations or [],
        excluded_paths=sorted(excluded_paths or []),
        expires_at=datetime.now(UTC) + timedelta(hours=2),
        ci_may_run=any(file.path.startswith(".github/workflows/") for file in files),
        expected_actions=(
            ["Create private repository if absent", "Publish approved baseline"]
            if purpose == "baseline_publication"
            else ["Copy selected source changes into isolated worktree"]
            if purpose == "source_snapshot"
            else ["Commit approved files", "Push VoxOps branch", "Verify remote SHA"]
        ),
    )
    if purpose == "artifact_publication" and run.execution and run.execution.attestation:
        review.execution_attestation = run.execution.attestation.model_copy(deep=True)
        review.attestation_digest = attestation_digest(review.execution_attestation)
    review.digest = review_digest(review)
    run.reviews[review.review_id] = review
    return review


# Compare the repository target in a review with the run's current target.
# Permit supported metadata that was previously missing to be filled in.
# Reject changes that would send approved contents to a different destination.
def target_matches(review: ChangeReview, run: ArtifactRun) -> bool:
    old, new = review.target, run.target
    return (
        old.owner == new.owner
        and old.name == new.name
        and old.private == new.private
        and (old.repository_id is None or old.repository_id == new.repository_id)
        and (old.clone_url is None or old.clone_url == new.clone_url)
        and (not old.can_push or new.can_push)
    )


# Check that the saved review still describes this run and plan.
# Compare its source, destination, policy, expiry, and content comparison code.
# Reject a changed or expired review before it can support approval or publication.
def validate_review(run: ArtifactRun, review: ChangeReview) -> None:
    if (
        review.digest != review_digest(review)
        or review.expires_at <= datetime.now(UTC)
        or review.run_id != run.run_id
        or review.plan_revision != run.plan.revision
        or review.plan_fingerprint != digest(run.plan.model_dump(mode="json"))
        or review.policy_fingerprint != run.policy_fingerprint
        or review.source_fingerprint != run.source_fingerprint
        or review.source_head_sha != run.source_head_sha
        or review.source_branch != run.source_branch
        or review.branch != run.branch
        or not target_matches(review, run)
    ):
        raise AppError("stale_review", "Review changed or expired; request a new review.", 409)


# Record a decision for the exact reviewed contents, not just a run ID.
# Check incomplete-verification acknowledgement when approving a report with missing checks.
# Reject conflicting repeat decisions and save the accepted decision with its review identity.
def approve_review(
    run: ArtifactRun,
    review_id: str,
    digest: str,
    decision: str,
    incomplete_acknowledgement: IncompleteAcknowledgement | None = None,
) -> ApprovalRecord:
    review = run.reviews.get(review_id)
    if review is None:
        raise AppError("review_not_found", "Unknown change review.", 404)
    validate_review(run, review)
    if digest != review.digest:
        raise AppError("stale_review", "Approval digest does not match this review.", 409)
    if decision == "approve":
        validate_acknowledgement(review, incomplete_acknowledgement)
    previous = run.approvals.get(review_id)
    if previous:
        if (
            previous.digest != digest
            or previous.decision != decision
            or previous.incomplete_acknowledgement != incomplete_acknowledgement
        ):
            raise AppError(
                "approval_conflict", "This review already has a different decision.", 409
            )
        return previous
    if run.status in {"rejected", "cancelled"}:
        raise AppError("run_closed", "This run is closed; create a new plan and run.", 409)
    approval = ApprovalRecord(
        approval_id=uuid4().hex,
        review_id=review_id,
        run_id=run.run_id,
        digest=digest,
        decision=decision,
        incomplete_acknowledgement=incomplete_acknowledgement,
    )
    run.approvals[review_id] = approval
    if decision == "reject":
        run.status = "rejected"
    elif review.purpose == "artifact_publication":
        run.status = "approved"
    return approval


# Find the applicable review and saved approval for a particular action.
# Revalidate that review and require a matching approving decision.
# Return the reviewed contents only when they still authorize the operation about to run.
def require_approval(run: ArtifactRun, purpose: str) -> ChangeReview:
    if run.status in {"rejected", "cancelled"}:
        raise AppError("approval_rejected", "This run has been rejected or cancelled.", 409)
    reviews = [review for review in run.reviews.values() if review.purpose == purpose]
    if not reviews:
        raise AppError("approval_required", "Review and explicit approval are required.", 409)
    review = max(reviews, key=lambda item: item.created_at)
    validate_review(run, review)
    approval = run.approvals.get(review.review_id)
    if (
        approval is None
        or approval.decision != "approve"
        or approval.digest != review.digest
        or approval.run_id != run.run_id
        or approval.review_id != review.review_id
    ):
        raise AppError("approval_required", "Review and explicit approval are required.", 409)
    return review
